from typing import Union, Literal, Optional, List, Tuple, Generator
import torch
from torch import Tensor
import wandb


# normalization, pointwise gaussian
class UnitGaussianNormalizer:
    def __init__(
        self,
        x: Union[Tensor, Generator],
        eps: float = 1e-6,
        reduce_dim: List[int] = [0],
        verbose: bool = True,
    ):
        super().__init__()
        self.verbose = verbose
        self.reduce_dim = reduce_dim

        # x could be in shape of ntrain*n or ntrain*T*n or ntrain*n*T
        count, mean, std = self.compute_statistics(x, reduce_dim)
        self.eps = eps
        self.mean = mean
        self.std = std
        self.count = count

        if verbose:
            print(
                f"UnitGaussianNormalizer init on {count}, reducing over {reduce_dim}."
            )
            print(f"   Mean and std of shape {self.mean.shape}, eps={eps}")

    def compute_statistics(
        self, x: Union[Tensor, Generator], reduce_dim: List[int] = [0]
    ) -> Tuple[int, Tensor, Tensor]:
        """
        Compute mean and standard deviaion
        """
        if isinstance(x, Tensor):
            mean = torch.mean(x, reduce_dim, keepdim=True).squeeze(0)
            std = torch.std(x, reduce_dim, keepdim=True).squeeze(0)
            count = len(x)
        elif isinstance(x, Generator):
            first_item = next(x)
            total_sum = first_item
            total_sum_square = first_item**2
            count = 1
            for item in x:
                total_sum += item
                total_sum_square += item**2
                count += 1
            mean = total_sum / count
            # Compute unbiased variance
            variance = (total_sum_square - (total_sum**2) / count) / (count - 1)
            std = torch.sqrt(variance)
        else:
            raise ValueError(f"Unsupported type {type(x)}")
        return count, mean, std

    def encode(self, x):
        # x = x.view(-1, *self.sample_shape)
        x -= self.mean
        x /= self.std + self.eps
        # x = (x.view(-1, *self.sample_shape) - self.mean) / (self.std + self.eps)
        return x

    def decode(self, x, sample_idx=None):
        if sample_idx is None:
            std = self.std + self.eps  # n
            mean = self.mean
        else:
            if len(self.mean.shape) == len(sample_idx[0].shape):
                std = self.std[sample_idx] + self.eps  # batch*n
                mean = self.mean[sample_idx]
            if len(self.mean.shape) > len(sample_idx[0].shape):
                std = self.std[:, sample_idx] + self.eps  # T*batch*n
                mean = self.mean[:, sample_idx]

        # x is in shape of batch*n or T*batch*n
        # x = (x.view(self.sample_shape) * std) + mean
        # x = x.view(-1, *self.sample_shape)
        x *= std
        x += mean

        return x

    def cuda(self):
        self.mean = self.mean.cuda()
        self.std = self.std.cuda()
        return self

    def cpu(self):
        self.mean = self.mean.cpu()
        self.std = self.std.cpu()
        return self

    def to(self, device):
        self.mean = self.mean.to(device)
        self.std = self.std.to(device)
        return self


def count_params(model):
    """Returns the number of parameters of a PyTorch model"""
    return sum(
        [p.numel() * 2 if p.is_complex() else p.numel() for p in model.parameters()]
    )


def wandb_login(api_key_file="../config/wandb_api_key.txt", key=None):
    if key is None:
        key = get_wandb_api_key(api_key_file)

    wandb.login(key=key)


def set_wandb_api_key(api_key_file="../config/wandb_api_key.txt"):
    import os

    try:
        os.environ["WANDB_API_KEY"]
    except KeyError:
        with open(api_key_file, "r") as f:
            key = f.read()
        os.environ["WANDB_API_KEY"] = key.strip()


def get_wandb_api_key(api_key_file="../config/wandb_api_key.txt"):
    import os

    try:
        return os.environ["WANDB_API_KEY"]
    except KeyError:
        with open(api_key_file, "r") as f:
            key = f.read()
        return key.strip()


# Define the function to compute the spectrum
def spectrum_2d(signal, n_observations, normalize=True):
    """This function computes the spectrum of a 2D signal using the Fast Fourier Transform (FFT).

    Paramaters
    ----------
    signal : a tensor of shape (T * n_observations * n_observations)
        A 2D discretized signal represented as a 1D tensor with shape (T * n_observations * n_observations), where T is the number of time steps and n_observations is the spatial size of the signal.
        T can be any number of channels that we reshape into and n_observations * n_observations is the spatial resolution.
    n_observations: an integer
        Number of discretized points. Basically the resolution of the signal.

    Returns
    --------
    spectrum: a tensor
        A 1D tensor of shape (s,) representing the computed spectrum.
    """
    T = signal.shape[0]
    signal = signal.view(T, n_observations, n_observations)

    if normalize:
        signal = torch.fft.fft2(signal)
    else:
        signal = torch.fft.rfft2(
            signal, s=(n_observations, n_observations), normalized=False
        )

    # 2d wavenumbers following PyTorch fft convention
    k_max = n_observations // 2
    wavenumers = torch.cat(
        (
            torch.arange(start=0, end=k_max, step=1),
            torch.arange(start=-k_max, end=0, step=1),
        ),
        0,
    ).repeat(n_observations, 1)
    k_x = wavenumers.transpose(0, 1)
    k_y = wavenumers

    # Sum wavenumbers
    sum_k = torch.abs(k_x) + torch.abs(k_y)
    sum_k = sum_k

    # Remove symmetric components from wavenumbers
    index = -1.0 * torch.ones((n_observations, n_observations))
    index[0 : k_max + 1, 0 : k_max + 1] = sum_k[0 : k_max + 1, 0 : k_max + 1]

    spectrum = torch.zeros((T, n_observations))
    for j in range(1, n_observations + 1):
        ind = torch.where(index == j)
        spectrum[:, j - 1] = (signal[:, ind[0], ind[1]].sum(dim=1)).abs() ** 2

    spectrum = spectrum.mean(dim=0)
    return spectrum


if __name__ == "__main__":
    # Test the Gaussian normalizer
    X = torch.randn(100, 10)
    normalizer = UnitGaussianNormalizer(X)
    X_norm = normalizer.encode(X)
    X_recon = normalizer.decode(X_norm)
    print(torch.allclose(X, X_recon))

    # Create a generator
    def gen():
        for i in range(100):
            yield X[i]

    normalizer = UnitGaussianNormalizer(gen())
    X_norm = normalizer.encode(X)
    X_recon = normalizer.decode(X_norm)
    print(torch.allclose(X, X_recon))
