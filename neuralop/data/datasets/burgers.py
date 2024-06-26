from pathlib import Path
from typing import List, Optional, Union

import torch
import numpy as np
from .pt_dataset import PTDataset
from .tensor_dataset import TensorDataset
from .web_utils import download_from_zenodo_record

class BurgersDataset(PTDataset):
    def __init__(
            root_dir: Union[Path, str], 
            n_train: int, 
            n_test: int, 
            batch_size: int=32, 
            test_batch_size: int=100,
            spatial_length: int=128,
            temporal_length: int=101,
            temporal_subsample: Optional[int]=None, 
            spatial_subsample: Optional[int]=None, 
            pad: int=0,
            grid_boundaries: List[List[int]]=[[0,1],[0,1]],
            channel_dim: int=1,
            download: bool=True,
            ):
        
        # convert root dir to path
        if isinstance(root_dir, str):
            root_dir = Path(root_dir)
        if not root_dir.exists():
            root_dir.mkdir(parents=True)
        
        # Zenodo record ID for Burgers dataset
        zenodo_record_id = "NA"

        # download darcy data from zenodo archive if passed
        if download:
            files_to_download = []
            already_downloaded_files = [x for x in root_dir.iterdir()]
            if f"burgers_train_{spatial_length}.pt" not in already_downloaded_files or \
            f"burgers_test_{spatial_length}.pt" not in already_downloaded_files:    
                    files_to_download.append(f"burgers.tgz")
            download_from_zenodo_record(record_id=zenodo_record_id,
                                        root=root_dir,
                                        files_to_download=files_to_download)
            
            super().__init__(root_dir=root_dir,
                             n_train=n_train,
                             n_tests=[n_test],
                             batch_size=batch_size,
                             test_batch_sizes=[test_batch_size],
                             train_resolution=None, #TODO: figure out how to integrate
                             test_resolutions=None, #TODO: ^
                             grid_boundaries=grid_boundaries,
                             subsampling_rate=[temporal_subsample, spatial_subsample],
                             positional_encoding=True,
                             encode_input=True,
                             encode_output=True,
                             encoding="channel-wise",
                             channel_dim=1
                             ) 
            

        

def load_burgers_1d(
    data_path, n_train, n_test, batch_train=32, batch_test=100, time=1, grid=[0, 1]
):

    data_path = Path(data_path).joinpath("burgers.pt").as_posix()
    data = torch.load(data_path)

    x_train = data[0:n_train, :, 0]
    x_test = data[n_train : (n_train + n_test), :, 0]

    y_train = data[0:n_train, :, time]
    y_test = data[n_train : (n_train + n_test), :, time]

    s = x_train.size(-1)

    if grid is not None:
        grid = torch.linspace(grid[0], grid[1], s + 1)[0:-1].view(1, -1)

        grid_train = grid.repeat(n_train, 1)
        grid_test = grid.repeat(n_test, 1)

        x_train = torch.cat((x_train.unsqueeze(1), grid_train.unsqueeze(1)), 1)
        x_test = torch.cat((x_test.unsqueeze(1), grid_test.unsqueeze(1)), 1)

    train_loader = torch.utils.data.DataLoader(
        torch.utils.data.TensorDataset(x_train, y_train),
        batch_size=batch_train,
        shuffle=False,
    )
    test_loader = torch.utils.data.DataLoader(
        torch.utils.data.TensorDataset(x_test, y_test),
        batch_size=batch_test,
        shuffle=False,
    )

    return train_loader, test_loader

def load_burgers_1dtime(
        data_path, n_train, n_test, batch_size=32, batch_size_test=100, 
        temporal_length=101, spatial_length=128, temporal_subsample=1, 
        spatial_subsample=1, pad=0):
    """
    Load burgers.mat data. Given the initial condition (t=0),
    predict timesteps 1 to temporal_length.
    """
    with np.load(data_path) as data:
        x_data = data['input']
        y_data = data['output']
        visc = data['visc']

    x_data = torch.from_numpy(x_data.astype(np.float32))
    x_data = x_data[:, :spatial_length:spatial_subsample]
    y_data = torch.from_numpy(y_data.astype(np.float32))
    y_data = y_data[:, :temporal_length:temporal_subsample, :spatial_length:spatial_subsample]
    visc = torch.from_numpy(visc.astype(np.float32)).item()

    x_train = x_data[:n_train]
    y_train = y_data[:n_train]
    x_test = x_data[n_train:n_train+n_test]
    y_test = y_data[n_train:n_train+n_test]

    domain_lengths = [spatial_length / 128, (temporal_length - 1) / 100]
    domain_starts = [0., 0.]

    spatial_length = spatial_length // spatial_subsample
    temporal_length = temporal_length // temporal_subsample

    if pad:
        x_train = torch.nn.ReplicationPad1d(pad)(x_train)
        x_test = torch.nn.ReplicationPad1d(pad)(x_test)
        spatial_length += 2 * pad
        temporal_length += 2 * pad
        incrs = [spatial_subsample / 128, temporal_subsample / 100]
        domain_lengths = [d + incr * pad for d, incr in zip(domain_lengths, incrs)]
        domain_starts = [-incr * pad for incr in incrs]

    # TODO: use include_endpoint arg here
    grid_x = torch.tensor(np.linspace(domain_starts[0], domain_lengths[0], spatial_length + 1)[:-1], dtype=torch.float)
    grid_t = torch.tensor(np.linspace(domain_starts[1], domain_lengths[1], temporal_length), dtype=torch.float)

    grid_x = grid_x.reshape(1, 1, spatial_length)
    grid_t = grid_t.reshape(1, temporal_length, 1)

    x_train = x_train.reshape(n_train, 1, spatial_length).repeat([1, temporal_length, 1])
    x_test = x_test.reshape(n_test, 1, spatial_length).repeat([1, temporal_length, 1])

    # TODO: add option to not have positional encoding
    x_train = torch.stack([x_train, 
                           grid_t.repeat([n_train, 1, spatial_length]),
                           grid_x.repeat([n_train, temporal_length, 1]) 
                           ], dim=3)
    x_test = torch.stack([x_test, 
                          grid_t.repeat([n_test, 1, spatial_length]),
                          grid_x.repeat([n_test, temporal_length, 1]) 
                          ], dim=3)

    x_train = x_train.permute(0, 3, 1, 2)
    x_test = x_test.permute(0, 3, 1, 2)
    y_train = y_train.unsqueeze(1)
    y_test = y_test.unsqueeze(1)

    train_db = TensorDataset(x_train, y_train)
    train_loader = torch.utils.data.DataLoader(train_db, batch_size=batch_size, shuffle=False)

    test_db = TensorDataset(x_test, y_test)
    test_loader = torch.utils.data.DataLoader(test_db, batch_size=batch_size_test, shuffle=False)

    output_encoder = None
    test_loaders = {'test':test_loader}

    return train_loader, test_loaders, output_encoder
