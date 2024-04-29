from __future__ import annotations

import inspect
import os
import random
import shutil
import time
from datetime import datetime
from typing import TYPE_CHECKING, Literal

import numpy as np
import torch
from torch import Tensor, nn
from torch.optim.lr_scheduler import (
    CosineAnnealingLR,
    CosineAnnealingWarmRestarts,
    ExponentialLR,
    MultiStepLR,
)

from chgnet.model.model import CHGNet
from chgnet.utils import AverageMeter, determine_device, mae, write_json

if TYPE_CHECKING:
    from torch.utils.data import DataLoader

    from chgnet import TrainTask


class Trainer:
    """A trainer to train CHGNet using energy, force, stress and magmom."""

    def __init__(
        self,
        model: CHGNet | None = None,
        targets: TrainTask = "ef",
        energy_loss_ratio: float = 1,
        force_loss_ratio: float = 1,
        stress_loss_ratio: float = 0.1,
        mag_loss_ratio: float = 0.1,
        optimizer: str = "Adam",
        scheduler: str = "CosLR",
        criterion: str = "MSE",
        epochs: int = 50,
        starting_epoch: int = 0,
        learning_rate: float = 1e-3,
        print_freq: int = 100,
        save_model_per_epoch: int = 50,
        torch_seed: int | None = None,
        data_seed: int | None = None,
        use_device: str | None = None,
        check_cuda_mem: bool = True,
        **kwargs,
    ) -> None:
        """Initialize all hyper-parameters for trainer.

        Args:
            model (nn.Module): a CHGNet model
            targets ("ef" | "efs" | "efsm"): The training targets. Default = "ef"
            energy_loss_ratio (float): energy loss ratio in loss function
                Default = 1
            force_loss_ratio (float): force loss ratio in loss function
                Default = 1
            stress_loss_ratio (float): stress loss ratio in loss function
                Default = 0.1
            mag_loss_ratio (float): magmom loss ratio in loss function
                Default = 0.1
            optimizer (str): optimizer to update model. Can be "Adam", "SGD", "AdamW",
                "RAdam". Default = 'Adam'
            scheduler (str): learning rate scheduler. Can be "CosLR", "ExponentialLR",
                "CosRestartLR". Default = 'CosLR'
            criterion (str): loss function criterion. Can be "MSE", "Huber", "MAE"
                Default = 'MSE'
            epochs (int): number of epochs for training
                Default = 50
            starting_epoch (int): The epoch number to start training at.
            learning_rate (float): initial learning rate
                Default = 1e-3
            print_freq (int): frequency to print training output
                Default = 100
            save_model_per_epoch (int): frequency to save the model during the trainig
                Default = 50
            torch_seed (int): random seed for torch
                Default = None
            data_seed (int): random seed for random
                Default = None
            use_device (str, optional): The device to be used for predictions,
                either "cpu", "cuda", or "mps". If not specified, the default device is
                automatically selected based on the available options.
                Default = None
            check_cuda_mem (bool): Whether to use cuda with most available memory
                Default = True
            **kwargs (dict): additional hyper-params for optimizer, scheduler, etc.
        """
        # Store trainer args for reproducibility
        self.trainer_args = {
            k: v
            for k, v in locals().items()
            if k not in ["self", "__class__", "model", "kwargs"]
        }
        self.trainer_args.update(kwargs)

        self.model = model
        self.targets = targets

        if torch_seed is not None:
            torch.manual_seed(torch_seed)
        if data_seed:
            random.seed(data_seed)

        # Define optimizer
        if optimizer == "SGD":
            momentum = kwargs.pop("momentum", 0.9)
            weight_decay = kwargs.pop("weight_decay", 0)
            self.optimizer = torch.optim.SGD(
                model.parameters(),
                learning_rate,
                momentum=momentum,
                weight_decay=weight_decay,
            )
        elif optimizer == "Adam":
            weight_decay = kwargs.pop("weight_decay", 0)
            self.optimizer = torch.optim.Adam(
                model.parameters(), learning_rate, weight_decay=weight_decay
            )
        elif optimizer == "AdamW":
            weight_decay = kwargs.pop("weight_decay", 1e-2)
            self.optimizer = torch.optim.AdamW(
                model.parameters(), learning_rate, weight_decay=weight_decay
            )
        elif optimizer == "RAdam":
            weight_decay = kwargs.pop("weight_decay", 0)
            self.optimizer = torch.optim.RAdam(
                model.parameters(), learning_rate, weight_decay=weight_decay
            )

        # Define learning rate scheduler
        if scheduler in ["MultiStepLR", "multistep"]:
            scheduler_params = kwargs.pop(
                "scheduler_params",
                {
                    "milestones": [4 * epochs, 6 * epochs, 8 * epochs, 9 * epochs],
                    "gamma": 0.3,
                },
            )
            self.scheduler = MultiStepLR(self.optimizer, **scheduler_params)
            self.scheduler_type = "multistep"
        elif scheduler in ["ExponentialLR", "Exp", "Exponential"]:
            scheduler_params = kwargs.pop("scheduler_params", {"gamma": 0.98})
            self.scheduler = ExponentialLR(self.optimizer, **scheduler_params)
            self.scheduler_type = "exp"
        elif scheduler in ["CosineAnnealingLR", "CosLR", "Cos", "cos"]:
            scheduler_params = kwargs.pop("scheduler_params", {"decay_fraction": 1e-2})
            decay_fraction = scheduler_params.pop("decay_fraction")
            self.scheduler = CosineAnnealingLR(
                self.optimizer,
                T_max=10 * epochs,  # Maximum number of iterations.
                eta_min=decay_fraction * learning_rate,
            )
            self.scheduler_type = "cos"
        elif scheduler == "CosRestartLR":
            scheduler_params = kwargs.pop(
                "scheduler_params", {"decay_fraction": 1e-2, "T_0": 10, "T_mult": 2}
            )
            decay_fraction = scheduler_params.pop("decay_fraction")
            self.scheduler = CosineAnnealingWarmRestarts(
                self.optimizer,
                eta_min=decay_fraction * learning_rate,
                **scheduler_params,
            )
            self.scheduler_type = "cosrestart"
        else:
            raise NotImplementedError

        # Define loss criterion
        self.criterion = CombinedLoss(
            target_str=self.targets,
            criterion=criterion,
            is_intensive=self.model.is_intensive,
            energy_loss_ratio=energy_loss_ratio,
            force_loss_ratio=force_loss_ratio,
            stress_loss_ratio=stress_loss_ratio,
            mag_loss_ratio=mag_loss_ratio,
            **kwargs,
        )
        self.epochs = epochs
        self.starting_epoch = starting_epoch

        # Determine the device to use
        self.device = determine_device(
            use_device=use_device, check_cuda_mem=check_cuda_mem
        )

        self.print_freq = print_freq
        self.save_model_per_epoch = save_model_per_epoch
        self.training_history: dict[
            str, dict[Literal["train", "val", "test"], list[float]]
        ] = {key: {"train": [], "val": [], "test": []} for key in self.targets}
        self.training_history["loss"] = {"train": [], "val": [], "test": []}
        self.best_model = None

    def train(
        self,
        train_loader: DataLoader,
        val_loader: DataLoader,
        test_loader: DataLoader | None = None,
        save_dir: str | None = None,
        save_test_result: bool = False,
        train_composition_model: bool = False,
    ) -> None:
        """Train the model using torch data_loaders.

        Args:
            train_loader (DataLoader): train loader to update CHGNet weights
            val_loader (DataLoader): val loader to test accuracy after each epoch
            test_loader (DataLoader):  test loader to test accuracy at end of training.
                Can be None.
                Default = None
            save_dir (str): the dir name to save the trained weights
                Default = None
            save_test_result (bool): Whether to save the test set prediction in a JSON
                file. Default = False
            train_composition_model (bool): whether to train the composition model
                (AtomRef), this is suggested when the fine-tuning dataset has large
                elemental energy shift from the pretrained CHGNet, which typically comes
                from different DFT pseudo-potentials.
                Default = False
        """
        if self.model is None:
            raise ValueError("Model needs to be initialized")
        global best_checkpoint  # noqa: PLW0603
        if save_dir is None:
            save_dir = f"{datetime.now():%m-%d-%Y}"
        os.makedirs(save_dir, exist_ok=True)

        print(f"Begin Training: using {self.device} device")
        print(f"training targets: {self.targets}")
        self.model.to(self.device)

        # Turn composition model training on / off
        for param in self.model.composition_model.parameters():
            param.requires_grad = train_composition_model

        for epoch in range(self.starting_epoch, self.epochs):
            # train
            train_mae, train_loss = self._train(train_loader, epoch)
            if "e" in train_mae and train_mae["e"] != train_mae["e"]:
                print("Exit due to NaN")
                break

            # val
            val_mae, val_loss = self._validate(val_loader)
            for key in self.targets:
                self.training_history[key]["train"].append(train_mae[key])
                self.training_history[key]["val"].append(val_mae[key])
            self.training_history["loss"]["train"].append(train_loss)
            self.training_history["loss"]["val"].append(val_loss)

            if "e" in val_mae and val_mae["e"] != val_mae["e"]:
                print("Exit due to NaN")
                break

            self.save_checkpoint(epoch, val_mae, val_loss, save_dir=save_dir)

        if test_loader is not None:
            # test best model
            print("---------Evaluate Model on Test Set---------------")
            for file in os.listdir(save_dir):
                if file.startswith("bestE_"):
                    test_file = file
                    best_checkpoint = torch.load(os.path.join(save_dir, test_file))

            self.model.load_state_dict(best_checkpoint["model"]["state_dict"])
            if save_test_result:
                test_mae = self._validate(
                    test_loader, is_test=True, test_result_save_path=save_dir
                )
            else:
                test_mae = self._validate(
                    test_loader, is_test=True, test_result_save_path=None
                )

            for key in self.targets:
                self.training_history[key]["test"] = test_mae[key]
            self.save(filename=os.path.join(save_dir, test_file))

    def _train(self, train_loader: DataLoader, current_epoch: int) -> dict:
        """Train all data for one epoch.

        Args:
            train_loader (DataLoader): train loader to update CHGNet weights
            current_epoch (int): used for resume unfinished training

        Returns:
            dictionary of training errors
        """
        batch_time = AverageMeter()
        data_time = AverageMeter()
        losses = AverageMeter()
        mae_errors = {}
        for target in self.targets:
            mae_errors[target] = AverageMeter()

        # switch to train mode
        self.model.train()

        start = time.perf_counter()  # start timer
        for idx, (graphs, targets) in enumerate(train_loader):
            # measure data loading time
            data_time.update(time.perf_counter() - start)

            # get input
            for g in graphs:
                requires_force = "f" in self.targets
                g.atom_frac_coord.requires_grad = requires_force
            graphs = [g.to(self.device) for g in graphs]
            targets = {k: self.move_to(v, self.device) for k, v in targets.items()}

            # compute output
            prediction = self.model(graphs, task=self.targets)
            combined_loss = self.criterion(targets, prediction)

            losses.update(combined_loss["loss"].data.cpu().item(), len(graphs))
            for key in self.targets:
                mae_errors[key].update(
                    combined_loss[f"{key}_MAE"].cpu().item(),
                    combined_loss[f"{key}_MAE_size"],
                )

            # compute gradient and do SGD step
            self.optimizer.zero_grad()
            combined_loss["loss"].backward()
            self.optimizer.step()

            # adjust learning rate every 1/10 of the epoch
            if idx + 1 in np.arange(1, 11) * len(train_loader) // 10:
                self.scheduler.step()

            # free memory
            del graphs, targets
            del prediction, combined_loss

            # measure elapsed time
            batch_time.update(time.perf_counter() - start)
            start = time.perf_counter()

            if idx == 0 or (idx + 1) % self.print_freq == 0:
                message = (
                    f"Epoch: [{current_epoch}][{idx + 1}/{len(train_loader)}] | "
                    f"Time ({batch_time.avg:.3f})({data_time.avg:.3f}) | "
                    f"Loss {losses.val:.4f}({losses.avg:.4f}) | MAE "
                )
                for key in self.targets:
                    message += (
                        f"{key} {mae_errors[key].val:.3f}({mae_errors[key].avg:.3f})  "
                    )
                print(message)
        return {key: round(err.avg, 6) for key, err in mae_errors.items()}, round(losses.avg, 6)

    def _validate(
        self,
        val_loader: DataLoader,
        is_test: bool = False,
        test_result_save_path: str | None = None,
    ) -> dict:
        """Validation or test step.

        Args:
            val_loader (DataLoader): val loader to test accuracy after each epoch
            is_test (bool): whether it's test step
            test_result_save_path (str): path to save test_result

        Returns:
            dictionary of training errors
        """
        batch_time = AverageMeter()
        losses = AverageMeter()
        mae_errors = {}
        for key in self.targets:
            mae_errors[key] = AverageMeter()

        # switch to evaluate mode
        self.model.eval()

        if is_test:
            test_pred = []

        end = time.perf_counter()
        for ii, (graphs, targets) in enumerate(val_loader):
            if "f" in self.targets or "s" in self.targets:
                for graph in graphs:
                    requires_force = "f" in self.targets
                    graph.atom_frac_coord.requires_grad = requires_force
                graphs = [g.to(self.device) for g in graphs]
                targets = {k: self.move_to(v, self.device) for k, v in targets.items()}
            else:
                with torch.no_grad():
                    graphs = [g.to(self.device) for g in graphs]
                    targets = {
                        k: self.move_to(v, self.device) for k, v in targets.items()
                    }

            # compute output
            prediction = self.model(graphs, task=self.targets)
            combined_loss = self.criterion(targets, prediction)

            losses.update(combined_loss["loss"].data.cpu().item(), len(graphs))
            for key in self.targets:
                mae_errors[key].update(
                    combined_loss[f"{key}_MAE"].cpu().item(),
                    combined_loss[f"{key}_MAE_size"],
                )
            if is_test and test_result_save_path:
                for jj, graph_i in enumerate(graphs):
                    tmp = {
                        "mp_id": graph_i.mp_id,
                        "graph_id": graph_i.graph_id,
                        "energy": {
                            "ground_truth": targets["e"][jj].cpu().detach().tolist(),
                            "prediction": prediction["e"][jj].cpu().detach().tolist(),
                        },
                    }
                    if "f" in self.targets:
                        tmp["force"] = {
                            "ground_truth": targets["f"][jj].cpu().detach().tolist(),
                            "prediction": prediction["f"][jj].cpu().detach().tolist(),
                        }
                    if "s" in self.targets:
                        tmp["stress"] = {
                            "ground_truth": targets["s"][jj].cpu().detach().tolist(),
                            "prediction": prediction["s"][jj].cpu().detach().tolist(),
                        }
                    if "m" in self.targets:
                        if targets["m"][jj] is not None:
                            m_ground_truth = targets["m"][jj].cpu().detach().tolist()
                        else:
                            m_ground_truth = None
                        tmp["mag"] = {
                            "ground_truth": m_ground_truth,
                            "prediction": prediction["m"][jj].cpu().detach().tolist(),
                        }
                    test_pred.append(tmp)

            # free memory
            del graphs, targets
            del prediction, combined_loss

            # measure elapsed time
            batch_time.update(time.perf_counter() - end)
            end = time.perf_counter()

            if (ii + 1) % self.print_freq == 0:
                name = "Test" if is_test else "Val"
                message = (
                    f"{name}: [{ii + 1}/{len(val_loader)}] | "
                    f"Time ({batch_time.avg:.3f}) | "
                    f"Loss {losses.val:.4f}({losses.avg:.4f}) | MAE "
                )
                for key in self.targets:
                    message += (
                        f"{key} {mae_errors[key].val:.3f}({mae_errors[key].avg:.3f})  "
                    )
                print(message)

        if is_test:
            message = "**  "
            if test_result_save_path:
                write_json(
                    test_pred, os.path.join(test_result_save_path, "test_result.json")
                )
        else:
            message = "*   "
        for key in self.targets:
            message += f"{key}_MAE ({mae_errors[key].avg:.3f}) \t"
        print(message)
        return {k: round(mae_error.avg, 6) for k, mae_error in mae_errors.items()}, round(losses.avg, 6)

    def get_best_model(self) -> CHGNet:
        """Get best model recorded in the trainer."""
        if self.best_model is None:
            raise RuntimeError("the model needs to be trained first")
        MAE = min(self.training_history["e"]["val"])
        print(f"Best model has val {MAE =:.4}")
        return self.best_model

    @property
    def _init_keys(self) -> list[str]:
        return [
            key
            for key in list(inspect.signature(Trainer.__init__).parameters)
            if key not in (["self", "model", "kwargs"])
        ]

    def save(self, filename: str = "training_result.pth.tar") -> None:
        """Save the model, graph_converter, etc."""
        state = {
            "model": self.model.as_dict(),
            "optimizer": self.optimizer.state_dict(),
            "scheduler": self.scheduler.state_dict(),
            "training_history": self.training_history,
            "trainer_args": self.trainer_args,
        }
        torch.save(state, filename)

    def save_checkpoint(
        self, epoch: int, mae_error: dict, val_loss: float, save_dir: str | None = None
    ) -> None:
        """Function to save CHGNet trained weights after each epoch.

        Args:
            epoch (int): the epoch number
            mae_error (dict): dictionary that stores the MAEs
            save_dir (str): the directory to save trained weights
        """
        for fname in os.listdir(save_dir):
            if fname.startswith("epoch"):
                fname_epoch = int(fname.split('_')[0][5:]) + 1
                if fname_epoch % self.save_model_per_epoch != 0:
                    os.remove(os.path.join(save_dir, fname))

        err_str = "_".join(
            f"{key}{f'{mae_error[key] * 1000:.0f}' if key in mae_error else 'NA'}"
            for key in "efsm"
        )
        filename = os.path.join(save_dir, f"epoch{epoch}_{err_str}.pth.tar")
        self.save(filename=filename)

        # save the model if it has minimal val energy error or val force error
        if mae_error["e"] == min(self.training_history["e"]["val"]):
            self.best_model = self.model
            for fname in os.listdir(save_dir):
                if fname.startswith("bestE"):
                    os.remove(os.path.join(save_dir, fname))
            shutil.copyfile(
                filename,
                os.path.join(save_dir, f"bestE_epoch{epoch}_{err_str}.pth.tar"),
            )
        if mae_error["f"] == min(self.training_history["f"]["val"]):
            for fname in os.listdir(save_dir):
                if fname.startswith("bestF"):
                    os.remove(os.path.join(save_dir, fname))
            shutil.copyfile(
                filename,
                os.path.join(save_dir, f"bestF_epoch{epoch}_{err_str}.pth.tar"),
            )
        if val_loss == min(self.training_history["loss"]["val"]):
            for fname in os.listdir(save_dir):
                if fname.startswith("bestL"):
                    os.remove(os.path.join(save_dir, fname))
            shutil.copyfile(
                filename,
                os.path.join(save_dir, f"bestL_epoch{epoch}_{err_str}.pth.tar"),
            )

    @classmethod
    def load(cls, path: str) -> Trainer:
        """Load trainer state_dict."""
        state = torch.load(path, map_location=torch.device("cpu"))
        model = CHGNet.from_dict(state["model"])
        print(f"Loaded model params = {sum(p.numel() for p in model.parameters()):,}")
        # drop model from trainer_args if present
        state["trainer_args"].pop("model", None)
        trainer = Trainer(model=model, **state["trainer_args"])
        trainer.model.to(trainer.device)
        trainer.optimizer.load_state_dict(state["optimizer"])
        trainer.scheduler.load_state_dict(state["scheduler"])
        trainer.training_history = state["training_history"]
        trainer.starting_epoch = len(trainer.training_history["e"]["train"])
        return trainer

    @staticmethod
    def move_to(obj, device) -> Tensor | list[Tensor]:
        """Move object to device."""
        if torch.is_tensor(obj):
            return obj.to(device)
        if isinstance(obj, list):
            out = []
            for tensor in obj:
                if tensor is not None:
                    out.append(tensor.to(device))
                else:
                    out.append(None)
            return out
        raise TypeError("Invalid type for move_to")


class CombinedLoss(nn.Module):
    """A combined loss function of energy, force, stress and magmom."""

    def __init__(
        self,
        target_str: str = "ef",
        criterion: str = "MSE",
        is_intensive: bool = True,
        energy_loss_ratio: float = 1,
        force_loss_ratio: float = 1,
        stress_loss_ratio: float = 0.1,
        mag_loss_ratio: float = 0.1,
        delta: float = 0.1,
    ) -> None:
        """Initialize the combined loss.

        Args:
            target_str: the training target label. Can be "e", "ef", "efs", "efsm" etc.
                Default = "ef"
            criterion: loss criterion to use
                Default = "MSE"
            is_intensive (bool): whether the energy label is intensive
                Default = True
            energy_loss_ratio (float): energy loss ratio in loss function
                Default = 1
            force_loss_ratio (float): force loss ratio in loss function
                Default = 1
            stress_loss_ratio (float): stress loss ratio in loss function
                Default = 0.1
            mag_loss_ratio (float): magmom loss ratio in loss function
                Default = 0.1
            delta (float): delta for torch.nn.HuberLoss. Default = 0.1
        """
        super().__init__()
        # Define loss criterion
        if criterion in ["MSE", "mse"]:
            self.criterion = nn.MSELoss()
        elif criterion in ["MAE", "mae", "l1"]:
            self.criterion = nn.L1Loss()
        elif criterion == "Huber":
            self.criterion = nn.HuberLoss(delta=delta)
        else:
            raise NotImplementedError
        self.target_str = target_str
        self.is_intensive = is_intensive
        self.energy_loss_ratio = energy_loss_ratio
        if "f" not in self.target_str:
            self.force_loss_ratio = 0
        else:
            self.force_loss_ratio = force_loss_ratio
        if "s" not in self.target_str:
            self.stress_loss_ratio = 0
        else:
            self.stress_loss_ratio = stress_loss_ratio
        if "m" not in self.target_str:
            self.mag_loss_ratio = 0
        else:
            self.mag_loss_ratio = mag_loss_ratio

    def forward(
        self,
        targets: dict[str, Tensor],
        prediction: dict[str, Tensor],
    ) -> dict[str, Tensor]:
        """Compute the combined loss using CHGNet prediction and labels
        this function can automatically mask out magmom loss contribution of
        data points without magmom labels.

        Args:
            targets (dict): DFT labels
            prediction (dict): CHGNet prediction

        Returns:
            dictionary of all the loss, MAE and MAE_size
        """
        out = {"loss": 0.0}
        # Energy
        if "e" in targets:
            if self.is_intensive:
                out["loss"] += self.energy_loss_ratio * self.criterion(
                    targets["e"], prediction["e"]
                )
                out["e_MAE"] = mae(targets["e"], prediction["e"])
                out["e_MAE_size"] = prediction["e"].shape[0]
            else:
                e_per_atom_target = targets["e"] / prediction["atoms_per_graph"]
                e_per_atom_pred = prediction["e"] / prediction["atoms_per_graph"]
                out["loss"] += self.energy_loss_ratio * self.criterion(
                    e_per_atom_target, e_per_atom_pred
                )
                out["e_MAE"] = mae(e_per_atom_target, e_per_atom_pred)
                out["e_MAE_size"] = prediction["e"].shape[0]

        # Force
        if "f" in targets:
            forces_pred = torch.cat(prediction["f"], dim=0)
            forces_target = torch.cat(targets["f"], dim=0)
            out["loss"] += self.force_loss_ratio * self.criterion(
                forces_target, forces_pred
            )
            out["f_MAE"] = mae(forces_target, forces_pred)
            out["f_MAE_size"] = forces_target.shape[0]

        # Stress
        if "s" in targets:
            stress_pred = torch.cat(prediction["s"], dim=0)
            stress_target = torch.cat(targets["s"], dim=0)
            out["loss"] += self.stress_loss_ratio * self.criterion(
                stress_target, stress_pred
            )
            out["s_MAE"] = mae(stress_target, stress_pred)
            out["s_MAE_size"] = stress_target.shape[0]

        # Mag
        if "m" in targets:
            mag_preds, mag_targets = [], []
            m_mae_size = 0
            for mag_pred, mag_target in zip(prediction["m"], targets["m"]):
                # exclude structures without magmom labels
                if mag_target is not None:
                    mag_preds.append(mag_pred)
                    mag_targets.append(mag_target)
                    m_mae_size += mag_target.shape[0]
            if mag_targets != []:
                mag_preds = torch.cat(mag_preds, dim=0)
                mag_targets = torch.cat(mag_targets, dim=0)
                out["loss"] += self.mag_loss_ratio * self.criterion(
                    mag_targets, mag_preds
                )
                out["m_MAE"] = mae(mag_targets, mag_preds)
                out["m_MAE_size"] = m_mae_size
            else:
                out["m_MAE"] = torch.zeros([1])
                out["m_MAE_size"] = m_mae_size

        return out
