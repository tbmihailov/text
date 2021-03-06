import os
from collections import defaultdict
from typing import Optional

import torch
import datasets as ds
from pytorch_lightning import LightningDataModule
from stl_text.ops.tokenizers import WhitespaceTokenizer
from stl_text.ops.transforms import LabelTransform
from torch.nn.utils.rnn import pad_sequence
from stl_text.ops.samplers import PoolBatchSampler


class DocClassificationDataModule(LightningDataModule):
    def __init__(self, data_path: str = 'glue_sst2_tiny', vocab_path: Optional[str] = None, batch_size: int = 32,
                 drop_last: bool = False,
                 num_proc_in_map: int = 1, distributed: bool = False, load_from_cache_file: bool = True):
        super().__init__()
        self.data_path = data_path
        self.vocab_path = vocab_path
        self.batch_size = batch_size
        self.drop_last = drop_last
        self.num_proc_in_map = num_proc_in_map
        self.distributed = distributed
        self.load_from_cache_file = load_from_cache_file

        self.text_transform = None
        self.label_transform = None
        self.datasets = {}

    def setup(self, stage):
        # torchtext's spm_tokenizer and vocab are not pickleable and will fail dataset.map()
        # tokenizer = spm_tokenizer(download_from_url(PRETRAINED_SP_MODEL["text_unigram_25000"]))
        # vocab = build_fairseq_vocab("vocab_tiny.txt")

        # sequential_transforms is not torchscriptable
        # self.text_transform = sequential_transforms(tokenizer, vocab)

        # use dummy text_transform and label_transform to get rid of pickling and torchscript issues
        self.text_transform = WhitespaceTokenizer(vocab_path=self.vocab_path)
        self.label_transform = LabelTransform(["0", "1"])

        for split in ("train", "valid", "test"):
            self.datasets[split] = ds.Dataset.load_from_disk(os.path.join(self.data_path, split))  # raw dataset
            self.datasets[split] = self.datasets[split].map(function=lambda x: {'label_id': self.label_transform(x)},
                                                            input_columns='label', num_proc=self.num_proc_in_map,
                                                            load_from_cache_file=self.load_from_cache_file)
            self.datasets[split] = self.datasets[split].map(function=lambda x: {'token_ids': self.text_transform(x)},
                                                            input_columns='text', num_proc=self.num_proc_in_map,
                                                            load_from_cache_file=self.load_from_cache_file)
            self.datasets[split] = self.datasets[split].map(function=lambda x: {'seq_len': len(x)},
                                                            input_columns='token_ids', num_proc=self.num_proc_in_map,
                                                            load_from_cache_file=self.load_from_cache_file)
            self.datasets[split].set_format(type='torch', columns=['label_id', 'token_ids', 'seq_len'])

    def forward(self, text):
        return self.text_transform(text)

    def train_dataloader(self):
        # sample data into `num_batches_in_page` sized pool. In each pool, sort examples by sequence length, batch them
        # with `batch_size` and shuffle batches
        batch_sampler = PoolBatchSampler(self.datasets["train"], batch_size=self.batch_size,
                                         drop_last=self.drop_last, key=lambda row: row["seq_len"])
        return torch.utils.data.DataLoader(self.datasets["train"], batch_sampler=batch_sampler,
                                           num_workers=1,
                                           collate_fn=self.collate)

    def valid_dataloader(self):
        return torch.utils.data.DataLoader(self.self.datasets["valid"], shuffle=True, batch_size=self.batch_size,
                                           num_workers=1,
                                           collate_fn=self.collate)

    def test_dataloader(self):
        return torch.utils.data.DataLoader(self.datasets["test"], shuffle=False, batch_size=self.batch_size,
                                           num_workers=1,
                                           collate_fn=self.collate)

    def collate(self, batch, pad_columns=("token_ids",)):
        columnar_data = defaultdict(list)
        for row in batch:
            for column, value in row.items():
                columnar_data[column].append(value)

        padded = {}
        for column, v in columnar_data.items():
            if pad_columns and column in pad_columns:
                padded[column] = pad_sequence(v, batch_first=True)
            else:
                padded[column] = torch.tensor(v, dtype=torch.long)
        return padded


class ConcatPairDocClassificationDataModule(DocClassificationDataModule):
    def __init__(self, separator_id: Optional[id] = 0, *args, **kwargs):
        self.separator_id = separator_id
        super().__init__(*args, **kwargs)

    def setup(self, stage):
        self.text_transform = WhitespaceTokenizer()
        self.label_transform = LabelTransform(["0", "1"])
        separator = [self.separator_id] if self.separator_id is not None else []

        for split in ("train", "valid", "test"):
            self.datasets[split] = ds.Dataset.load_from_disk(os.path.join(self.data_path, split))
            self.datasets[split] = self.datasets[split].map(
                function=lambda x: {'label_id': self.label_transform(x)},
                input_columns='label',
                num_proc=self.num_proc_in_map,
                load_from_cache_file=self.load_from_cache_file,
            )
            self.datasets[split] = self.datasets[split].map(
                function=lambda x, y: { 'token_ids': self.text_transform(x) + separator + self.text_transform(y)},
                input_columns=('text1', 'text2'),
                num_proc=self.num_proc_in_map,
                load_from_cache_file=self.load_from_cache_file,
            )
            self.datasets[split] = self.datasets[split].map(
                function=lambda x: {'seq_len': len(x)},
                input_columns='token_ids',
                num_proc=self.num_proc_in_map,
                load_from_cache_file=self.load_from_cache_file,
            )
            self.datasets[split].set_format(type='torch', columns=['label_id', 'token_ids', 'seq_len'])
