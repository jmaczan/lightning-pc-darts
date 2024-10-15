import os.path
import torch
import torch.nn as nn
import torch.nn.functional as F
import pytorch_lightning as pl
from pytorch_lightning.callbacks import RichProgressBar
from pytorch_lightning.loggers import TensorBoardLogger
from torchvision import datasets, transforms
from torch.utils.data import DataLoader, random_split
import yaml


# TODO: for now it's not partially-connected yet
class PCDARTSSearchSpace(nn.Module):
    def __init__(self, num_nodes, num_ops, in_channels):
        super(PCDARTSSearchSpace, self).__init__()
        self.num_nodes = num_nodes
        self.num_ops = num_ops
        self.in_channels = in_channels

        # initial architecture params (alpha)
        # they are learnable
        # they represent importance of each operation for each connection
        self.arch_parameters = nn.ParameterList(
            [
                nn.Parameter(torch.randn(i + 2, self.num_ops))
                for i in range(self.num_nodes)
            ]
        )

        # set of possible candidates for operations
        self.candidate_operations = nn.ModuleList(
            [
                nn.Identity(),
                nn.Conv2d(
                    in_channels=in_channels,
                    out_channels=in_channels,
                    kernel_size=3,
                    padding=1,
                    bias=False,
                ),
                nn.Conv2d(
                    in_channels=in_channels,
                    out_channels=in_channels,
                    kernel_size=1,
                    bias=False,
                ),
                nn.MaxPool2d(kernel_size=3, stride=1, padding=1),
                nn.AvgPool2d(kernel_size=3, stride=1, padding=1),
            ]
        )

    # this function is the heart of PC-DARTS, so for anyone reading this, let's break this down
    def forward(self, x):
        states = [x]  # this is the input to the cell
        for node in range(
            self.num_nodes
        ):  # iterates over each intermediate node in the cell
            node_inputs = []  # for each node, we will store inputs here
            for i in range(min(node + 2, len(states))):
                # iterate over all possible input states for current node.
                # +2 because each node can take input from all previous nodes plus two initial inputs
                # which is output of the previous call and output of the previous-previous cell
                op_weights = F.softmax(
                    self.arch_parameters[node][i], dim=-1
                )  # softmax to architectural parameters for current node and input
                # these parametsr tell us about importance of each operation in this particular connection
                for j, op in enumerate(self.candidate_operations):
                    # go through all candidate operations (poolings, convs etc.)
                    node_inputs.append(
                        op_weights[j] * op(states[i])
                    )  # this is the most important part - it applies all operations and weights their outputs;
                    # this is what is called 'continuous relaxation' - applying all operations and weighting them,
                    # instead of selecting a single opration
            states.append(
                sum(node_inputs)
            )  # after processing all inputs and operations for a node, we sum the weighted inputs
            # the sum is output of the current node and we append it to states list
        return states[-1]  # we return the last state, which is the output of the cell


class PCDARTSLightningModule(pl.LightningModule):
    def __init__(self, config):
        super(PCDARTSLightningModule, self).__init__()
        self.save_hyperparameters()
        self.config = config

        self.stem = nn.Sequential(
            nn.Conv2d(
                in_channels=3,
                out_channels=16,
                kernel_size=3,
                stride=1,
                padding=1,
                bias=False,
            ),
            nn.BatchNorm2d(num_features=16),
            nn.ReLU(inplace=True),
            nn.Conv2d(
                in_channels=16,
                out_channels=32,
                kernel_size=3,
                stride=1,
                padding=1,
                bias=False,
            ),
            nn.BatchNorm2d(num_features=32),
            nn.ReLU(inplace=True),
            nn.Conv2d(32, 64, 3, stride=1, padding=1, bias=False),
            nn.BatchNorm2d(64),
            nn.ReLU(inplace=True),
        )

        stem_output_channels = self._get_output_channels(self.stem)

        self.search_space = PCDARTSSearchSpace(
            in_channels=stem_output_channels,
            num_nodes=config["model"]["num_nodes"],
            num_ops=config["model"]["num_ops"],
        )

        self.classifier = nn.Sequential(
            nn.AdaptiveAvgPool2d(output_size=1),
            nn.Flatten(),
            nn.Linear(
                in_features=stem_output_channels,
                out_features=config["model"]["num_classes"],
            ),
        )

    def forward(self, x):
        x = self.stem(x)
        x = self.search_space(x)
        return self.classifier(x)

    def training_step(self, batch, batch_idx):
        x, y = batch
        logits = self(x)
        loss = F.cross_entropy(logits, y)
        self.log("train_loss", loss)
        return loss

    def validation_step(self, batch, batch_idx):
        x, y = batch
        logits = self(x)
        loss = F.cross_entropy(logits, y)
        acc = (logits.argmax(dim=1) == y).float().mean()
        self.log("val_loss", loss)
        self.log("val_acc", acc)

    def configure_optimizers(self):
        optimizer = torch.optim.SGD(
            params=self.parameters(),
            lr=self.config["training"]["learning_rate"],
            momentum=self.config["training"]["momentum"],
            weight_decay=self.config["training"]["weight_decay"],
        )
        scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(
            optimizer=optimizer, T_max=self.config["training"]["max_epochs"]
        )

        return [optimizer], [scheduler]

    def _get_output_channels(self, module):
        if hasattr(module, "num_features"):
            return module.num_features
        elif hasattr(module, "out_channels"):
            return module.out_channels
        elif isinstance(module, nn.Sequential):
            for layer in reversed(module):
                if hasattr(layer, "num_features"):
                    return layer.num_features
                if hasattr(layer, "out_channels"):
                    return layer.out_channels
        else:
            raise ValueError(
                f"Unsupported module type: {type(module)}. Perhaps you want to add it to _get_output_channel() function to compute number of channels correctly"
            )


class CIFAR10DataModule(pl.LightningDataModule):
    def __init__(self, config):
        super().__init__()
        self.config = config
        self.data_dir = config["data"]["data_dir"]
        self.batch_size = config["data"]["batch_size"]
        self.num_workers = config["data"]["num_workers"]

        self.transform = transforms.Compose(
            [
                transforms.RandomCrop(size=32, padding=4),
                transforms.RandomHorizontalFlip(),
                transforms.ToTensor(),
                transforms.Normalize(
                    (0.4914, 0.4822, 0.4465), (0.2023, 0.1994, 0.2010)
                ),  # CIFAR10 - see https://github.com/kuangliu/pytorch-cifar/issues/8
            ]
        )

    def prepare_data(self):
        datasets.CIFAR10(root=self.data_dir, train=True, download=True)
        datasets.CIFAR10(root=self.data_dir, train=False, download=True)

    def setup(self, stage=None):
        if stage == "fit" or stage is None:
            cifar_full = datasets.CIFAR10(
                root=self.data_dir, train=True, transform=self.transform
            )
            self.cifar_train, self.cifar_val = random_split(cifar_full, [45000, 5000])

        if stage == "test" or stage is None:
            self.cifar_test = datasets.CIFAR10(
                root=self.data_dir, train=False, transform=self.transform
            )

    def train_dataloader(self):
        return DataLoader(
            self.cifar_train,
            batch_size=self.batch_size,
            shuffle=True,
            num_workers=self.num_workers,
            persistent_workers=True,
        )

    def val_dataloader(self):
        return DataLoader(
            self.cifar_val,
            batch_size=self.batch_size,
            num_workers=self.num_workers,
            persistent_workers=True,
        )

    def test_dataloader(self):
        return DataLoader(
            self.cifar_test,
            batch_size=self.batch_size,
            num_workers=self.num_workers,
            persistent_workers=True,
        )


def load_config(config_path):
    with open(config_path, "r") as f:
        config = yaml.safe_load(f)
    return config


def main():
    config = load_config(os.path.join("src", "config.yaml"))
    model = PCDARTSLightningModule(config)
    data_module = CIFAR10DataModule(config)

    trainer = pl.Trainer(
        max_epochs=config["training"]["max_epochs"],
        accelerator="gpu" if config["training"]["gpus"] else "auto",
        devices=config["training"]["gpus"] or 0,
        # devices=config["training"]["gpus"] if torch.cuda.is_available() else 0,
        callbacks=[RichProgressBar()],
        logger=TensorBoardLogger(
            config["logging"]["log_dir"], name=config["logging"]["experiment_name"]
        ),
    )

    trainer.fit(model, data_module)


if __name__ == "__main__":
    main()