import sys
import copy

from configmypy import ConfigPipeline, YamlConfig, ArgparseConfig
import numpy as np
import torch
from torch.nn.parallel import DistributedDataParallel as DDP
from torch.utils.data import DataLoader
import wandb

from neuralop import H1Loss, LpLoss, Trainer, get_model
from neuralop.datasets.darcy import load_darcy_421_1k, load_darcy_421_5k, loader_to_residual_db
from neuralop.datasets.tensor_dataset import TensorDataset
from neuralop.datasets.data_transforms import DataProcessor, MGPatchingDataProcessor
from neuralop.losses import PointwiseQuantileLoss
from neuralop.models import UQNO
from neuralop.training import setup
from neuralop.training.callbacks import BasicLoggerCallback, Callback, CheckpointCallback
from neuralop.utils import get_wandb_api_key, count_model_params


# Read the configuration
config_name = "default"
pipe = ConfigPipeline(
    [
        YamlConfig(
            "./uqno_config.yaml", config_name="default", config_folder="../config"
        ),
        ArgparseConfig(infer_types=True, config_name=None, config_file=None),
        YamlConfig(config_folder="../config"),
    ]
)
config = pipe.read_conf()
config_name = pipe.steps[-1].config_name

# Set-up distributed communication, if using
device, is_logger = setup(config)

# Set up WandB logging
wandb_args = None
if config.wandb.log and is_logger:
    wandb.login(key=get_wandb_api_key())
    if config.wandb.name:
        wandb_name = config.wandb.name
    else:
        wandb_name = "_".join(
            f"{var}"
            for var in [
                config_name,
                config.tfno2d.n_layers,
                config.tfno2d.hidden_channels,
                config.tfno2d.n_modes_width,
                config.tfno2d.n_modes_height,
                config.tfno2d.factorization,
                config.tfno2d.rank,
                config.patching.levels,
                config.patching.padding,
            ]
        )
    wandb_args =  dict(
        config=config,
        name=wandb_name,
        group=config.wandb.group,
        project=config.wandb.project,
        entity=config.wandb.entity,
    )
    if config.wandb.sweep:
        for key in wandb.config.keys():
            config.params[key] = wandb.config[key]

# Make sure we only print information when needed
config.verbose = config.verbose and is_logger

# Print config to screen
if config.verbose and is_logger:
    pipe.log()
    sys.stdout.flush()

# Loading the Darcy flow dataset for training the base model
train_loader, train_db, test_loaders, data_processor = load_darcy_421_5k(
    data_root=config.data.train_data_path,
    n_train=config.data.n_train_total,
    n_test=config.data.n_test,
    sub=config.data.sub,
    test_batch_size=config.data.test_batch_size,
    batch_size=config.data.batch_size,
    positional_encoding=config.data.positional_encoding,
    encode_input=config.data.encode_input,
    encode_output=config.data.encode_output,
)


# split the training set up into train, residual_train, residual_calibration
train_db = train_loader.dataset
solution_train_db = TensorDataset(**train_db[:config.data.n_train_solution])
residual_train_db = TensorDataset(**train_db[config.data.n_train_solution:config.data.n_train_solution +\
                                  config.data.n_train_residual])
residual_calib_db = TensorDataset(**train_db[config.data.n_train_solution + config.data.n_train_residual:\
                                  config.data.n_train_solution + config.data.n_train_residual +\
                                  config.data.n_calib_residual])
print(len(solution_train_db))
print(len(residual_train_db))
print(len(residual_calib_db))

# convert dataprocessor to an MGPatchingDataprocessor if patching levels > 0
if config.patching.levels > 0:
    data_processor = MGPatchingDataProcessor(in_normalizer=data_processor.in_normalizer,
                                             out_normalizer=data_processor.out_normalizer,
                                             positional_encoding=data_processor.positional_encoding,
                                             padding_fraction=config.patching.padding,
                                             stitching=config.patching.stitching,
                                             levels=config.patching.levels)
print(f"{data_processor=}")
print(f"{data_processor.in_normalizer=}")
print(f"{data_processor.out_normalizer=}")
data_processor.train = True
data_processor = data_processor.to(device)

solution_model = get_model(config)
if config.load_soln_model:
    solution_model = solution_model.from_checkpoint(save_folder="./ckpt",
                                              save_name=config.soln_checkpoint)
    #solution_model.load_state_dict(torch.load("./ckpt/ziqi-model-main"))
solution_model = solution_model.to(device)

# Use distributed data parallel
if config.distributed.use_distributed:
    model = DDP(
        solution_model, device_ids=[device.index], output_device=device.index, static_graph=True
    )

# Create the optimizer
optimizer = torch.optim.Adam(
    solution_model.parameters(),
    lr=config.opt.solution.learning_rate,
    weight_decay=config.opt.solution.weight_decay,
)

if config.opt.solution.scheduler == "ReduceLROnPlateau":
    scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(
        optimizer,
        factor=config.opt.solution.gamma,
        patience=config.opt.solution.scheduler_patience,
        mode="min",
    )
elif config.opt.solution.scheduler == "CosineAnnealingLR":
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(
        optimizer, T_max=config.opt.solution.scheduler_T_max
    )
elif config.opt.solution.scheduler == "StepLR":
    scheduler = torch.optim.lr_scheduler.StepLR(
        optimizer, step_size=config.opt.solution.step_size, gamma=config.opt.solution.gamma
    )
else:
    raise ValueError(f"Got scheduler={config.opt.solution.scheduler}")


# Creating the losses
l2loss = LpLoss(d=2, p=2)
h1loss = H1Loss(d=2)
if config.opt.solution.training_loss == "l2":
    train_loss = l2loss
elif config.opt.solution.training_loss == "h1":
    train_loss = h1loss
else:
    raise ValueError(
        f'Got training_loss={config.opt.solution.training_loss} '
        f'but expected one of ["l2", "h1"]'
    )
eval_losses = {"h1": h1loss, "l2": l2loss}


                                              
if not config.load_soln_model:
    if config.verbose and is_logger:
        print("\n### MODEL ###\n", solution_model)
        print("\n### OPTIMIZER ###\n", optimizer)
        print("\n### SCHEDULER ###\n", scheduler)
        print("\n### LOSSES ###")
        print(f"\n * Train: {train_loss}")
        print(f"\n * Test: {eval_losses}")
        print(f"\n### Beginning Training...\n")
        sys.stdout.flush()

    trainer = Trainer(
        model=solution_model,
        n_epochs=config.opt.solution.n_epochs,
        device=device,
        data_processor=data_processor,
        amp_autocast=config.opt.solution.amp_autocast,
        wandb_log=config.wandb.log,
        log_test_interval=config.wandb.log_test_interval,
        log_output=config.wandb.log_output,
        use_distributed=config.distributed.use_distributed,
        verbose=config.verbose and is_logger,
        callbacks=[
            BasicLoggerCallback(wandb_args)
                ]
                )

    # Log parameter count
    if is_logger:
        n_params = count_model_params(solution_model)

        if config.verbose:
            print(f"\nn_params: {n_params}")
            sys.stdout.flush()

        if config.wandb.log:
            to_log = {"n_params": n_params}
            if config.n_params_baseline is not None:
                to_log["n_params_baseline"] = (config.n_params_baseline,)
                to_log["compression_ratio"] = (config.n_params_baseline / n_params,)
                to_log["space_savings"] = 1 - (n_params / config.n_params_baseline)
            wandb.log(to_log)
            #wandb.watch(model)


    solution_train_loader = DataLoader(solution_train_db,
                                    batch_size=config.data.batch_size,
                                        shuffle=True,
                                        num_workers=1,
                                        pin_memory=True,
                                        persistent_workers=False,
                                    )
    trainer.train(
        train_loader=solution_train_loader,
        test_loaders=test_loaders,
        optimizer=optimizer,
        scheduler=scheduler,
        regularizer=False,
        training_loss=train_loss,
        eval_losses=eval_losses,
    )

    solution_model.save_checkpoint(save_folder="./ckpt",save_name=config.soln_checkpoint)

######
# UQ #
######

## TODO
# compute quantile loss as follows:
# y = solution(x) - y_true
# x = residual(x)

# quantile(x,y) is pointwise quantile loss

# compute via data processor


class UQNODataProcessor(DataProcessor):
    def __init__(self, base_data_processor: DataProcessor, resid_data_processor: DataProcessor,
                 device: str="cpu"):
        """UQNODataProcessor converts tuple (G_hat(a,x), E(a,x)) and 
        sample['y'] = G_true(a,x) into the form expected by PointwiseQuantileLoss

        y_pred = E(a,x)
        y_true = abs(G_hat(a,x) - G_true(a,x))

        It also preserves any transformations that need to be performed
        on inputs/outputs from the solution model. 

        Parameters
        ----------
        base_data_processor : DataProcessor
            transforms required for base solution_model input/output
        resid_data_processor : DataProcessor
            transforms required for residual input/output
        device: str
            "cpu" or "cuda" 
        """
        super().__init__()
        self.base_data_processor = base_data_processor
        self.residual_normalizer = resid_data_processor.out_normalizer

        self.device = device
        self.scale_factor = None
    
    def set_scale_factor(self, factor):
        self.scale_factor = factor.to(device)
    
    def wrap(self, model):
        self.model = model
        return self

    def to(self, device):
        self.device = device
        self.base_data_processor = self.base_data_processor.to(device)
        self.residual_normalizer = self.residual_normalizer.to(device)
        return self
    
    def train(self):
        self.base_data_processor.train = True
    
    def eval(self):
        self.base_data_processor.train = False

    def preprocess(self, *args, **kwargs):
        """
        nothing required at preprocessing - just wrap the base DataProcessor
        """
        return self.base_data_processor.preprocess(*args, **kwargs)
    
    def postprocess(self, out, sample):
        """
        unnormalize the residual prediction as well as the output
        """
        self.base_data_processor.train = False
        g_hat, pred_uncertainty = out # UQNO returns a tuple
        #print(f"{torch.mean(pred_uncertainty)=}")
        #if not self.train
        #print(f"{torch.mean(pred_uncertainty)=}") 
        pred_uncertainty = self.residual_normalizer.inverse_transform(pred_uncertainty)
        # this is normalized
        #print(f"WARNING: no inverse xform, {torch.mean(pred_uncertainty)=}")

        g_hat, sample = self.base_data_processor.postprocess(g_hat, sample) #unnormalize g_hat

        g_true = sample['y'] # this is unnormalized in eval mode
        #print(f"{g_true.mean()=}")
        #print(f"{g_hat.mean()=}")
        sample['y'] = g_true - g_hat # both unnormalized
        # trying with normalized outs
        #print(f"{sample['y'].mean()=}")


        sample.pop('x') # remove x arg to avoid overloading loss args

        if self.scale_factor is not None:
            pred_uncertainty = pred_uncertainty * self.scale_factor
        return pred_uncertainty, sample

    def forward(self, **sample):
        # combine pre and postprocess for wrap
        sample = self.preprocess(sample)
        out = self.model(**sample)
        out, sample = self.postprocess(out, sample)
        return out, sample

residual_model = copy.deepcopy(solution_model)

if config.load_resid_model:
    residual_model = residual_model.from_checkpoint(save_folder='./ckpt/residual-savebest', save_name=config.resid_checkpoint)
residual_model = residual_model.to(device)
'''
if not config.load_resid_model:
    for resid_param, solution_param in zip(residual_model.parameters(), solution_model.parameters()):
        assert torch.isclose(resid_param,solution_param).all()'''
quantile_loss = PointwiseQuantileLoss(alpha = 1 - config.opt.alpha)


# Create the quantile model's optimizer
residual_optimizer = torch.optim.Adam(
    residual_model.parameters(),
    lr=config.opt.residual.learning_rate,
    weight_decay=config.opt.residual.weight_decay,
)

# reuse scheduler
if config.wandb.log and is_logger:
    wandb.finish()

if wandb_args is not None:
    uq_wandb_name = 'uq_'+ wandb_args['name']
    wandb_args['name'] = uq_wandb_name

## Training residual model
    
residual_train_loader_unprocessed = DataLoader(residual_train_db,
                                    batch_size=1,
                                        shuffle=True,
                                        num_workers=0,
                                        pin_memory=True,
                                        persistent_workers=False,
                                    )

# return dataset of x: a(x), y: G_hat(a,x) - u(x)
processed_residual_train_db, processed_residual_val_db, residual_data_processor =\
        loader_to_residual_db(solution_model, data_processor, residual_train_loader_unprocessed, device)

residual_data_processor = residual_data_processor.to(device)

if not config.load_resid_model:

    residual_train_loader = DataLoader(processed_residual_train_db,
                                    batch_size=config.data.batch_size,
                                        shuffle=True,
                                        num_workers=0,
                                        pin_memory=True,
                                        persistent_workers=False,
                                    )
    residual_val_loader = DataLoader(processed_residual_val_db,
                                    batch_size=config.data.batch_size,
                                        shuffle=True,
                                        num_workers=0,
                                        pin_memory=True,
                                        persistent_workers=False,
                                    )

    # config residual scheduler
    if config.opt.residual.scheduler == "ReduceLROnPlateau":
        resid_scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(
            residual_optimizer,
            factor=config.opt.residual.gamma,
            patience=config.opt.residual.scheduler_patience,
            mode="min",
        )
    elif config.opt.residual.scheduler == "CosineAnnealingLR":
        resid_scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(
            residual_optimizer, T_max=config.opt.residual.scheduler_T_max
        )
    elif config.opt.residual.scheduler == "StepLR":
        resid_scheduler = torch.optim.lr_scheduler.StepLR(
            residual_optimizer, step_size=config.opt.solution.step_size, gamma=config.opt.solution.gamma
        )
    else:
        raise ValueError(f"Got residual scheduler={config.opt.residual.scheduler}")
    # train on normalized inputs
    residual_data_processor.train = True
    residual_trainer = Trainer(model=residual_model,
                            n_epochs=config.opt.residual.n_epochs,
                            data_processor=residual_data_processor,
                            wandb_log=config.wandb.log,
                            device=device,
                            amp_autocast=config.opt.residual.amp_autocast,
                            log_test_interval=config.wandb.log_test_interval,
                            log_output=config.wandb.log_output,
                            use_distributed=config.distributed.use_distributed,
                            verbose=config.verbose and is_logger,
                            callbacks=[
                                    BasicLoggerCallback(wandb_args),
                                    #intCallback(save_dir='./ckpt/residual-savebest',
                                    #                   save_best='quantile')
                                        ]
                            )

    
    residual_trainer.train(train_loader=residual_train_loader,
                        test_loaders={'test':residual_val_loader}, 
                        optimizer=residual_optimizer,
                        scheduler=resid_scheduler,
                        regularizer=False,
                        training_loss=quantile_loss,
                        eval_losses={'quantile':quantile_loss,
                                     'l2':l2loss}
                        )



    #residual_model.save_checkpoint(save_folder='./ckpt', save_name=config.resid_checkpoint)

### calibrate trained quantile model
def get_coeff_quantile_idx(alpha, delta, n_samples, n_gridpts):
    """
    get the index of (ranked) sigma's for given delta and t
    we take the min alpha for given delta
    delta is percentage of functions that satisfy alpha threshold in domain
    alpha is percentage of points in ball on domain
    return 2 idxs
    domain_idx is the k for which kth (ranked descending by ptwise |err|/quantile_model_pred_err)
    value we take per function
    func_idx is the j for which jth (ranked descending) value we take among n_sample functions
    Note: there is a min alpha we can take based on number of gridpoints, n and delta, we specify lower bounds lb1 and lb2
    t needs to be between the lower bound and alpha
    """
    lb = np.sqrt(-np.log(delta)/2/n_gridpts)
    t = (alpha-lb)/3+lb # if t too small, will make the in-domain estimate conservative
    # too large will make the across-function estimate conservative. so we find a moderate t value
    print(f"we set alpha (on domain): {alpha}, t={t}")
    percentile = alpha-t
    domain_idx = int(np.ceil(percentile*n_gridpts))
    print(f"domain index: {domain_idx}'th largest of {n_gridpts}")

    # get function idx
    function_percentile= np.ceil((n_samples+1)*(delta-np.exp(-2*n_gridpts*t*t)))/n_samples
    function_idx = int(np.ceil(function_percentile*n_samples))
    print(f"function index: {function_idx}'th largest of {n_samples}")
    return domain_idx, function_idx

# create full uqno and uqno data processor
uqno = UQNO(base_model=solution_model, residual_model=residual_model)
uqno_data_proc = UQNODataProcessor(base_data_processor=data_processor,
                                   resid_data_processor=residual_data_processor,
                                               device=device)

uqno_data_proc.eval()

# list of (true error / uncertainty band), indexed by score
val_ratio_list = []
calib_loader = DataLoader(residual_calib_db, shuffle=True, batch_size=1)
with torch.no_grad():
    for idx, sample in enumerate(calib_loader):
        sample = uqno_data_proc.preprocess(sample)
        out = uqno(sample['x'])
        out, sample = uqno_data_proc.postprocess(out, sample)#.squeeze()
        ratio = torch.abs(sample['y'])/out
        val_ratio_list.append(ratio.squeeze().to("cpu"))
        del sample, out
val_ratios = torch.stack(val_ratio_list)

vr_view = val_ratios.view(val_ratios.shape[0], -1)


def eval_coverage_bandwidth(test_loader, alpha, device="cuda"):
    """
    Get percentage of instances hitting target-percentage pointwise coverage
    (e.g. pctg of instances with >1-alpha points being covered by quantile model)
    as well as avg band length
    """
    in_pred_list = []
    avg_interval_list = []
    

    with torch.no_grad():
        for _, sample in enumerate(test_loader):
            sample = {
                k:v.to(device) for k,v in sample.items()
                if torch.is_tensor(v)
            }
            sample = uqno_data_proc.preprocess(sample)
            out = uqno(**sample)
            uncertainty_pred, sample = uqno_data_proc.postprocess(out, sample)
            pointwise_true_err = sample['y']

            in_pred = (torch.abs(pointwise_true_err) < torch.abs(uncertainty_pred)).float().squeeze()
            avg_interval = torch.abs(uncertainty_pred.squeeze()).view(uncertainty_pred.shape[0],-1).mean(dim=1)
            avg_interval_list.append(avg_interval.to("cpu"))

            in_pred_flattened = in_pred.view(in_pred.shape[0], -1)
            in_pred_instancewise = torch.mean(in_pred_flattened,dim=1) >= 1-alpha # expected shape (batchsize, 1)
            in_pred_list.append(in_pred_instancewise.float().to("cpu"))
            #del x, y, pred, point_pred, in_pred_flattened
            #torch.cuda.empty_cache()

    in_pred = torch.cat(in_pred_list, axis=0)
    intervals = torch.cat(avg_interval_list, axis=0)
    mean_interval = torch.mean(intervals, dim=0)
    in_pred_percentage = torch.mean(in_pred, dim=0)
    print(f"{in_pred_percentage} of instances satisfy that >= {1-alpha} pts drawn are inside the predicted quantile")
    print(f"Mean interval width is {mean_interval}")
    return mean_interval, in_pred_percentage

'''
if config.wandb.log and is_logger:
    wandb.log(interval, percentage)'''

for alpha in [0.02, 0.05, 0.1]:
    for delta in [0.02, 0.05, 0.1]:
        # get quantile of domain gridpoints and quantile of function samples
        darcy_discretization = train_db[0]['x'].shape[-1] ** 2
        domain_idx, function_idx = get_coeff_quantile_idx(alpha, delta, n_samples=len(calib_loader), n_gridpts=darcy_discretization)

        val_ratios_pointwise_quantile = torch.topk(val_ratios.view(val_ratios.shape[0], -1),domain_idx+1, dim=1).values[:,-1]
        uncertainty_scaling_factor = torch.abs(torch.topk(val_ratios_pointwise_quantile, function_idx+1, dim=0).values[-1])
        print(f"scale factor: {uncertainty_scaling_factor}")

        uqno_data_proc.set_scale_factor(uncertainty_scaling_factor)

        uqno_data_proc.eval()
        print(f"------- for values {alpha=} {delta=} ----------")
        interval, percentage = eval_coverage_bandwidth(test_loader=test_loaders[train_db[0]['x'].shape[-1]], alpha=alpha, device=device)

if config.wandb.log and is_logger:
    wandb.finish()