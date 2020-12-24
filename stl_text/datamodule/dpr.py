import os
import random
from collections import defaultdict
from typing import Optional

import torch
import datasets as ds
from pytorch_lightning import LightningDataModule
from stl_text.ops.tokenizers import WhitespaceTokenizer
from stl_text.ops.transforms import LabelTransform
from torch.nn.utils.rnn import pad_sequence
from stl_text.ops.samplers import PoolBatchSampler


class DPRDataModule(LightningDataModule):
    def __init__(self, data_path: str, 
                vocab_path: Optional[str] = None, 
                batch_size: int = 32,
                drop_last: bool = False,
                num_proc_in_map: int = 1, 
                distributed: bool = False, 
                load_from_cache_file: bool = True):
        super().__init__()
        self.data_path = data_path
        self.vocab_path = vocab_path
        self.batch_size = batch_size
        self.drop_last = drop_last
        self.num_proc_in_map = num_proc_in_map
        self.distributed = distributed
        self.load_from_cache_file = load_from_cache_file

        self.text_transform = None
        self.datasets = {}

    def setup(self, stage):
        self.text_transform = WhitespaceTokenizer(vocab_path=self.vocab_path)

        for split in ("train", "valid", "test"):
            dataset_split = ds.Dataset.load_from_disk(os.path.join(self.data_path, split))  # raw dataset
            dataset_split = dataset_split.map(function=lambda x: {'query_ids': self.text_transform(x)},
                                                            input_columns='question', num_proc=self.num_proc_in_map,
                                                            load_from_cache_file=self.load_from_cache_file)
            dataset_split = dataset_split.map(function=lambda pos_ctxs, neg_ctxs: 
                                                                {
                                                                    'contexts_ids': [self.text_transform(x) for x in pos_ctxs] + [self.text_transform(x) for x in neg_ctxs],
                                                                    'contexts_positive': [1] * len(pos_ctxs) + [0] * len(neg_ctxs) ,
                                                                },
                                                            input_columns=['positive_ctxs','negative_ctxs'], num_proc=self.num_proc_in_map,
                                                            load_from_cache_file=self.load_from_cache_file)
            dataset_split = dataset_split.map(function=lambda x: {'query_seq_len': len(x)},
                                                            input_columns='query_ids', num_proc=self.num_proc_in_map,
                                                            load_from_cache_file=self.load_from_cache_file)
            dataset_split = dataset_split.map(function=lambda x: {'contexts_cnt': len(x)},
                                                            input_columns='contexts_ids', num_proc=self.num_proc_in_map,
                                                            load_from_cache_file=self.load_from_cache_file)
            dataset_split = dataset_split.map(function=lambda x: {'contexts_seq_lens': [len(c) for c in x]},
                                                            input_columns='contexts_ids', num_proc=self.num_proc_in_map,
                                                            load_from_cache_file=self.load_from_cache_file)
            dataset_split.set_format(type='torch', columns=['query_ids', 'query_seq_len', 
                                                            'contexts_ids', 'contexts_cnt', 'contexts_seq_lens'
                                                            'contexts_positive'])
            
            self.datasets[split] = curr_dataset

    def forward(self, text):
        return self.text_transform(text)

    def train_dataloader(self):
        # sample data into `num_batches_in_page` sized pool. In each pool, sort examples by sequence length, batch them
        # with `batch_size` and shuffle batches
        train_dataset = self.datasets["train"]
        batch_sampler = PoolBatchSampler(train_dataset, batch_size=self.batch_size,
                                         drop_last=self.drop_last, key=lambda row: row["query_seq_len"])
        return torch.utils.data.DataLoader(train_dataset, batch_sampler=batch_sampler,
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

    def collate(self, batch):
        return self._collate(batch, pad_columns=('query_ids',
                                                 'contexts_ids'
                                                 'contexts_positive'))

    # generic collate(), same as DocClassificationDataModule
    def _collate(self, batch, pad_columns=("token_ids",)):
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
