import torch
import numpy as np
import torch.nn as nn
import torch.nn.functional as F

from ..layers.recurrent_layers import RNO_layer
from ..layers.padding import DomainPadding


class RNO(nn.Module):
    """
    N-Dimensional Recurrent Neural Operator.
    
    The RNO has an identical architecture to the finite-dimensional GRU, with 
    the exception that linear matrix-vector multiplications are replaced by  
    Fourier layers (Li et al., 2021), and for regression problems, the output 
    nonlinearity is replaced by a SELU activation.

    Parameters
    ----------
    n_modes : int tuple
        number of modes to keep in Fourier Layer, along each dimension
        The dimensionality of the RNO is inferred from ``len(n_modes)``.
    hidden_channels : int
        width of the RNO (i.e. number of channels).
    in_channels : int, optional
        Number of input channels.
    out_channels : int, optional
        Number of output channels.
    n_layers : int
        Number of RNO layers to use.
    residual : bool
        Whether to use residual connections in the hidden layers.
    domain_padding : float list, optional
        If not None, percentage of padding to use, by default None
    domain_padding_mode : {'symmetric', 'one-sided'}, optional
        How to perform domain padding, by default 'one-sided'.
    output_scaling_factor : int or None
        Scaling factor of output resolution, by default None.
    fft_norm : str, optional
        by default 'forward'.
    separable : bool, default is False
        if True, use a depthwise separable spectral convolution
    factorization : str or None, {'tucker', 'cp', 'tt'}
        Tensor factorization of the parameters weight to use, by default None.
        * If None, a dense tensor parametrizes the Spectral convolutions
        * Otherwise, the specified tensor factorization is used.
    """
    def __init__(self, n_modes,
                hidden_channels,
                in_channels, 
                out_channels, 
                n_layers, 
                residual=False, 
                domain_padding=None, 
                domain_padding_mode='one-sided', 
                output_scaling_factor=None,
                fft_norm='forward',  
                separable=False,
                factorization=None
                ):
        super(RNO, self).__init__()

        self.n_modes = n_modes
        self.n_dims = len(n_modes)
        self.n_layers = n_layers
        self.width = width

        if domain_padding is not None and ((isinstance(domain_padding, list) and sum(domain_padding) > 0)):
                domain_padding = [0,] + domain_padding # avoid padding channel dimension
                self.domain_padding = DomainPadding(domain_padding=domain_padding, padding_mode=domain_padding_mode, output_scaling_factor=output_scaling_factor)
        else:
            self.domain_padding = None

        self.domain_padding_mode = domain_padding_mode

        self.in_channels = in_channels
        self.out_channels = out_channels
        self.residual = residual

        self.lifting = nn.Linear(in_channels, self.width)

        module_list = [RNO_layer(n_modes, width, return_sequences=True, fft_norm=fft_norm, factorization=factorization, separable=separable)
                                     for _ in range(n_layers - 1)]
        module_list.append(RNO_layer(n_modes, width, return_sequences=False, fft_norm=fft_norm, factorization=factorization, separable=separable))
        self.layers = nn.ModuleList(module_list)

        self.projection = nn.Linear(self.width, out_channels)
    
    def forward(self, x, init_hidden_states=None): # h must be padded if using padding
        batch_size, timesteps = x.shape[:2]
        dim = x.shape[-1]
        dom_sizes = x.shape[2 : 2 + self.n_dims]
        x_size = len(x.shape)

        if init_hidden_states is None:
            init_hidden_states = [None] * self.n_layers
        
        x = self.lifting(x)

        x = torch.movedim(x, x_size - 1, 2) # new shape: (batch, timesteps, dim, dom_size1, dom_size2, ..., dom_sizen)

        if self.domain_padding:
            x = self.domain_padding.pad(x)

        final_hidden_states = []
        for i in range(self.n_layers):
            pred_x = self.layers[i](x, init_hidden_states[i])
            if i < self.n_layers - 1:
                if self.residual:
                    x = x + pred_x
                else:
                    x = pred_x
                final_hidden_states.append(x[:, -1])
            else:
                x = pred_x
                final_hidden_states.append(x)
        h = final_hidden_states[-1]

        if self.domain_padding:
            h = h.unsqueeze(1) # add dim for padding compatibility
            h = self.domain_padding.unpad(h)
            h = h[:,0] # remove extraneous dim

        h = torch.movedim(h, 1, x_size - 2)

        pred = self.projection(h)

        return pred, final_hidden_states

    def predict(self, x, num_steps): # num_steps is the number of steps ahead to predict
        output = []
        states = [None] * self.n_layers
        
        for i in range(num_steps):
            pred, states = self.forward(x, states)
            output.append(pred)
            x = pred.reshape((pred.shape[0], 1, *pred.shape[1:]))

        return torch.stack(output, dim=1)