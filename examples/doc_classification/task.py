from typing import List, Tuple

import torch
import torch.nn as nn

from stl_text.datamodule import DocClassificationDataModule
from pytorch_lightning import metrics
from torch.optim import Optimizer
from torch import Tensor
from torch.nn.utils.rnn import pad_sequence

from pytorch_lightning import LightningModule


class DocClassificationTask(LightningModule):

    def __init__(
            self,
            datamodule: DocClassificationDataModule,
            model: nn.Module,
            optimizer: Optimizer,
    ):
        super().__init__()
        self.text_transform = datamodule.text_transform
        self.label_transform = datamodule.label_transform
        self.model = model
        self.optimizer = optimizer
        self.loss = torch.nn.CrossEntropyLoss()
        self.valid_acc = metrics.Accuracy()
        self.test_acc = metrics.Accuracy()

    def forward(self, text_batch: List[str]) -> List[str]:
        token_ids: List[Tensor] = [torch.tensor(self.text_transform(text), dtype=torch.long) for text in text_batch]
        model_inputs: Tensor = pad_sequence(token_ids, batch_first=True)
        logits = self.model(model_inputs)
        prediction_idx = torch.max(logits, dim=1)[1]
        prediction_labels = [self.label_transform.decode(idx) for idx in prediction_idx]
        return prediction_labels

    def configure_optimizers(self):
        return self.optimizer

    def training_step(self, batch, batch_idx):
        logits = self.model(batch["token_ids"])
        loss = self.loss(logits, batch["label_id"])
        self.log("train_loss", loss, prog_bar=True, on_epoch=True, sync_dist=True)
        return loss

    def validation_step(self, batch, batch_idx):
        logits = self.model(batch["token_ids"])
        loss = self.loss(logits, batch["label_id"])
        self.valid_acc(logits, batch["label_id"])
        self.log("val_loss", loss, on_epoch=True, sync_dist=True)
        self.log("checkpoint_on", loss, on_epoch=True, sync_dist=True)
        self.log("valid_acc", self.valid_acc, on_epoch=True, sync_dist=True)

    def test_step(self, batch, batch_idx):
        logits = self.model(batch["token_ids"])
        loss = self.loss(logits, batch["label_id"])
        self.test_acc(logits, batch["label_id"])
        self.log("test_loss", loss, on_epoch=True, sync_dist=True)
        self.log("test_acc", self.test_acc, on_epoch=True, sync_dist=True)
