import os
import random
from collections import defaultdict
from typing import Optional, List

import torch
from torch import Tensor
import datasets as ds
from pytorch_lightning import LightningDataModule
from stl_text.ops.tokenizers import WhitespaceTokenizer
from stl_text.ops.transforms import LabelTransform
from torch.nn.utils.rnn import pad_sequence
from stl_text.ops.samplers import PoolBatchSampler


def assert_padded_tensor3d_equal_to_list3d(list_val, tensor_val):
    """
        Asserts if all values in a list match the tensor values. 
    """
    for i, seq1 in enumerate(list_val):
        for j, seq2 in enumerate(seq1):
            seq = seq2
            if isinstance(seq, torch.Tensor):
                seq = seq.tolist()

            for k, val in enumerate(seq):
                assert val == tensor_val[i,j,k].data.item()
                

class DPRRetrieverDataModule(LightningDataModule):
    """
        This reads a jsonl file with json objects from the original DPR data obtained from https://github.com/facebookresearch/DPR/blob/master/data/download_data.py.
    """
    def __init__(self, data_path: str, 
                vocab_path: Optional[str] = None, 
                batch_size: int = 32,
                max_positive: int = 1, # currently, like the original paper only 1 is supported
                max_negative: int = 7,
                ctxs_random_sample: bool = True, 
                limit_eval: bool = False, # Limits pos_neg with test
                drop_last: bool = False,
                num_proc_in_map: int = 1, 
                distributed: bool = False, 
                load_from_cache_file: bool = True,
                vocab_trainable:bool = False):
        super().__init__()
        self.data_path = data_path
        self.vocab_path = vocab_path
        self.vocab_trainable = vocab_trainable
        self.batch_size = batch_size
        self.drop_last = drop_last
        self.num_proc_in_map = num_proc_in_map
        self.distributed = distributed
        self.load_from_cache_file = load_from_cache_file

        if max_positive>1:
            raise ValueError("Only 1 positive example is supported. Update the loss accordingly to support more than 1!")
        
        self.max_positive = max_positive
        self.max_negative = max_negative
        self.ctxs_random_sample = ctxs_random_sample
        self.limit_eval = limit_eval

        self.text_transform = None
        self.datasets = {}


    def setup(self, stage):
        self.text_transform = WhitespaceTokenizer(vocab_path=self.vocab_path, trainable=self.vocab_trainable)

        for split in ("train", "valid", "test"):
            dataset_split = ds.Dataset.load_from_disk(os.path.join(self.data_path, split))  # raw dataset
            dataset_split = dataset_split.map(function=lambda x: {'query_ids': self.text_transform(x)},
                                                            input_columns='question', num_proc=self.num_proc_in_map,
                                                            load_from_cache_file=self.load_from_cache_file)
            dataset_split = dataset_split.map(function=lambda ctxs: {'contexts_pos_ids': [self.text_transform(x["text"]) for x in ctxs]},
                                                            input_columns='positive_ctxs', num_proc=self.num_proc_in_map,
                                                            load_from_cache_file=self.load_from_cache_file)
            dataset_split = dataset_split.map(function=lambda ctxs: {'contexts_neg_ids': [self.text_transform(x["text"]) for x in ctxs]},
                                                input_columns='negative_ctxs', num_proc=self.num_proc_in_map,
                                                load_from_cache_file=self.load_from_cache_file)
            dataset_split = dataset_split.map(function=lambda x: {'query_seq_len': len(x)},
                                                            input_columns='query_ids', num_proc=self.num_proc_in_map,
                                                            load_from_cache_file=self.load_from_cache_file)
            dataset_split.set_format(type='torch', columns=['query_ids', 'query_seq_len', 
                                                            'contexts_pos_ids', 'contexts_neg_ids'])
            
            self.datasets[split] = dataset_split

    def forward(self, text):
        return self.text_transform(text)

    def train_dataloader(self):
        # sample data into `num_batches_in_page` sized pool. In each pool, sort examples by sequence length, batch them
        # with `batch_size` and shuffle batches
        train_dataset = self.datasets["train"]
        batch_sampler = PoolBatchSampler(train_dataset, batch_size=self.batch_size,
                                         drop_last=self.drop_last, 
                                         #key=lambda r: r["query_seq_len"]
                                         )
        return torch.utils.data.DataLoader(train_dataset, batch_sampler=batch_sampler,
                                           num_workers=1,
                                           collate_fn=self.collate_train)

    def valid_dataloader(self):
        return torch.utils.data.DataLoader(self.datasets["valid"], shuffle=True, batch_size=self.batch_size,
                                           num_workers=1,
                                           collate_fn=self.collate_eval)

    def test_dataloader(self):
        return torch.utils.data.DataLoader(self.datasets["test"], shuffle=False, batch_size=self.batch_size,
                                           num_workers=1,
                                           collate_fn=self.collate_eval)

    def collate_eval(self, batch):
        return self.collate(batch, False)

    def collate_train(self, batch):
        return self.collate(batch, True)

    def collate(self, batch, is_train):
        """
            Combines pos and neg contexts. Samples randomly limited number of pos/neg contexts if is_train is True.
        """
        for row in batch:
            # sample positive contexts
            contexts_pos_ids = row["contexts_pos_ids"]
            if (is_train or self.limit_eval) and self.max_positive > 0:
                if is_train and self.ctxs_random_sample:
                    contexts_pos_ids = random.sample(contexts_pos_ids, min(len(contexts_pos_ids),self.max_positive))
                else:   
                    contexts_pos_ids = contexts_pos_ids[:self.max_positive]
            
            # sample negative contexts
            contexts_neg_ids = row["contexts_neg_ids"]
            if (is_train or self.limit_eval) and self.max_negative > 0:
                if is_train and self.ctxs_random_sample:
                    contexts_neg_ids = random.sample(contexts_neg_ids, self.max_negative) 
                else:
                    contexts_neg_ids = contexts_neg_ids[:self.max_negative]
            
            row["contexts_ids"] = contexts_pos_ids + contexts_neg_ids
            row["contexts_is_pos"] = torch.Tensor([1] * len(contexts_pos_ids) + [0] * len(contexts_neg_ids))

            row.pop("contexts_pos_ids")
            row.pop("contexts_neg_ids")

        return self._collate(batch, pad_columns=('query_ids',
                                                 'contexts_is_pos'),
                                    pad_columns_2d=('contexts_ids',)
                                    )

    def pad_sequence_2d(self, sequences:List[List[Tensor]], 
                        batch_first:bool=False, 
                        padding_value=0.0):
        # type: (List[List[Tensor]], bool, float) -> Tensor
        r"""Pad a list of variable length Tensors with ``padding_value``

        ``pad_sequence`` stacks a list of Tensors along a new dimension,
        and pads them to equal length. For example, if the input is list of
        sequences with size ``L x *`` and if batch_first is False, and ``T x B x *``
        otherwise.

        `B` is batch size. It is equal to the number of elements in ``sequences``.
        `T` is length of the longest sequence.
        `L` is length of the sequence.
        `*` is any number of trailing dimensions, including none.

        Example:
            >>> from torch.nn.utils.rnn import pad_sequence
            >>> a = torch.ones(25, 300)
            >>> b = torch.ones(22, 300)
            >>> c = torch.ones(15, 300)
            >>> pad_sequence([a, b, c]).size()
            torch.Size([25, 3, 300])

        Note:
            This function returns a Tensor of size ``T x B x *`` or ``B x T x *``
            where `T` is the length of the longest sequence. This function assumes
            trailing dimensions and type of all the Tensors in sequences are same.

        Arguments:
            sequences (list[Tensor]): list of variable length sequences.
            batch_first (bool, optional): output will be in ``B x T x *`` if True, or in
                ``T x B x *`` otherwise
            padding_value (float, optional): value for padded elements. Default: 0.

        Returns:
            Tensor of size ``T x B x *`` if :attr:`batch_first` is ``False``.
            Tensor of size ``B x T x *`` otherwise
        """

        # assuming trailing dimensions and type of all the Tensors
        # in sequences are same and fetching those from sequences[0]
        
        max_len = max([len(s) for s in sequences])
        max_dim = max([max([s2.size(0) for s2 in s1]) for s1 in sequences])
        batch_size = len(sequences)
        if batch_first:
            out_dims = (batch_size, max_len, max_dim)
        else:
            out_dims = (max_len, batch_size, max_dim)

        out_tensor = sequences[0][0].new_full(out_dims, padding_value)
        for i, tensor_list in enumerate(sequences):
            # use index notation to prevent duplicate references to the tensor
            for i2, tensor in enumerate(tensor_list):
                length = tensor.size(0)
                # batch first
                out_tensor[i, i2, :length] = tensor
                
        return out_tensor

    def _collate(self, batch, pad_columns, pad_columns_2d):
        columnar_data = defaultdict(list)
        for row in batch:
            for column, value in row.items():
                columnar_data[column].append(value)

        padded = {}
        for column, v in columnar_data.items():
            if pad_columns_2d and column in pad_columns_2d:
                padded[column] = self.pad_sequence_2d(v, batch_first=True)
            elif pad_columns and column in pad_columns:
                padded[column] = pad_sequence(v, batch_first=True)
            else:
                padded[column] = torch.tensor(v, dtype=torch.long)
        return padded
