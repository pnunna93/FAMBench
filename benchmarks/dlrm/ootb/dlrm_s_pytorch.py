# Copyright (c) Facebook, Inc. and its affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.
#
# Description: an implementation of a deep learning recommendation model (DLRM)
# The model input consists of dense and sparse features. The former is a vector
# of floating point values. The latter is a list of sparse indices into
# embedding tables, which consist of vectors of floating point values.
# The selected vectors are passed to mlp networks denoted by triangles,
# in some cases the vectors are interacted through operators (Ops).
#
# output:
#                         vector of values
# model:                        |
#                              /\
#                             /__\
#                               |
#       _____________________> Op  <___________________
#     /                         |                      \
#    /\                        /\                      /\
#   /__\                      /__\           ...      /__\
#    |                          |                       |
#    |                         Op                      Op
#    |                    ____/__\_____           ____/__\____
#    |                   |_Emb_|____|__|    ...  |_Emb_|__|___|
# input:
# [ dense features ]     [sparse indices] , ..., [sparse indices]
#
# More precise definition of model layers:
# 1) fully connected layers of an mlp
# z = f(y)
# y = Wx + b
#
# 2) embedding lookup (for a list of sparse indices p=[p1,...,pk])
# z = Op(e1,...,ek)
# obtain vectors e1=E[:,p1], ..., ek=E[:,pk]
#
# 3) Operator Op can be one of the following
# Sum(e1,...,ek) = e1 + ... + ek
# Dot(e1,...,ek) = [e1'e1, ..., e1'ek, ..., ek'e1, ..., ek'ek]
# Cat(e1,...,ek) = [e1', ..., ek']'
# where ' denotes transpose operation
#
# References:
# [1] Maxim Naumov, Dheevatsa Mudigere, Hao-Jun Michael Shi, Jianyu Huang,
# Narayanan Sundaram, Jongsoo Park, Xiaodong Wang, Udit Gupta, Carole-Jean Wu,
# Alisson G. Azzolini, Dmytro Dzhulgakov, Andrey Mallevich, Ilia Cherniavskii,
# Yinghai Lu, Raghuraman Krishnamoorthi, Ansha Yu, Volodymyr Kondratenko,
# Stephanie Pereira, Xianjie Chen, Wenlin Chen, Vijay Rao, Bill Jia, Liang Xiong,
# Misha Smelyanskiy, "Deep Learning Recommendation Model for Personalization and
# Recommendation Systems", CoRR, arXiv:1906.00091, 2019

# TERMS:
#
# qr_       quotient-remainder trick
# md_       mixed-dimension trick
# lS_i      Indices used as inputs to embedding bag operators. Indices determine
#           which embeddings to select.
# lS_o      Offsets used as inputs to embedding bag operators. Offsets determine how
#           the selected embeddings are grouped together for the 'mode' operation.
#           (Mode operation examples: sum, mean, max)

from __future__ import absolute_import, division, print_function, unicode_literals

import argparse

# miscellaneous
import builtins
import datetime
import json
import sys
import time
import itertools
import traceback

# onnx
# The onnx import causes deprecation warnings every time workers
# are spawned during testing. So, we filter out those warnings.
import warnings

# data generation
import dlrm_data_pytorch as dp

# For distributed run
import extend_distributed as ext_dist
import mlperf_logger

# numpy
import numpy as np
import optim.rwsadagrad as RowWiseSparseAdagrad
import sklearn.metrics

# pytorch
import torch
import torch.nn as nn
from torch._ops import ops
from torch.autograd.profiler import record_function
from torch.nn.parallel.parallel_apply import parallel_apply
from torch.nn.parallel.replicate import replicate
from torch.nn.parallel.scatter_gather import gather, scatter
from torch.nn.parameter import Parameter
from torch.optim.lr_scheduler import _LRScheduler
from torch.utils.tensorboard import SummaryWriter

try:
    import fbgemm_gpu
    from fbgemm_gpu import split_table_batched_embeddings_ops
    from fbgemm_gpu.split_table_batched_embeddings_ops import (
        CacheAlgorithm,
        PoolingMode,
        OptimType,
        SparseType,
        SplitTableBatchedEmbeddingBagsCodegen,
        IntNBitTableBatchedEmbeddingBagsCodegen,
    )
except (ImportError, OSError):
    fbgemm_gpu_import_error_msg = traceback.format_exc()
    fbgemm_gpu = None

try:
    import apex
except (ImportError, OSError):
    apex_import_error_msg = traceback.format_exc()
    apex = None

try:
    import torch2trt
    from torch2trt import torch2trt
except (ImportError, OSError):
    torch2trt_import_error_msg = traceback.format_exc()
    torch2trt = None

# mixed-dimension trick
from tricks.md_embedding_bag import PrEmbeddingBag, md_solver

# FB5 Logger
import pathlib
from os import fspath
p = pathlib.Path(__file__).parent.resolve() / "../../../fb5logging"
sys.path.append(fspath(p))
from fb5logger import FB5Logger
import loggerconstants

# quotient-remainder trick
from tricks.qr_embedding_bag import QREmbeddingBag

with warnings.catch_warnings():
    warnings.filterwarnings("ignore", category=DeprecationWarning)
    try:
        import onnx
    except ImportError as error:
        print("Unable to import onnx. ", error)

# from torchviz import make_dot
# import torch.nn.functional as Functional
# from torch.nn.parameter import Parameter

exc = getattr(builtins, "IOError", "FileNotFoundError")


def time_wrap(use_gpu):
    if use_gpu:
        torch.cuda.synchronize()
    return time.time()


def dlrm_wrap(X, lS_o, lS_i, use_gpu, device, ndevices=1):
    with record_function("DLRM forward"):
        if use_gpu:  # .cuda()
            # lS_i can be either a list of tensors or a stacked tensor.
            # Handle each case below:
            if ndevices == 1:
                lS_i = (
                    [S_i.to(device) for S_i in lS_i]
                    if isinstance(lS_i, list)
                    else lS_i.to(device)
                )
                lS_o = (
                    [S_o.to(device) for S_o in lS_o]
                    if isinstance(lS_o, list)
                    else lS_o.to(device)
                )
        return dlrm(X.to(device), lS_o, lS_i)


def loss_fn_wrap(Z, T, use_gpu, device):
    with record_function("DLRM loss compute"):
        if args.loss_function == "mse" or args.loss_function == "bce":
            return dlrm.loss_fn(Z, T.to(device))
        elif args.loss_function == "wbce":
            loss_ws_ = dlrm.loss_ws[T.data.view(-1).long()].view_as(T).to(device)
            loss_fn_ = dlrm.loss_fn(Z, T.to(device))
            loss_sc_ = loss_ws_ * loss_fn_
            return loss_sc_.mean()


# The following function is a wrapper to avoid checking this multiple times in th
# loop below.
def unpack_batch(b):
    # Experiment with unweighted samples
    return b[0], b[1], b[2], b[3], torch.ones(b[3].size()), None


class LRPolicyScheduler(_LRScheduler):
    def __init__(self, optimizer, num_warmup_steps, decay_start_step, num_decay_steps):
        self.num_warmup_steps = num_warmup_steps
        self.decay_start_step = decay_start_step
        self.decay_end_step = decay_start_step + num_decay_steps
        self.num_decay_steps = num_decay_steps

        if self.decay_start_step < self.num_warmup_steps:
            sys.exit("Learning rate warmup must finish before the decay starts")

        super(LRPolicyScheduler, self).__init__(optimizer)

    def get_lr(self):
        step_count = self._step_count
        if step_count < self.num_warmup_steps:
            # warmup
            scale = 1.0 - (self.num_warmup_steps - step_count) / self.num_warmup_steps
            lr = [base_lr * scale for base_lr in self.base_lrs]
            self.last_lr = lr
        elif self.decay_start_step <= step_count and step_count < self.decay_end_step:
            # decay
            decayed_steps = step_count - self.decay_start_step
            scale = ((self.num_decay_steps - decayed_steps) / self.num_decay_steps) ** 2
            min_lr = 0.0000001
            lr = [max(min_lr, base_lr * scale) for base_lr in self.base_lrs]
            self.last_lr = lr
        else:
            if self.num_decay_steps > 0:
                # freeze at last, either because we're after decay
                # or because we're between warmup and decay
                lr = self.last_lr
            else:
                # do not adjust
                lr = self.base_lrs
        return lr


# quantize_fbgemm_gpu_embedding_bag is partially lifted from
# fbgemm_gpu/test/split_embedding_inference_converter.py, def _quantize_split_embs.
# Converts SplitTableBatchedEmbeddingBagsCodegen to IntNBitTableBatchedEmbeddingBagsCodegen
def quantize_fbgemm_gpu_embedding_bag(model, quantize_type, device):
    embedding_specs = []
    if device.type == "cpu":
        emb_location = split_table_batched_embeddings_ops.EmbeddingLocation.HOST
    else:
        emb_location = split_table_batched_embeddings_ops.EmbeddingLocation.DEVICE

    for (E, D, _, _) in model.embedding_specs:
        weights_ty = quantize_type
        if D % weights_ty.align_size() != 0:
            assert D % 4 == 0
            weights_ty = (
                SparseType.FP16
            )  # fall back to FP16 if dimension couldn't be aligned with the required size
        embedding_specs.append(("", E, D, weights_ty, emb_location))

    q_model = (
        split_table_batched_embeddings_ops.IntNBitTableBatchedEmbeddingBagsCodegen(
            embedding_specs=embedding_specs,
            pooling_mode=model.pooling_mode,
            device=device,
        )
    )
    q_model.initialize_weights()
    for t, (_, _, _, weight_ty, _) in enumerate(embedding_specs):
        if weight_ty == SparseType.FP16:
            original_weight = model.split_embedding_weights()[t]
            q_weight = original_weight.half()
            weights = torch.tensor(q_weight.cpu().numpy().view(np.uint8))
            q_model.split_embedding_weights()[t][0].data.copy_(weights)

        elif weight_ty == SparseType.INT8:
            original_weight = model.split_embedding_weights()[t]
            q_weight = torch.ops.fbgemm.FloatToFused8BitRowwiseQuantized(
                original_weight
            )
            weights = q_weight[:, :-8]
            scale_shift = torch.tensor(
                q_weight[:, -8:]
                .contiguous()
                .cpu()
                .numpy()
                .view(np.float32)
                .astype(np.float16)
                .view(np.uint8)
            )
            q_model.split_embedding_weights()[t][0].data.copy_(weights)
            q_model.split_embedding_weights()[t][1].data.copy_(scale_shift)

        elif weight_ty == SparseType.INT4 or weight_ty == SparseType.INT2:
            original_weight = model.split_embedding_weights()[t]
            q_weight = torch.ops.fbgemm.FloatToFusedNBitRowwiseQuantizedSBHalf(
                original_weight,
                bit_rate=quantize_type.bit_rate(),
            )
            weights = q_weight[:, :-4]
            scale_shift = torch.tensor(
                q_weight[:, -4:].contiguous().cpu().numpy().view(np.uint8)
            )
            q_model.split_embedding_weights()[t][0].data.copy_(weights)
            q_model.split_embedding_weights()[t][1].data.copy_(scale_shift)
    return q_model


def create_fbgemm_gpu_emb_bag(
    device,
    emb_l,
    m_spa,
    quantize_bits,
    learning_rate,
    codegen_preference=None,
    requires_grad=True,
):
    if isinstance(emb_l[0], PrEmbeddingBag):
        emb_l = [e.embs for e in emb_l]
    if isinstance(emb_l[0], nn.EmbeddingBag):
        emb_l = [e.weight for e in emb_l]
    Es = [e.shape[0] for e in emb_l]

    if isinstance(m_spa, list):
        Ds = m_spa
    else:
        Ds = [m_spa for _ in emb_l]

    if device.type == "cpu":
        emb_location = split_table_batched_embeddings_ops.EmbeddingLocation.HOST
        compute_device = split_table_batched_embeddings_ops.ComputeDevice.CPU
    else:
        emb_location = split_table_batched_embeddings_ops.EmbeddingLocation.DEVICE
        compute_device = split_table_batched_embeddings_ops.ComputeDevice.CUDA
    pooling_mode = PoolingMode.SUM
    cache_algorithm = CacheAlgorithm.LRU

    sparse_type_dict = {
        4: SparseType.INT4,
        8: SparseType.INT8,
        16: SparseType.FP16,
        32: SparseType.FP32,
    }
    codegen_type_dict = {
        4: "IntN",
        8: "Split" if codegen_preference != "IntN" else "IntN",
        16: "Split" if codegen_preference != "IntN" else "IntN",
        32: "Split",
    }

    codegen_type = codegen_type_dict[quantize_bits]
    quantize_type = sparse_type_dict[quantize_bits]
    if codegen_type == "IntN":
        # Create non-quantized model and then call quantize_fbgemm_gpu_embedding_bag
        fbgemm_gpu_emb_bag = SplitTableBatchedEmbeddingBagsCodegen(
            embedding_specs=[
                (
                    E,  # num of rows in the table
                    D,  # num of columns in the table
                    split_table_batched_embeddings_ops.EmbeddingLocation.HOST,
                    split_table_batched_embeddings_ops.ComputeDevice.CPU,
                )
                for (E, D) in zip(Es, Ds)
            ],
            weights_precision=SparseType.FP32,
            optimizer=OptimType.EXACT_SGD,
            learning_rate=learning_rate,
            cache_algorithm=cache_algorithm,
            pooling_mode=pooling_mode,
        ).to(device)
        if quantize_type == quantize_type.FP16:
            weights = fbgemm_gpu_emb_bag.split_embedding_weights()
            for i, emb in enumerate(weights):
                emb.data.copy_(emb_l[i])

        elif quantize_type == quantize_type.INT8:
            # copy quantized values upsampled/recasted to FP32
            for i in range(len(Es)):
                fbgemm_gpu_emb_bag.split_embedding_weights()[i].data.copy_(
                    torch.ops.fbgemm.Fused8BitRowwiseQuantizedToFloat(emb_l[i])
                )
        elif quantize_type == quantize_type.INT4:
            # copy quantized values upsampled/recasted to FP32
            for i in range(len(Es)):
                fbgemm_gpu_emb_bag.split_embedding_weights()[i].data.copy_(
                    torch.ops.fbgemm.FusedNBitRowwiseQuantizedSBHalfToFloat(
                        emb_l[i],
                        bit_rate=quantize_type.bit_rate(),
                    )
                )
        fbgemm_gpu_emb_bag = quantize_fbgemm_gpu_embedding_bag(
            fbgemm_gpu_emb_bag, quantize_type, device
        )
    else:
        fbgemm_gpu_emb_bag = SplitTableBatchedEmbeddingBagsCodegen(
            embedding_specs=[
                (
                    E,  # num of rows in the table
                    D,  # num of columns in the table
                    emb_location,
                    compute_device,
                )
                for (E, D) in zip(Es, Ds)
            ],
            weights_precision=quantize_type,
            optimizer=OptimType.EXACT_SGD,
            learning_rate=learning_rate,
            cache_algorithm=cache_algorithm,
            pooling_mode=pooling_mode,
        ).to(device)

        weights = fbgemm_gpu_emb_bag.split_embedding_weights()
        for i, emb in enumerate(weights):
            emb.data.copy_(emb_l[i])

    if not requires_grad:
        torch.no_grad()
        torch.set_grad_enabled(False)

    return fbgemm_gpu_emb_bag


# The purpose of this wrapper is to encapsulate the format conversions to/from fbgemm_gpu
# so parallel_apply() executes the format-in -> fbgemm_gpu op -> format-out instructions
# for each respective GPU in parallel.
class fbgemm_gpu_emb_bag_wrapper(nn.Module):
    def __init__(
        self,
        device,
        emb_l,
        m_spa,
        quantize_bits,
        learning_rate,
        codegen_preference,
        requires_grad,
    ):
        super(fbgemm_gpu_emb_bag_wrapper, self).__init__()
        self.fbgemm_gpu_emb_bag = create_fbgemm_gpu_emb_bag(
            device,
            emb_l,
            m_spa,
            quantize_bits,
            learning_rate,
            codegen_preference,
            requires_grad,
        )
        self.device = device
        self.m_spa = m_spa
        # create cumsum array for mixed dimension support
        if isinstance(m_spa, list):
            self.m_spa_cumsum = np.cumsum([0] + m_spa)
        if not requires_grad:
            torch.no_grad()
            torch.set_grad_enabled(False)

    def forward(self, lS_o, lS_i, v_W_l=None):

        # convert offsets to fbgemm format
        lengths_list = list(map(len, lS_i))
        indices_lengths_cumsum = np.cumsum([0] + lengths_list)
        if isinstance(lS_o, list):
            lS_o = torch.stack(lS_o)
        lS_o = lS_o.to(self.device)
        lS_o += torch.from_numpy(indices_lengths_cumsum[:-1, np.newaxis]).to(
            self.device
        )
        numel = torch.tensor([indices_lengths_cumsum[-1]], dtype=torch.long).to(
            self.device
        )
        lS_o = torch.cat((lS_o.flatten(), numel))

        # create per_sample_weights
        if v_W_l:
            per_sample_weights = torch.cat(
                [a.gather(0, b) for a, b in zip(v_W_l, lS_i)]
            )
        else:
            per_sample_weights = None

        # convert indices to fbgemm_gpu format
        if isinstance(lS_i, torch.Tensor):
            lS_i = [lS_i]
        lS_i = torch.cat(lS_i, dim=0).to(self.device)

        if isinstance(self.fbgemm_gpu_emb_bag, IntNBitTableBatchedEmbeddingBagsCodegen):
            lS_o = lS_o.int()
            lS_i = lS_i.int()

        # gpu embedding bag op
        ly = self.fbgemm_gpu_emb_bag(lS_i, lS_o, per_sample_weights)

        # convert the results to the next layer's input format.
        if isinstance(self.m_spa, list):
            # handle mixed dimensions case.
            ly = [
                ly[:, s:e]
                for (s, e) in zip(self.m_spa_cumsum[:-1], self.m_spa_cumsum[1:])
            ]
        else:
            # handle case in which all tables share the same column dimension.
            cols = self.m_spa
            ntables = len(self.fbgemm_gpu_emb_bag.embedding_specs)
            ly = ly.reshape(-1, ntables, cols).swapaxes(0, 1)
            ly = list(ly)
        return ly


### define dlrm in PyTorch ###
class DLRM_Net(nn.Module):
    def create_mlp(self, ln, sigmoid_layer):
        # build MLP layer by layer
        layers = nn.ModuleList()
        layers.training = self.requires_grad
        for i in range(0, ln.size - 1):
            n = ln[i]
            m = ln[i + 1]

            # construct fully connected operator
            LL = nn.Linear(int(n), int(m), bias=True)

            # initialize the weights
            # with torch.no_grad():
            # custom Xavier input, output or two-sided fill
            mean = 0.0  # std_dev = np.sqrt(variance)
            std_dev = np.sqrt(2 / (m + n))  # np.sqrt(1 / m) # np.sqrt(1 / n)
            W = np.random.normal(mean, std_dev, size=(m, n)).astype(np.float32)
            std_dev = np.sqrt(1 / m)  # np.sqrt(2 / (m + 1))
            bt = np.random.normal(mean, std_dev, size=m).astype(np.float32)
            # approach 1
            LL.weight.data = torch.tensor(W)
            LL.weight.requires_grad = self.requires_grad
            LL.bias.data = torch.tensor(bt)
            LL.bias.requires_grad = self.requires_grad
            # approach 2
            # LL.weight.data.copy_(torch.tensor(W))
            # LL.bias.data.copy_(torch.tensor(bt))
            # approach 3
            # LL.weight = Parameter(torch.tensor(W),requires_grad=True)
            # LL.bias = Parameter(torch.tensor(bt),requires_grad=True)
            layers.append(LL)

            # construct sigmoid or relu operator
            if i == sigmoid_layer:
                layers.append(nn.Sigmoid())
            else:
                layers.append(nn.ReLU())

        # approach 1: use ModuleList
        # return layers
        # approach 2: use Sequential container to wrap all layers
        return torch.nn.Sequential(*layers)

    def create_emb(self, m, ln, weighted_pooling=None):
        # create_emb parameter description
        #
        # ln parameter:
        # ln is a list of all the tables' row counts. E.g. [10,5,16] would mean
        # table 0 has 10 rows, table 1 has 5 rows, and table 2 has 16 rows.
        #
        # m parameter (when m is a single value):
        # m is the length of all embedding vectors. All embedding vectors in all
        # embedding tables are created to be the same length. E.g. if ln were [3,2,5]
        # and m were 4, table 0 would be dimension 3 x 4, table 1 would be 2 x 4,
        # and table 2 would be 5 x 4.
        #
        # m parameter (when m is a list):
        # m is a list of all the tables' column counts. E.g. if m were [4,5,6] and
        # ln were [3,2,5], table 0 would be dimension 3 x 4, table 1 would be 2 x 5,
        # and table 2 would be 5 x 6.
        #
        # Key to remember:
        # embedding table i has shape: ln[i] rows, m columns, when m is a single value.
        # embedding table i has shape: ln[i] rows, m[i] columns, when m is a list.

        emb_l = nn.ModuleList()
        v_W_l = []
        for i in range(0, ln.size):
            if ext_dist.my_size > 1:
                if i not in self.local_emb_indices:
                    continue
            n = ln[i]

            # construct embedding operator
            if self.qr_flag and n > self.qr_threshold:
                EE = QREmbeddingBag(
                    n,
                    m,
                    self.qr_collisions,
                    operation=self.qr_operation,
                    mode="sum",
                    sparse=True,
                )
            elif self.md_flag and n > self.md_threshold:
                base = max(m)
                _m = m[i] if n > self.md_threshold else base
                EE = PrEmbeddingBag(n, _m, base)
                # use np initialization as below for consistency...
                W = np.random.uniform(
                    low=-np.sqrt(1 / n), high=np.sqrt(1 / n), size=(n, _m)
                ).astype(np.float32)
                EE.embs.weight.data = torch.tensor(W, requires_grad=self.requires_grad)
            else:
                EE = nn.EmbeddingBag(n, m, mode="sum", sparse=True)
                # initialize embeddings
                # nn.init.uniform_(EE.weight, a=-np.sqrt(1 / n), b=np.sqrt(1 / n))
                W = np.random.uniform(
                    low=-np.sqrt(1 / n), high=np.sqrt(1 / n), size=(n, m)
                ).astype(np.float32)
                # approach 1
                EE.weight.data = torch.tensor(W, requires_grad=self.requires_grad)
                # approach 2
                # EE.weight.data.copy_(torch.tensor(W))
                # approach 3
                # EE.weight = Parameter(torch.tensor(W),requires_grad=True)
            if weighted_pooling is None:
                v_W_l.append(None)
            else:
                v_W_l.append(torch.ones(n, dtype=torch.float32))
            emb_l.append(EE)
        return emb_l, v_W_l

    def __init__(
        self,
        m_spa=None,
        ln_emb=None,
        ln_bot=None,
        ln_top=None,
        arch_interaction_op=None,
        arch_interaction_itself=False,
        sigmoid_bot=-1,
        sigmoid_top=-1,
        sync_dense_params=True,
        loss_threshold=0.0,
        ndevices=-1,
        qr_flag=False,
        qr_operation="mult",
        qr_collisions=0,
        qr_threshold=200,
        md_flag=False,
        md_threshold=200,
        weighted_pooling=None,
        loss_function="bce",
        learning_rate=0.1,
        use_gpu=False,
        use_fbgemm_gpu=False,
        fbgemm_gpu_codegen_pref="Split",
        inference_only=False,
        quantize_mlp_with_bit=False,
        quantize_emb_with_bit=False,
    ):
        super(DLRM_Net, self).__init__()

        if (
            (m_spa is not None)
            and (ln_emb is not None)
            and (ln_bot is not None)
            and (ln_top is not None)
            and (arch_interaction_op is not None)
        ):
            # save arguments
            self.ntables = len(ln_emb)
            self.m_spa = m_spa
            self.use_gpu = use_gpu
            self.use_fbgemm_gpu = use_fbgemm_gpu
            self.fbgemm_gpu_codegen_pref = fbgemm_gpu_codegen_pref
            self.requires_grad = not inference_only
            self.ndevices_available = ndevices
            self.ndevices_in_use = ndevices
            self.output_d = 0
            self.add_new_weights_to_params = False
            self.arch_interaction_op = arch_interaction_op
            self.arch_interaction_itself = arch_interaction_itself
            self.sync_dense_params = sync_dense_params and not inference_only
            self.loss_threshold = loss_threshold
            self.loss_function = loss_function
            self.learning_rate = learning_rate
            if weighted_pooling is not None and weighted_pooling != "fixed":
                self.weighted_pooling = "learned"
            else:
                self.weighted_pooling = weighted_pooling
            # create variables for QR embedding if applicable
            self.qr_flag = qr_flag
            if self.qr_flag:
                self.qr_collisions = qr_collisions
                self.qr_operation = qr_operation
                self.qr_threshold = qr_threshold
            # create variables for MD embedding if applicable
            self.md_flag = md_flag
            if self.md_flag:
                self.md_threshold = md_threshold

            # If running distributed, get local slice of embedding tables
            if ext_dist.my_size > 1:
                n_emb = len(ln_emb)
                if n_emb < ext_dist.my_size:
                    sys.exit(
                        "only (%d) sparse features for (%d) devices, table partitions will fail"
                        % (n_emb, ext_dist.my_size)
                    )
                self.n_global_emb = n_emb
                self.n_local_emb, self.n_emb_per_rank = ext_dist.get_split_lengths(
                    n_emb
                )
                self.local_emb_slice = ext_dist.get_my_slice(n_emb)
                self.local_emb_indices = list(range(n_emb))[self.local_emb_slice]

            # create operators
            self.emb_l, self.v_W_l = self.create_emb(m_spa, ln_emb, weighted_pooling)
            if self.weighted_pooling == "learned":
                self.v_W_l = nn.ParameterList(list(map(Parameter, self.v_W_l)))

            self.bot_l = self.create_mlp(ln_bot, sigmoid_bot)
            self.top_l = self.create_mlp(ln_top, sigmoid_top)

            # quantization
            self.quantize_emb = False
            self.emb_l_q = []
            self.quantize_bits = 32

            # fbgemm_gpu
            self.fbgemm_emb_l = []
            self.v_W_l_l = [self.v_W_l] if self.weighted_pooling else [None]

            self.interact_features_l = []

            # specify the loss function
            if self.loss_function == "mse":
                self.loss_fn = torch.nn.MSELoss(reduction="mean")
            elif self.loss_function == "bce":
                self.loss_fn = torch.nn.BCELoss(reduction="mean")
            elif self.loss_function == "wbce":
                self.loss_ws = torch.tensor(
                    np.fromstring(args.loss_weights, dtype=float, sep="-")
                )
                self.loss_fn = torch.nn.BCELoss(reduction="none")
            else:
                sys.exit(
                    "ERROR: --loss-function=" + self.loss_function + " is not supported"
                )

    def prepare_parallel_model(self, ndevices):
        device_ids = range(ndevices)
        # replicate mlp (data parallelism)
        self.bot_l_replicas = replicate(self.bot_l, device_ids)
        self.top_l_replicas = replicate(self.top_l, device_ids)

        # distribute embeddings (model parallelism)
        if self.weighted_pooling is not None:
            for k, w in enumerate(self.v_W_l):
                self.v_W_l[k] = Parameter(
                    w.to(torch.device("cuda:" + str(k % ndevices)))
                )
        if not self.use_fbgemm_gpu:
            for k, w in enumerate(self.emb_l):
                self.emb_l[k] = w.to(torch.device("cuda:" + str(k % ndevices)))
        else:
            self.fbgemm_emb_l, self.v_W_l_l = zip(
                *[
                    (
                        fbgemm_gpu_emb_bag_wrapper(
                            torch.device("cuda:" + str(k)),
                            self.emb_l[k::ndevices]
                            if self.emb_l
                            else self.emb_l_q[k::ndevices],
                            self.m_spa[k::ndevices]
                            if isinstance(self.m_spa, list)
                            else self.m_spa,
                            self.quantize_bits,
                            self.learning_rate,
                            self.fbgemm_gpu_codegen_pref,
                            self.requires_grad,
                        ),
                        self.v_W_l[k::ndevices] if self.weighted_pooling else None,
                    )
                    for k in range(ndevices)
                ]
            )
            self.add_new_weights_to_params = True
        self.interact_features_l = [self.nn_module_wrapper() for _ in range(ndevices)]

    # nn_module_wrapper is used to call functions concurrently across multi-gpus, using parallel_apply,
    # which requires an nn.Module subclass.
    class nn_module_wrapper(nn.Module):
        def __init__(self):
            super(DLRM_Net.nn_module_wrapper, self).__init__()
        def forward(self, E, x, ly):
            return E(x, ly)

    def apply_mlp(self, x, layers):
        # approach 1: use ModuleList
        # for layer in layers:
        #     x = layer(x)
        # return x
        # approach 2: use Sequential container to wrap all layers
        return layers(x)

    def apply_emb(self, lS_o, lS_i):
        # WARNING: notice that we are processing the batch at once. We implicitly
        # assume that the data is laid out such that:
        # 1. each embedding is indexed with a group of sparse indices,
        #   corresponding to a single lookup
        # 2. for each embedding the lookups are further organized into a batch
        # 3. for a list of embedding tables there is a list of batched lookups

        if self.use_fbgemm_gpu:
            # Deinterleave and reshape to 2d, so items are grouped by device
            # per row. Then parallel apply.
            ndevices = len(self.fbgemm_emb_l)
            lS_o_l = [lS_o[k::ndevices] for k in range(ndevices)]
            lS_i_l = [lS_i[k::ndevices] for k in range(ndevices)]
            ly = parallel_apply(
                self.fbgemm_emb_l, list(zip(lS_o_l, lS_i_l, self.v_W_l_l))
            )
            # Interleave and flatten to match non-fbgemm_gpu ly format.
            ly = [ly[i % ndevices][i // ndevices] for i in range(self.ntables)]
        else:
            ly = []
            for k, sparse_index_group_batch in enumerate(lS_i):
                sparse_offset_group_batch = lS_o[k]

                # embedding lookup
                # We are using EmbeddingBag, which implicitly uses sum operator.
                # The embeddings are represented as tall matrices, with sum
                # happening vertically across 0 axis, resulting in a row vector
                # E = emb_l[k]

                if self.v_W_l[k] is not None:
                    per_sample_weights = self.v_W_l[k].gather(
                        0, sparse_index_group_batch
                    )
                else:
                    per_sample_weights = None

                if self.quantize_emb:
                    if self.quantize_bits == 4:
                        E = ops.quantized.embedding_bag_4bit_rowwise_offsets
                    elif self.quantize_bits == 8:
                        E = ops.quantized.embedding_bag_byte_rowwise_offsets
                    QV = E(
                        self.emb_l_q[k],
                        sparse_index_group_batch,
                        sparse_offset_group_batch,
                        per_sample_weights=per_sample_weights,
                    )

                    ly.append(QV)
                else:
                    E = self.emb_l[k]
                    V = E(
                        sparse_index_group_batch,
                        sparse_offset_group_batch,
                        per_sample_weights=per_sample_weights,
                    )

                    ly.append(V)

        # print(ly)
        return ly

    #  using quantizing functions from caffe2/aten/src/ATen/native/quantized/cpu
    def quantize_embedding(self, bits):

        n = len(self.emb_l)
        self.emb_l_q = [None] * n
        for k in range(n):
            if bits == 4:
                self.emb_l_q[k] = ops.quantized.embedding_bag_4bit_prepack(
                    self.emb_l[k].weight
                )
            elif bits == 8:
                self.emb_l_q[k] = ops.quantized.embedding_bag_byte_prepack(
                    self.emb_l[k].weight
                )
            elif bits == 16:
                self.emb_l_q[k] = self.emb_l[k].half().weight
            else:
                return
        self.emb_l = None
        self.quantize_emb = True
        self.quantize_bits = bits

    def interact_features(self, x, ly):

        if self.arch_interaction_op == "dot":
            # concatenate dense and sparse features
            (batch_size, d) = x.shape
            T = torch.cat([x] + ly, dim=1).view((batch_size, -1, d))
            # perform a dot product
            Z = torch.bmm(T, torch.transpose(T, 1, 2))
            # append dense feature with the interactions (into a row vector)
            # approach 1: all
            # Zflat = Z.view((batch_size, -1))
            # approach 2: unique
            _, ni, nj = Z.shape
            # approach 1: tril_indices
            # offset = 0 if self.arch_interaction_itself else -1
            # li, lj = torch.tril_indices(ni, nj, offset=offset)
            # approach 2: custom
            offset = 1 if self.arch_interaction_itself else 0
            li = torch.tensor([i for i in range(ni) for j in range(i + offset)])
            lj = torch.tensor([j for i in range(nj) for j in range(i + offset)])
            Zflat = Z[:, li, lj]
            # concatenate dense features and interactions
            R = torch.cat([x] + [Zflat], dim=1)
        elif self.arch_interaction_op == "cat":
            # concatenation features (into a row vector)
            R = torch.cat([x] + ly, dim=1)
        else:
            sys.exit(
                "ERROR: --arch-interaction-op="
                + self.arch_interaction_op
                + " is not supported"
            )

        return R

    def forward(self, dense_x, lS_o, lS_i):
        if ext_dist.my_size > 1:
            # multi-node multi-device run
            return self.distributed_forward(dense_x, lS_o, lS_i)
        elif self.ndevices_available <= 1:
            # single device run
            return self.sequential_forward(dense_x, lS_o, lS_i)
        else:
            # single-node multi-device run
            return self.parallel_forward(dense_x, lS_o, lS_i)

    def distributed_forward(self, dense_x, lS_o, lS_i):
        batch_size = dense_x.size()[0]
        # WARNING: # of ranks must be <= batch size in distributed_forward call
        if batch_size < ext_dist.my_size:
            sys.exit(
                "ERROR: batch_size (%d) must be larger than number of ranks (%d)"
                % (batch_size, ext_dist.my_size)
            )
        if batch_size % ext_dist.my_size != 0:
            sys.exit(
                "ERROR: batch_size %d can not split across %d ranks evenly"
                % (batch_size, ext_dist.my_size)
            )

        dense_x = dense_x[ext_dist.get_my_slice(batch_size)]
        lS_o = lS_o[self.local_emb_slice]
        lS_i = lS_i[self.local_emb_slice]

        if (self.ntables != len(lS_o)) or (self.ntables != len(lS_i)):
            sys.exit(
                "ERROR: corrupted model input detected in distributed_forward call"
            )

        # embeddings
        with record_function("DLRM embedding forward"):
            ly = self.apply_emb(lS_o, lS_i)

        # WARNING: Note that at this point we have the result of the embedding lookup
        # for the entire batch on each rank. We would like to obtain partial results
        # corresponding to all embedding lookups, but part of the batch on each rank.
        # Therefore, matching the distribution of output of bottom mlp, so that both
        # could be used for subsequent interactions on each device.
        if self.ntables != len(ly):
            sys.exit("ERROR: corrupted intermediate result in distributed_forward call")

        a2a_req = ext_dist.alltoall(ly, self.n_emb_per_rank)

        with record_function("DLRM bottom nlp forward"):
            x = self.apply_mlp(dense_x, self.bot_l)

        ly = a2a_req.wait()
        ly = list(ly)

        # interactions
        with record_function("DLRM interaction forward"):
            z = self.interact_features(x, ly)

        # top mlp
        with record_function("DLRM top nlp forward"):
            p = self.apply_mlp(z, self.top_l)

        # clamp output if needed
        if 0.0 < self.loss_threshold and self.loss_threshold < 1.0:
            z = torch.clamp(p, min=self.loss_threshold, max=(1.0 - self.loss_threshold))
        else:
            z = p

        return z

    def sequential_forward(self, dense_x, lS_o, lS_i):
        # process dense features (using bottom mlp), resulting in a row vector
        x = self.apply_mlp(dense_x, self.bot_l)
        # debug prints
        # print("intermediate")
        # print(x.detach().cpu().numpy())

        # process sparse features(using embeddings), resulting in a list of row vectors
        ly = self.apply_emb(lS_o, lS_i)
        # for y in ly:
        #     print(y.detach().cpu().numpy())

        # interact features (dense and sparse)
        z = self.interact_features(x, ly)
        # print(z.detach().cpu().numpy())

        # obtain probability of a click (using top mlp)
        p = self.apply_mlp(z, self.top_l)

        # clamp output if needed
        if 0.0 < self.loss_threshold and self.loss_threshold < 1.0:
            z = torch.clamp(p, min=self.loss_threshold, max=(1.0 - self.loss_threshold))
        else:
            z = p

        return z

    def parallel_forward(self, dense_x, lS_o, lS_i):
        ### prepare model (overwrite) ###
        # WARNING: # of devices must be >= batch size in parallel_forward call
        batch_size = dense_x.size()[0]
        ndevices = min(self.ndevices_available, batch_size, self.ntables)
        device_ids = range(ndevices)
        # WARNING: must redistribute the model if mini-batch size changes(this is common
        # for last mini-batch, when # of elements in the dataset/batch size is not even
        if self.ndevices_in_use != ndevices:
            self.ndevices_in_use = ndevices
            self.prepare_parallel_model(ndevices)
        elif self.sync_dense_params:
            # When training, replicate the new/updated mlp weights each iteration.
            # For inference-only, this code should never run.
            self.bot_l_replicas = replicate(self.bot_l, device_ids)
            self.top_l_replicas = replicate(self.top_l, device_ids)

        ### prepare input (overwrite) ###
        # scatter dense features (data parallelism)
        # print(dense_x.device)
        dense_x = scatter(dense_x, device_ids, dim=0)
        # distribute sparse features (model parallelism)
        if (self.ntables != len(lS_o)) or (self.ntables != len(lS_i)):
            sys.exit("ERROR: corrupted model input detected in parallel_forward call")

        lS_o = [
            lS_o[k].to(torch.device("cuda:" + str(k % ndevices)))
            for k in range(self.ntables)
        ]
        lS_i = [
            lS_i[k].to(torch.device("cuda:" + str(k % ndevices)))
            for k in range(self.ntables)
        ]

        ### compute results in parallel ###
        # bottom mlp
        # WARNING: Note that the self.bot_l is a list of bottom mlp modules
        # that have been replicated across devices, while dense_x is a tuple of dense
        # inputs that has been scattered across devices on the first (batch) dimension.
        # The output is a list of tensors scattered across devices according to the
        # distribution of dense_x.
        x = parallel_apply(self.bot_l_replicas, dense_x, None, device_ids)
        # debug prints
        # print(x)

        # embeddings
        ly = self.apply_emb(lS_o, lS_i)
        # debug prints
        # print(ly)

        # butterfly shuffle (implemented inefficiently for now)
        # WARNING: Note that at this point we have the result of the embedding lookup
        # for the entire batch on each device. We would like to obtain partial results
        # corresponding to all embedding lookups, but part of the batch on each device.
        # Therefore, matching the distribution of output of bottom mlp, so that both
        # could be used for subsequent interactions on each device.
        if self.ntables != len(ly):
            sys.exit("ERROR: corrupted intermediate result in parallel_forward call")

        t_list = [scatter(ly[k], device_ids, dim=0) for k in range(self.ntables)]

        # adjust the list to be ordered per device
        ly = list(map(lambda y: list(y), zip(*t_list)))
        # debug prints
        # print(ly)

        # interactions
        z = parallel_apply(self.interact_features_l, list(zip(itertools.repeat(self.interact_features),x,ly)))
        # debug prints
        # print(z)

        # top mlp
        # WARNING: Note that the self.top_l is a list of top mlp modules that
        # have been replicated across devices, while z is a list of interaction results
        # that by construction are scattered across devices on the first (batch) dim.
        # The output is a list of tensors scattered across devices according to the
        # distribution of z.
        p = parallel_apply(self.top_l_replicas, z, None, device_ids)

        ### gather the distributed results ###
        p0 = gather(p, self.output_d, dim=0)

        # clamp output if needed
        if 0.0 < self.loss_threshold and self.loss_threshold < 1.0:
            z0 = torch.clamp(
                p0, min=self.loss_threshold, max=(1.0 - self.loss_threshold)
            )
        else:
            z0 = p0

        return z0

    def print_weights(self):
        if self.use_fbgemm_gpu and len(self.fbgemm_emb_l):
            ntables_l = [
                len(e.fbgemm_gpu_emb_bag.embedding_specs) for e in self.fbgemm_emb_l
            ]
            for j in range(ntables_l[0] + 1):
                for k, e in enumerate(self.fbgemm_emb_l):
                    if j < ntables_l[k]:
                        print(
                            e.fbgemm_gpu_emb_bag.split_embedding_weights()[j]
                            .detach()
                            .cpu()
                            .numpy()
                        )
        elif self.quantize_bits != 32:
            for e in self.emb_l_q:
                print(e.data.detach().cpu().numpy())
        else:  # if self.emb_l:
            for param in self.emb_l.parameters():
                print(param.detach().cpu().numpy())
        if isinstance(self.v_W_l, nn.ParameterList):
            for param in self.v_W_l.parameters():
                print(param.detach().cpu().numpy())
        for param in self.bot_l.parameters():
            print(param.detach().cpu().numpy())
        for param in self.top_l.parameters():
            print(param.detach().cpu().numpy())


def dash_separated_ints(value):
    vals = value.split("-")
    for val in vals:
        try:
            int(val)
        except ValueError:
            raise argparse.ArgumentTypeError(
                "%s is not a valid dash separated list of ints" % value
            )

    return value


def dash_separated_floats(value):
    vals = value.split("-")
    for val in vals:
        try:
            float(val)
        except ValueError:
            raise argparse.ArgumentTypeError(
                "%s is not a valid dash separated list of floats" % value
            )

    return value


def inference(
    args,
    dlrm,
    best_acc_test,
    best_auc_test,
    test_ld,
    device,
    use_gpu,
    log_iter=-1,
):
    test_accu = 0
    test_samp = 0

    if args.mlperf_logging:
        scores = []
        targets = []

    if args.fb5logger is not None:
        fb5logger = FB5Logger(args.fb5logger)
        fb5logger.header("DLRM", "OOTB", "eval", args.fb5config, score_metric=loggerconstants.EXPS)

    for i, testBatch in enumerate(test_ld):
        # early exit if nbatches was set by the user and was exceeded
        if nbatches > 0 and i >= nbatches:
            break

        if i == args.warmup_steps and args.fb5logger is not None:
            fb5logger.run_start()

        X_test, lS_o_test, lS_i_test, T_test, W_test, CBPP_test = unpack_batch(
            testBatch
        )

        # Skip the batch if batch size not multiple of total ranks
        if ext_dist.my_size > 1 and X_test.size(0) % ext_dist.my_size != 0:
            print("Warning: Skiping the batch %d with size %d" % (i, X_test.size(0)))
            continue

        # forward pass
        Z_test = dlrm_wrap(
            X_test,
            lS_o_test,
            lS_i_test,
            use_gpu,
            device,
            ndevices=ndevices,
        )
        ### gather the distributed results on each rank ###
        # For some reason it requires explicit sync before all_gather call if
        # tensor is on GPU memory
        if Z_test.is_cuda:
            torch.cuda.synchronize()
        (_, batch_split_lengths) = ext_dist.get_split_lengths(X_test.size(0))
        if ext_dist.my_size > 1:
            Z_test = ext_dist.all_gather(Z_test, batch_split_lengths)

        if args.mlperf_logging:
            S_test = Z_test.detach().cpu().numpy()  # numpy array
            T_test = T_test.detach().cpu().numpy()  # numpy array
            scores.append(S_test)
            targets.append(T_test)
        else:
            with record_function("DLRM accuracy compute"):
                # compute loss and accuracy
                S_test = Z_test.detach().cpu().numpy()  # numpy array
                T_test = T_test.detach().cpu().numpy()  # numpy array

                mbs_test = T_test.shape[0]  # = mini_batch_size except last
                A_test = np.sum((np.round(S_test, 0) == T_test).astype(np.uint8))

                test_accu += A_test
                test_samp += mbs_test

    if args.fb5logger is not None:
        fb5logger.run_stop(nbatches - args.warmup_steps, args.mini_batch_size)

    if args.mlperf_logging:
        with record_function("DLRM mlperf sklearn metrics compute"):
            scores = np.concatenate(scores, axis=0)
            targets = np.concatenate(targets, axis=0)

            metrics = {
                "recall": lambda y_true, y_score: sklearn.metrics.recall_score(
                    y_true=y_true, y_pred=np.round(y_score)
                ),
                "precision": lambda y_true, y_score: sklearn.metrics.precision_score(
                    y_true=y_true, y_pred=np.round(y_score)
                ),
                "f1": lambda y_true, y_score: sklearn.metrics.f1_score(
                    y_true=y_true, y_pred=np.round(y_score)
                ),
                "ap": sklearn.metrics.average_precision_score,
                "roc_auc": sklearn.metrics.roc_auc_score,
                "accuracy": lambda y_true, y_score: sklearn.metrics.accuracy_score(
                    y_true=y_true, y_pred=np.round(y_score)
                ),
            }

        validation_results = {}
        for metric_name, metric_function in metrics.items():
            validation_results[metric_name] = metric_function(targets, scores)
            writer.add_scalar(
                "mlperf-metrics-test/" + metric_name,
                validation_results[metric_name],
                log_iter,
            )
        acc_test = validation_results["accuracy"]
    else:
        acc_test = test_accu / test_samp
        writer.add_scalar("Test/Acc", acc_test, log_iter)

    model_metrics_dict = {
        "nepochs": args.nepochs,
        "nbatches": nbatches,
        "nbatches_test": nbatches_test,
        "state_dict": dlrm.state_dict(),
        "test_acc": acc_test,
    }

    if args.mlperf_logging:
        is_best = validation_results["roc_auc"] > best_auc_test
        if is_best:
            best_auc_test = validation_results["roc_auc"]
            model_metrics_dict["test_auc"] = best_auc_test
        print(
            "recall {:.4f}, precision {:.4f},".format(
                validation_results["recall"],
                validation_results["precision"],
            )
            + " f1 {:.4f}, ap {:.4f},".format(
                validation_results["f1"], validation_results["ap"]
            )
            + " auc {:.4f}, best auc {:.4f},".format(
                validation_results["roc_auc"], best_auc_test
            )
            + " accuracy {:3.3f} %, best accuracy {:3.3f} %".format(
                validation_results["accuracy"] * 100, best_acc_test * 100
            ),
            flush=True,
        )
    else:
        is_best = acc_test > best_acc_test
        if is_best:
            best_acc_test = acc_test
        print(
            " accuracy {:3.3f} %, best {:3.3f} %".format(
                acc_test * 100, best_acc_test * 100
            ),
            flush=True,
        )
    return model_metrics_dict, is_best


def run():
    ### parse arguments ###
    parser = argparse.ArgumentParser(
        description="Train Deep Learning Recommendation Model (DLRM)"
    )
    # model related parameters
    parser.add_argument("--arch-sparse-feature-size", type=int, default=2)
    parser.add_argument(
        "--arch-embedding-size", type=dash_separated_ints, default="4-3-2"
    )
    # j will be replaced with the table number
    parser.add_argument("--arch-mlp-bot", type=dash_separated_ints, default="4-3-2")
    parser.add_argument("--arch-mlp-top", type=dash_separated_ints, default="4-2-1")
    parser.add_argument(
        "--arch-interaction-op", type=str, choices=["dot", "cat"], default="dot"
    )
    parser.add_argument("--arch-interaction-itself", action="store_true", default=False)
    parser.add_argument(
        "--weighted-pooling", type=str, choices=["fixed", "learned", None], default=None
    )

    # embedding table options
    parser.add_argument("--md-flag", action="store_true", default=False)
    parser.add_argument("--md-threshold", type=int, default=200)
    parser.add_argument("--md-temperature", type=float, default=0.3)
    parser.add_argument("--md-round-dims", action="store_true", default=False)
    parser.add_argument("--qr-flag", action="store_true", default=False)
    parser.add_argument("--qr-threshold", type=int, default=200)
    parser.add_argument("--qr-operation", type=str, default="mult")
    parser.add_argument("--qr-collisions", type=int, default=4)
    # activations and loss
    parser.add_argument("--activation-function", type=str, default="relu")
    parser.add_argument("--loss-function", type=str, default="mse")  # or bce or wbce
    parser.add_argument(
        "--loss-weights", type=dash_separated_floats, default="1.0-1.0"
    )  # for wbce
    parser.add_argument("--loss-threshold", type=float, default=0.0)  # 1.0e-7
    parser.add_argument("--round-targets", type=bool, default=False)
    # data
    parser.add_argument("--data-size", type=int, default=1)
    parser.add_argument("--num-batches", type=int, default=0)
    parser.add_argument(
        "--data-generation", type=str, default="random"
    )  # synthetic or dataset
    parser.add_argument(
        "--rand-data-dist", type=str, default="uniform"
    )  # uniform or gaussian
    parser.add_argument("--rand-data-min", type=float, default=0)
    parser.add_argument("--rand-data-max", type=float, default=1)
    parser.add_argument("--rand-data-mu", type=float, default=-1)
    parser.add_argument("--rand-data-sigma", type=float, default=1)
    parser.add_argument("--data-trace-file", type=str, default="./input/dist_emb_j.log")
    parser.add_argument("--data-set", type=str, default="kaggle")  # or terabyte
    parser.add_argument("--raw-data-file", type=str, default="")
    parser.add_argument("--processed-data-file", type=str, default="")
    parser.add_argument("--data-randomize", type=str, default="total")  # or day or none
    parser.add_argument("--data-trace-enable-padding", type=bool, default=False)
    parser.add_argument("--max-ind-range", type=int, default=-1)
    parser.add_argument("--data-sub-sample-rate", type=float, default=0.0)  # in [0, 1]
    parser.add_argument("--num-indices-per-lookup", type=int, default=10)
    parser.add_argument("--num-indices-per-lookup-fixed", type=bool, default=False)
    parser.add_argument("--num-workers", type=int, default=0)
    parser.add_argument("--memory-map", action="store_true", default=False)
    # training
    parser.add_argument("--mini-batch-size", type=int, default=1)
    parser.add_argument("--nepochs", type=int, default=1)
    parser.add_argument("--learning-rate", type=float, default=0.01)
    parser.add_argument("--print-precision", type=int, default=5)
    parser.add_argument("--numpy-rand-seed", type=int, default=123)
    parser.add_argument("--sync-dense-params", type=bool, default=True)
    parser.add_argument("--optimizer", type=str, default="sgd")
    parser.add_argument(
        "--dataset-multiprocessing",
        action="store_true",
        default=False,
        help="The Kaggle dataset can be multiprocessed in an environment \
                        with more than 7 CPU cores and more than 20 GB of memory. \n \
                        The Terabyte dataset can be multiprocessed in an environment \
                        with more than 24 CPU cores and at least 1 TB of memory.",
    )
    # inference
    parser.add_argument("--inference-only", action="store_true", default=False)
    # quantize
    parser.add_argument("--quantize-mlp-with-bit", type=int, default=32)
    parser.add_argument("--quantize-emb-with-bit", type=int, default=32)
    # onnx
    parser.add_argument("--save-onnx", action="store_true", default=False)
    # gpu
    parser.add_argument("--use-gpu", action="store_true", default=False)
    parser.add_argument("--use-fbgemm-gpu", action="store_true", default=False)
    parser.add_argument(
        "--fbgemm-gpu-codegen-pref",
        type=str,
        choices=["Split", "IntN"],
        default="Split",
    )
    # torch2trt
    parser.add_argument("--use-torch2trt-for-mlp", action="store_true", default=False)
    # distributed
    parser.add_argument("--local_rank", type=int, default=-1)
    parser.add_argument("--dist-backend", type=str, default="")
    # debugging and profiling
    parser.add_argument("--print-freq", type=int, default=1)
    parser.add_argument("--test-freq", type=int, default=-1)
    parser.add_argument("--test-mini-batch-size", type=int, default=-1)
    parser.add_argument("--test-num-workers", type=int, default=-1)
    parser.add_argument("--print-time", action="store_true", default=False)
    parser.add_argument("--print-wall-time", action="store_true", default=False)
    parser.add_argument("--print-accumulated-time", action="store_true", default=False)
    parser.add_argument("--debug-mode", action="store_true", default=False)
    parser.add_argument("--enable-profiling", action="store_true", default=False)
    parser.add_argument("--plot-compute-graph", action="store_true", default=False)
    parser.add_argument("--tensor-board-filename", type=str, default="run_kaggle_pt")
    # store/load model
    parser.add_argument("--save-model", type=str, default="")
    parser.add_argument("--load-model", type=str, default="")
    # mlperf logging (disables other output and stops early)
    parser.add_argument("--mlperf-logging", action="store_true", default=False)
    # stop at target accuracy Kaggle 0.789, Terabyte (sub-sampled=0.875) 0.8107
    parser.add_argument("--mlperf-acc-threshold", type=float, default=0.0)
    # stop at target AUC Terabyte (no subsampling) 0.8025
    parser.add_argument("--mlperf-auc-threshold", type=float, default=0.0)
    parser.add_argument("--mlperf-bin-loader", action="store_true", default=False)
    parser.add_argument("--mlperf-bin-shuffle", action="store_true", default=False)
    # mlperf gradient accumulation iterations
    parser.add_argument("--mlperf-grad-accum-iter", type=int, default=1)
    # LR policy
    parser.add_argument("--lr-num-warmup-steps", type=int, default=0)
    parser.add_argument("--lr-decay-start-step", type=int, default=0)
    parser.add_argument("--lr-num-decay-steps", type=int, default=0)

    parser.add_argument("--precache-ml-data", type=int, nargs='?', default=None, const=sys.maxsize)
    parser.add_argument("--warmup-steps", type=int, default=0)
    # FB5 Logging
    parser.add_argument("--fb5logger", type=str, default=None)
    parser.add_argument("--fb5config", type=str, default="tiny")

    global args
    global nbatches
    global nbatches_test
    global writer
    args = parser.parse_args()

    if args.dataset_multiprocessing:
        assert float(sys.version[:3]) > 3.7, (
            "The dataset_multiprocessing "
            + "flag is susceptible to a bug in Python 3.7 and under. "
            + "https://github.com/facebookresearch/dlrm/issues/172"
        )

    if args.mlperf_logging:
        mlperf_logger.log_event(key=mlperf_logger.constants.CACHE_CLEAR, value=True)
        mlperf_logger.log_start(
            key=mlperf_logger.constants.INIT_START, log_all_ranks=True
        )

    if args.weighted_pooling is not None:
        if args.qr_flag:
            sys.exit("ERROR: quotient remainder with weighted pooling is not supported")
        if args.md_flag:
            sys.exit("ERROR: mixed dimensions with weighted pooling is not supported")
    if args.quantize_emb_with_bit in [4, 8]:
        if args.qr_flag:
            sys.exit(
                "ERROR: 4 and 8-bit quantization with quotient remainder is not supported"
            )
        if args.md_flag:
            sys.exit(
                "ERROR: 4 and 8-bit quantization with mixed dimensions is not supported"
            )
    if args.quantize_emb_with_bit in [4, 8, 16] and (
        not fbgemm_gpu or not args.use_fbgemm_gpu
    ):
        extra_info = ""
        if not fbgemm_gpu:
            extra_info += "\nfbgemm_gpu module failed to import.\n\n" + fbgemm_gpu_import_error_msg
        if not args.use_fbgemm_gpu:
            extra_info += "--use-fbgemm-gpu not set. "

        if not args.inference_only:
            sys.exit(
                "ERROR: Training quantized embeddings requires fbgemm_gpu. "
                + extra_info
            )
        elif args.use_gpu:
            sys.exit(
                "ERROR: Quantized embeddings on GPU requires fbgemm_gpu. " + extra_info
            )
        elif args.quantize_emb_with_bit == 16:
            sys.exit(
                "ERROR: 16-bit quantized embeddings requires fbgemm_gpu. " + extra_info
            )

    assert args.quantize_emb_with_bit in [
        4,
        8,
        16,
        32,
    ], "only support 4/8/16/32-bit but got {}".format(args.quantize_emb_with_bit)

    if args.use_gpu:
        assert torch.cuda.is_available(), "No cuda device is available."
    if args.use_fbgemm_gpu:
        assert fbgemm_gpu, ("\nfbgemm_gpu module failed to import.\n\n" + fbgemm_gpu_import_error_msg)
    use_gpu = args.use_gpu
    use_fbgemm_gpu = args.use_fbgemm_gpu

    ### some basic setup ###
    np.random.seed(args.numpy_rand_seed)
    np.set_printoptions(precision=args.print_precision)
    torch.set_printoptions(precision=args.print_precision)
    torch.manual_seed(args.numpy_rand_seed)

    if args.test_mini_batch_size < 0:
        # if the parameter is not set, use the training batch size
        args.test_mini_batch_size = args.mini_batch_size
    if args.test_num_workers < 0:
        # if the parameter is not set, use the same parameter for training
        args.test_num_workers = args.num_workers

    if not args.debug_mode:
        ext_dist.init_distributed(
            local_rank=args.local_rank, use_gpu=use_gpu, backend=args.dist_backend
        )

    if use_gpu:
        torch.cuda.manual_seed_all(args.numpy_rand_seed)
        torch.backends.cudnn.deterministic = True
        if ext_dist.my_size > 1:
            ngpus = 1
            device = torch.device("cuda", ext_dist.my_local_rank)
        else:
            ngpus = torch.cuda.device_count()
            device = torch.device("cuda", 0)
        print("Using {} GPU(s)...".format(ngpus))
    else:
        device = torch.device("cpu")
        print("Using CPU...")

    ### prepare training data ###
    ln_bot = np.fromstring(args.arch_mlp_bot, dtype=int, sep="-")
    # input data

    if args.mlperf_logging:
        mlperf_logger.barrier()
        mlperf_logger.log_end(key=mlperf_logger.constants.INIT_STOP)
        mlperf_logger.barrier()
        mlperf_logger.log_start(key=mlperf_logger.constants.RUN_START)
        mlperf_logger.barrier()

    if args.data_generation == "dataset":
        train_data, train_ld, test_data, test_ld = dp.make_criteo_data_and_loaders(args)
        table_feature_map = {idx: idx for idx in range(len(train_data.counts))}
        nbatches = args.num_batches if args.num_batches > 0 else len(train_ld)
        nbatches_test = len(test_ld)

        ln_emb = train_data.counts
        # enforce maximum limit on number of vectors per embedding
        if args.max_ind_range > 0:
            ln_emb = np.array(
                list(
                    map(
                        lambda x: x if x < args.max_ind_range else args.max_ind_range,
                        ln_emb,
                    )
                )
            )
        else:
            ln_emb = np.array(ln_emb)
        m_den = train_data.m_den
        ln_bot[0] = m_den
    else:
        # input and target at random
        ln_emb = np.fromstring(args.arch_embedding_size, dtype=int, sep="-")
        m_den = ln_bot[0]
        train_data, train_ld, test_data, test_ld = dp.make_random_data_and_loader(
            args, ln_emb, m_den, cache_size=args.precache_ml_data
        )
        nbatches = args.num_batches if args.num_batches > 0 else len(train_ld)
        nbatches_test = len(test_ld)

    assert args.num_batches > args.warmup_steps, (f"Change --warmup-steps={args.warmup_steps} to be lower than --num-batches={args.num_batches}.")

    args.ln_emb = ln_emb.tolist()
    if args.mlperf_logging:
        print("command line args: ", json.dumps(vars(args)))

    ### parse command line arguments ###
    m_spa = args.arch_sparse_feature_size
    ln_emb = np.asarray(ln_emb)
    num_fea = ln_emb.size + 1  # num sparse + num dense features

    if args.use_fbgemm_gpu:
        assert m_spa % 4 == 0, (
            f"{m_spa} % 4 is not 0, but fbgemm_gpu requires the embedding dim "
            + "(--arch-sparse-feature-size number) to be evenly divisible by 4."
        )

    m_den_out = ln_bot[ln_bot.size - 1]
    if args.arch_interaction_op == "dot":
        # approach 1: all
        # num_int = num_fea * num_fea + m_den_out
        # approach 2: unique
        if args.arch_interaction_itself:
            num_int = (num_fea * (num_fea + 1)) // 2 + m_den_out
        else:
            num_int = (num_fea * (num_fea - 1)) // 2 + m_den_out
    elif args.arch_interaction_op == "cat":
        num_int = num_fea * m_den_out
    else:
        sys.exit(
            "ERROR: --arch-interaction-op="
            + args.arch_interaction_op
            + " is not supported"
        )
    arch_mlp_top_adjusted = str(num_int) + "-" + args.arch_mlp_top
    ln_top = np.fromstring(arch_mlp_top_adjusted, dtype=int, sep="-")

    # sanity check: feature sizes and mlp dimensions must match
    if m_den != ln_bot[0]:
        sys.exit(
            "ERROR: arch-dense-feature-size "
            + str(m_den)
            + " does not match first dim of bottom mlp "
            + str(ln_bot[0])
        )
    if args.qr_flag:
        if args.qr_operation == "concat" and 2 * m_spa != m_den_out:
            sys.exit(
                "ERROR: 2 arch-sparse-feature-size "
                + str(2 * m_spa)
                + " does not match last dim of bottom mlp "
                + str(m_den_out)
                + " (note that the last dim of bottom mlp must be 2x the embedding dim)"
            )
        if args.qr_operation != "concat" and m_spa != m_den_out:
            sys.exit(
                "ERROR: arch-sparse-feature-size "
                + str(m_spa)
                + " does not match last dim of bottom mlp "
                + str(m_den_out)
            )
    else:
        if m_spa != m_den_out:
            sys.exit(
                "ERROR: arch-sparse-feature-size "
                + str(m_spa)
                + " does not match last dim of bottom mlp "
                + str(m_den_out)
            )
    if num_int != ln_top[0]:
        sys.exit(
            "ERROR: # of feature interactions "
            + str(num_int)
            + " does not match first dimension of top mlp "
            + str(ln_top[0])
        )

    # assign mixed dimensions if applicable
    if args.md_flag:
        m_spa = md_solver(
            torch.tensor(ln_emb),
            args.md_temperature,  # alpha
            d0=m_spa,
            round_dim=args.md_round_dims,
        ).tolist()
        if use_fbgemm_gpu:
            for m in m_spa:
                assert m % 4 == 0, (
                    "Found an incompatible embedding dim in m_spa. "
                    + f"{m} % 4 is not 0, but fbgemm_gpu requires the "
                    + "embedding dim to be evenly divisible by 4."
                )

    # test prints (model arch)
    if args.debug_mode:
        print("model arch:")
        print(
            "mlp top arch "
            + str(ln_top.size - 1)
            + " layers, with input to output dimensions:"
        )
        print(ln_top)
        print("# of interactions")
        print(num_int)
        print(
            "mlp bot arch "
            + str(ln_bot.size - 1)
            + " layers, with input to output dimensions:"
        )
        print(ln_bot)
        print("# of features (sparse and dense)")
        print(num_fea)
        print("dense feature size")
        print(m_den)
        print("sparse feature size")
        print(m_spa)
        print(
            "# of embeddings (= # of sparse features) "
            + str(ln_emb.size)
            + ", with dimensions "
            + str(m_spa)
            + "x:"
        )
        print(ln_emb)

        print("data (inputs and targets):")
        for j, inputBatch in enumerate(train_ld):
            X, lS_o, lS_i, T, W, CBPP = unpack_batch(inputBatch)

            torch.set_printoptions(precision=4)
            # early exit if nbatches was set by the user and has been exceeded
            if nbatches > 0 and j >= nbatches:
                break
            print("mini-batch: %d" % j)
            print(X.detach().cpu())
            # transform offsets to lengths when printing
            print(
                torch.IntTensor(
                    [
                        np.diff(
                            S_o.detach().cpu().tolist() + list(lS_i[i].shape)
                        ).tolist()
                        for i, S_o in enumerate(lS_o)
                    ]
                )
            )
            print([S_i.detach().cpu() for S_i in lS_i])
            print(T.detach().cpu())

    global ndevices
    ndevices = min(ngpus, args.mini_batch_size, num_fea - 1) if use_gpu else -1

    ### construct the neural network specified above ###
    # WARNING: to obtain exactly the same initialization for
    # the weights we need to start from the same random seed.
    # np.random.seed(args.numpy_rand_seed)
    global dlrm
    dlrm = DLRM_Net(
        m_spa,
        ln_emb,
        ln_bot,
        ln_top,
        arch_interaction_op=args.arch_interaction_op,
        arch_interaction_itself=args.arch_interaction_itself,
        sigmoid_bot=-1,
        sigmoid_top=ln_top.size - 2,
        sync_dense_params=args.sync_dense_params,
        loss_threshold=args.loss_threshold,
        ndevices=ndevices,
        qr_flag=args.qr_flag,
        qr_operation=args.qr_operation,
        qr_collisions=args.qr_collisions,
        qr_threshold=args.qr_threshold,
        md_flag=args.md_flag,
        md_threshold=args.md_threshold,
        weighted_pooling=args.weighted_pooling,
        loss_function=args.loss_function,
        learning_rate=args.learning_rate,
        use_gpu=use_gpu,
        use_fbgemm_gpu=use_fbgemm_gpu,
        fbgemm_gpu_codegen_pref=args.fbgemm_gpu_codegen_pref,
        inference_only=args.inference_only,
        quantize_mlp_with_bit=args.quantize_mlp_with_bit,
        quantize_emb_with_bit=args.quantize_emb_with_bit,
    )

    # test prints
    if args.debug_mode:
        print("initial parameters (weights and bias):")
        dlrm.print_weights()

    # In dlrm.quantize_embedding called below, the torch quantize calls run
    # on cpu tensors only. They cannot quantize tensors stored on the gpu.
    # So quantization occurs on cpu tensors before transferring them to gpu if
    # use_gpu is enabled.
    if args.quantize_emb_with_bit != 32:
        dlrm.quantize_embedding(args.quantize_emb_with_bit)

    if not args.inference_only:
        assert args.quantize_mlp_with_bit == 32, (
            "Dynamic quantization for mlp requires "
            + "--inference-only because training is not supported"
        )
    else:
        # Currently only INT8 and FP16 quantized types are supported for quantized MLP inference.
        # By default we don't do the quantization: quantize_{mlp,emb}_with_bit == 32 (FP32)
        assert args.quantize_mlp_with_bit in [
            8,
            16,
            32,
        ], "only support 8/16/32-bit but got {}".format(args.quantize_mlp_with_bit)

        if args.quantize_mlp_with_bit != 32:
            assert not use_gpu, (
                "Cannot run dynamic quantization for mlp "
                + "with --use-gpu enabled, because DynamicQuantizedLinear's "
                + "forward call calls 'quantized::linear_dynamic', which cannot "
                + "run with arguments from the 'CUDA' backend."
            )
            if args.quantize_mlp_with_bit in [8]:
                quantize_dtype = torch.qint8
            else:
                quantize_dtype = torch.float16
            dlrm.top_l = torch.quantization.quantize_dynamic(
                dlrm.top_l, {torch.nn.Linear}, quantize_dtype
            )
            dlrm.bot_l = torch.quantization.quantize_dynamic(
                dlrm.bot_l, {torch.nn.Linear}, quantize_dtype
            )

    # Prep work for embedding tables and model transfer:
    # Handling single-cpu and single-gpu modes
    # NOTE: This also handles dist-backend modes (CLI args --dist-backend=nccl,
    # --dist-backend=ccl, and --dist-backend=mpi) because in these modes each
    # process runs in single-gpu mode. For example, if 8 processes are launched
    # running dlrm_s_pytorch.py with --dist-backend=nccl --use-gpu, each process
    # will run in single-gpu mode, resulting in 8 gpus total running distributed
    # training or distributed inference if --inference-only is enabled.
    if dlrm.ndevices_available <= 1:
        if use_fbgemm_gpu:
            dlrm.fbgemm_emb_l = nn.ModuleList(
                [
                    fbgemm_gpu_emb_bag_wrapper(
                        device,
                        dlrm.emb_l if dlrm.emb_l else dlrm.emb_l_q,
                        dlrm.m_spa,
                        dlrm.quantize_bits,
                        dlrm.learning_rate,
                        dlrm.fbgemm_gpu_codegen_pref,
                        dlrm.requires_grad,
                    )
                ]
            )
        if use_gpu:
            dlrm = dlrm.to(device)
            if dlrm.weighted_pooling == "fixed":
                for k, w in enumerate(dlrm.v_W_l):
                    dlrm.v_W_l[k] = w.cuda()
    else:
        # Handing Multi-gpu mode
        dlrm.bot_l = dlrm.bot_l.to(device)
        dlrm.top_l = dlrm.top_l.to(device)
        dlrm.prepare_parallel_model(ndevices)

    if args.use_torch2trt_for_mlp:
        if torch2trt and use_gpu and args.inference_only and args.quantize_mlp_with_bit == 32:
            bot_l_sample_input = torch.ones([1, ln_bot[0]], dtype=torch.float32).cuda()
            top_l_sample_input = torch.ones([1, ln_top[0]], dtype=torch.float32).cuda()
            dlrm.bot_l = torch2trt.torch2trt(dlrm.bot_l, (bot_l_sample_input,))
            dlrm.top_l = torch2trt.torch2trt(dlrm.top_l, (top_l_sample_input,))
        elif torch2trt is None:
            sys.exit("\ntorch2trt module failed to import.\n\n" + torch2trt_import_error_msg)
        else:
            error_msg = "ERROR: When --use-torch2trt-for-mlp is enabled, "
            if not use_gpu:
                error_msg += "--use-gpu must be enabled, "
            if not args.inference_only:
                error_msg += "--inference-only must be enabled, "
            if args.quantize_mlp_with_bit != 32:
                error_msg += "--quantize-mlp-with-bit must be disabled. "
            error_msg = error_msg[:-2] + "."
            sys.exit(error_msg)

    # distribute data parallel mlps
    if ext_dist.my_size > 1:
        if use_gpu:
            device_ids = [ext_dist.my_local_rank]
            dlrm.bot_l = ext_dist.DDP(dlrm.bot_l, device_ids=device_ids)
            dlrm.top_l = ext_dist.DDP(dlrm.top_l, device_ids=device_ids)
        else:
            dlrm.bot_l = ext_dist.DDP(dlrm.bot_l)
            dlrm.top_l = ext_dist.DDP(dlrm.top_l)

    if not args.inference_only:
        # specify the optimizer algorithm
        opts = {
            "sgd": torch.optim.SGD,
            "rwsadagrad": RowWiseSparseAdagrad.RWSAdagrad,
            "adagrad": apex.optimizers.FusedAdagrad
            if apex
            else torch.optim.Adagrad,
        }

        parameters = (
            dlrm.parameters()
            if ext_dist.my_size == 1
            else [
                {
                    "params": [
                        p
                        for emb in (
                            [e.fbgemm_gpu_emb_bag for e in dlrm.fbgemm_emb_l]
                            if use_fbgemm_gpu
                            else dlrm.emb_l_q
                            if dlrm.quantize_bits != 32
                            else dlrm.emb_l
                        )
                        for p in emb.parameters()
                    ],
                    "lr": args.learning_rate,
                },
                # TODO check this lr setup
                # bottom mlp has no data parallelism
                # need to check how do we deal with top mlp
                {
                    "params": dlrm.bot_l.parameters(),
                    "lr": args.learning_rate,
                },
                {
                    "params": dlrm.top_l.parameters(),
                    "lr": args.learning_rate,
                },
            ]
        )
        optimizer = opts[args.optimizer](parameters, lr=args.learning_rate)
        lr_scheduler = LRPolicyScheduler(
            optimizer,
            args.lr_num_warmup_steps,
            args.lr_decay_start_step,
            args.lr_num_decay_steps,
        )

    # Guarantee GPU setup has completed before training or inference starts.
    if use_gpu:
        torch.cuda.synchronize()

    ### main loop ###

    # training or inference
    best_acc_test = 0
    best_auc_test = 0
    skip_upto_epoch = 0
    skip_upto_batch = 0
    total_time = 0
    total_loss = 0
    total_iter = 0
    total_samp = 0

    if args.mlperf_logging:
        mlperf_logger.mlperf_submission_log("dlrm")
        mlperf_logger.log_event(
            key=mlperf_logger.constants.SEED, value=args.numpy_rand_seed
        )
        mlperf_logger.log_event(
            key=mlperf_logger.constants.GLOBAL_BATCH_SIZE, value=args.mini_batch_size
        )

    # Load model is specified
    if not (args.load_model == ""):
        print("Loading saved model {}".format(args.load_model))
        if use_gpu:
            if dlrm.ndevices_available > 1:
                # NOTE: when targeting inference on multiple GPUs,
                # load the model as is on CPU or GPU, with the move
                # to multiple GPUs to be done in parallel_forward
                ld_model = torch.load(args.load_model)
            else:
                # NOTE: when targeting inference on single GPU,
                # note that the call to .to(device) has already happened
                ld_model = torch.load(
                    args.load_model,
                    map_location=torch.device("cuda")
                    # map_location=lambda storage, loc: storage.cuda(0)
                )
        else:
            # when targeting inference on CPU
            ld_model = torch.load(args.load_model, map_location=torch.device("cpu"))
        dlrm.load_state_dict(ld_model["state_dict"])
        ld_j = ld_model["iter"]
        ld_k = ld_model["epoch"]
        ld_nepochs = ld_model["nepochs"]
        ld_nbatches = ld_model["nbatches"]
        ld_nbatches_test = ld_model["nbatches_test"]
        ld_train_loss = ld_model["train_loss"]
        ld_total_loss = ld_model["total_loss"]
        if args.mlperf_logging:
            ld_gAUC_test = ld_model["test_auc"]
        ld_acc_test = ld_model["test_acc"]
        if not args.inference_only:
            optimizer.load_state_dict(ld_model["opt_state_dict"])
            best_acc_test = ld_acc_test
            total_loss = ld_total_loss
            skip_upto_epoch = ld_k  # epochs
            skip_upto_batch = ld_j  # batches
        else:
            args.print_freq = ld_nbatches
            args.test_freq = 0

        print(
            "Saved at: epoch = {:d}/{:d}, batch = {:d}/{:d}, ntbatch = {:d}".format(
                ld_k, ld_nepochs, ld_j, ld_nbatches, ld_nbatches_test
            )
        )
        print(
            "Training state: loss = {:.6f}".format(
                ld_train_loss,
            )
        )
        if args.mlperf_logging:
            print(
                "Testing state: accuracy = {:3.3f} %, auc = {:.3f}".format(
                    ld_acc_test * 100, ld_gAUC_test
                )
            )
        else:
            print("Testing state: accuracy = {:3.3f} %".format(ld_acc_test * 100))

    print("time/loss/accuracy (if enabled):")

    if args.mlperf_logging:
        # LR is logged twice for now because of a compliance checker bug
        mlperf_logger.log_event(
            key=mlperf_logger.constants.OPT_BASE_LR, value=args.learning_rate
        )
        mlperf_logger.log_event(
            key=mlperf_logger.constants.OPT_LR_WARMUP_STEPS,
            value=args.lr_num_warmup_steps,
        )

        # use logging keys from the official HP table and not from the logging library
        mlperf_logger.log_event(
            key="sgd_opt_base_learning_rate", value=args.learning_rate
        )
        mlperf_logger.log_event(
            key="lr_decay_start_steps", value=args.lr_decay_start_step
        )
        mlperf_logger.log_event(
            key="sgd_opt_learning_rate_decay_steps", value=args.lr_num_decay_steps
        )
        mlperf_logger.log_event(key="sgd_opt_learning_rate_decay_poly_power", value=2)

    tb_file = "./" + args.tensor_board_filename
    writer = SummaryWriter(tb_file)

    # Pre-cache samples.
    if args.precache_ml_data:
        for _ in (test_ld if args.inference_only else train_ld):
            pass

    ext_dist.barrier()
    with torch.autograd.profiler.profile(
        args.enable_profiling, use_cuda=use_gpu, record_shapes=True
    ) as prof:

        if not args.inference_only:

            if args.fb5logger is not None:
                fb5logger = FB5Logger(args.fb5logger)
                fb5logger.header("DLRM", "OOTB", "train", args.fb5config, score_metric=loggerconstants.EXPS)
            
            k = 0
            while k < args.nepochs:
                if args.mlperf_logging:
                    mlperf_logger.barrier()
                    mlperf_logger.log_start(
                        key=mlperf_logger.constants.BLOCK_START,
                        metadata={
                            mlperf_logger.constants.FIRST_EPOCH_NUM: (k + 1),
                            mlperf_logger.constants.EPOCH_COUNT: 1,
                        },
                    )
                    mlperf_logger.barrier()
                    mlperf_logger.log_start(
                        key=mlperf_logger.constants.EPOCH_START,
                        metadata={mlperf_logger.constants.EPOCH_NUM: (k + 1)},
                    )

                if k < skip_upto_epoch:
                    continue

                if args.print_accumulated_time:
                    accum_time_begin = time_wrap(use_gpu)

                if args.mlperf_logging:
                    previous_iteration_time = None

                for j, inputBatch in enumerate(train_ld):
                    if j == 0 and args.save_onnx:
                        X_onnx, lS_o_onnx, lS_i_onnx, _, _, _ = unpack_batch(inputBatch)

                    if j < skip_upto_batch:
                        continue

                    if k == 0 and j == args.warmup_steps and args.fb5logger is not None:
                        fb5logger.run_start()

                    X, lS_o, lS_i, T, W, CBPP = unpack_batch(inputBatch)

                    if args.mlperf_logging:
                        current_time = time_wrap(use_gpu)
                        if previous_iteration_time:
                            iteration_time = current_time - previous_iteration_time
                        else:
                            iteration_time = 0
                        previous_iteration_time = current_time
                    else:
                        t1 = time_wrap(use_gpu)

                    # early exit if nbatches was set by the user and has been exceeded
                    if nbatches > 0 and j >= nbatches:
                        break

                    # Skip the batch if batch size not multiple of total ranks
                    if ext_dist.my_size > 1 and X.size(0) % ext_dist.my_size != 0:
                        print(
                            "Warning: Skiping the batch %d with size %d"
                            % (j, X.size(0))
                        )
                        continue

                    mbs = T.shape[0]  # = args.mini_batch_size except maybe for last

                    # forward pass
                    Z = dlrm_wrap(
                        X,
                        lS_o,
                        lS_i,
                        use_gpu,
                        device,
                        ndevices=ndevices,
                    )

                    if ext_dist.my_size > 1:
                        T = T[ext_dist.get_my_slice(mbs)]
                        W = W[ext_dist.get_my_slice(mbs)]

                    # loss
                    E = loss_fn_wrap(Z, T, use_gpu, device)

                    # compute loss and accuracy
                    L = E.detach().cpu().numpy()  # numpy array
                    # training accuracy is not disabled
                    # S = Z.detach().cpu().numpy()  # numpy array
                    # T = T.detach().cpu().numpy()  # numpy array

                    # # print("res: ", S)

                    # # print("j, train: BCE", j, L)

                    # mbs = T.shape[0]  # = args.mini_batch_size except maybe for last
                    # A = np.sum((np.round(S, 0) == T).astype(np.uint8))

                    with record_function("DLRM backward"):
                        # Update optimizer parameters to train weights instantiated lazily in
                        # the parallel_forward call.
                        if dlrm.ndevices_available > 1 and dlrm.add_new_weights_to_params:

                            # Pop any prior extra parameters. Priors may exist because
                            # self.parallel_model_is_not_prepared is set back to True
                            # when self.parallel_model_batch_size != batch_size.
                            # Search "self.parallel_model_batch_size != batch_size" in code.
                            if "lazy_params" in optimizer.param_groups[-1].keys():
                                optimizer.param_groups.pop()

                            # dlrm.v_W_l_l is a list of nn.ParameterLists, one ParameterList per gpu.
                            # Flatten the list of nn.ParameterList to one nn.ParameterList,
                            # and add it to the trainable params list.
                            lazy_params = nn.ParameterList()
                            if dlrm.weighted_pooling == "learned":
                                lazy_params.extend(
                                    nn.ParameterList(
                                        [p for p_l in dlrm.v_W_l_l for p in p_l]
                                    )
                                )
                            if dlrm.use_fbgemm_gpu:
                                lazy_params.extend(
                                    nn.ParameterList(
                                        [
                                            emb
                                            for emb_ in dlrm.fbgemm_emb_l
                                            for emb in emb_.fbgemm_gpu_emb_bag.parameters()
                                        ]
                                    )
                                )
                            lazy_params_dict = optimizer.param_groups[0]
                            lazy_params_dict["lazy_params"] = True
                            lazy_params_dict["params"] = lazy_params
                            optimizer.param_groups.append(lazy_params_dict)
                            dlrm.add_new_weights_to_params = False
                            # Run "[[t.device.type for t in grp['params']] for grp in optimizer.param_groups]"
                            # to view devices used by tensors in the param groups.

                        # scaled error gradient propagation
                        # (where we do not accumulate gradients across mini-batches)
                        if (
                            args.mlperf_logging
                            and (j + 1) % args.mlperf_grad_accum_iter == 0
                        ) or not args.mlperf_logging:
                            optimizer.zero_grad()
                        # backward pass
                        E.backward()

                        # optimizer
                        if (
                            args.mlperf_logging
                            and (j + 1) % args.mlperf_grad_accum_iter == 0
                        ) or not args.mlperf_logging:
                            optimizer.step()
                            lr_scheduler.step()

                    if args.mlperf_logging:
                        total_time += iteration_time
                    else:
                        t2 = time_wrap(use_gpu)
                        total_time += t2 - t1

                    total_loss += L * mbs
                    total_iter += 1
                    total_samp += mbs

                    should_print = ((j + 1) % args.print_freq == 0) or (
                        j + 1 == nbatches
                    )
                    should_test = (
                        (args.test_freq > 0)
                        and (args.data_generation in ["dataset", "random"])
                        and (((j + 1) % args.test_freq == 0) or (j + 1 == nbatches))
                    )

                    # print time, loss and accuracy
                    if should_print or should_test:
                        gT = 1000.0 * total_time / total_iter if args.print_time else -1
                        total_time = 0

                        train_loss = total_loss / total_samp
                        total_loss = 0

                        str_run_type = (
                            "inference" if args.inference_only else "training"
                        )

                        wall_time = ""
                        if args.print_wall_time:
                            wall_time = " ({})".format(time.strftime("%H:%M"))

                        print(
                            "Finished {} it {}/{} of epoch {}, {:.2f} ms/it,".format(
                                str_run_type, j + 1, nbatches, k, gT
                            )
                            + " loss {:.6f}".format(train_loss)
                            + wall_time,
                            flush=True,
                        )

                        if args.print_accumulated_time and ext_dist.my_rank < 2:
                            current_unix_time = time_wrap(use_gpu)
                            ext_dist.orig_print(
                                "Accumulated time so far: {} for process {} for step {} at {}".format(
                                    current_unix_time - accum_time_begin,
                                    ext_dist.my_rank,
                                    j + 1,
                                    current_unix_time,
                                )
                            )

                        log_iter = nbatches * k + j + 1
                        writer.add_scalar("Train/Loss", train_loss, log_iter)

                        total_iter = 0
                        total_samp = 0

                    # testing
                    if should_test:
                        epoch_num_float = (j + 1) / len(train_ld) + k + 1
                        if args.mlperf_logging:
                            mlperf_logger.barrier()
                            mlperf_logger.log_start(
                                key=mlperf_logger.constants.EVAL_START,
                                metadata={
                                    mlperf_logger.constants.EPOCH_NUM: epoch_num_float
                                },
                            )

                        # don't measure training iter time in a test iteration
                        if args.mlperf_logging:
                            previous_iteration_time = None
                        print(
                            "Testing at - {}/{} of epoch {},".format(j + 1, nbatches, k)
                        )
                        model_metrics_dict, is_best = inference(
                            args,
                            dlrm,
                            best_acc_test,
                            best_auc_test,
                            test_ld,
                            device,
                            use_gpu,
                            log_iter,
                        )

                        if (
                            is_best
                            and not (args.save_model == "")
                            and not args.inference_only
                        ):
                            model_metrics_dict["epoch"] = k
                            model_metrics_dict["iter"] = j + 1
                            model_metrics_dict["train_loss"] = train_loss
                            model_metrics_dict["total_loss"] = total_loss
                            model_metrics_dict[
                                "opt_state_dict"
                            ] = optimizer.state_dict()
                            print("Saving model to {}".format(args.save_model))
                            torch.save(model_metrics_dict, args.save_model)

                        if args.mlperf_logging:
                            mlperf_logger.barrier()
                            mlperf_logger.log_end(
                                key=mlperf_logger.constants.EVAL_STOP,
                                metadata={
                                    mlperf_logger.constants.EPOCH_NUM: epoch_num_float
                                },
                            )

                        # Uncomment the line below to print out the total time with overhead
                        # print("Total test time for this group: {}" \
                        # .format(time_wrap(use_gpu) - accum_test_time_begin))

                        if (
                            args.mlperf_logging
                            and (args.mlperf_acc_threshold > 0)
                            and (best_acc_test > args.mlperf_acc_threshold)
                        ):
                            print(
                                "MLPerf testing accuracy threshold "
                                + str(args.mlperf_acc_threshold)
                                + " reached, stop training"
                            )
                            break

                        if (
                            args.mlperf_logging
                            and (args.mlperf_auc_threshold > 0)
                            and (best_auc_test > args.mlperf_auc_threshold)
                        ):
                            print(
                                "MLPerf testing auc threshold "
                                + str(args.mlperf_auc_threshold)
                                + " reached, stop training"
                            )
                            if args.mlperf_logging:
                                mlperf_logger.barrier()
                                mlperf_logger.log_end(
                                    key=mlperf_logger.constants.RUN_STOP,
                                    metadata={
                                        mlperf_logger.constants.STATUS: mlperf_logger.constants.SUCCESS
                                    },
                                )
                            break
                if k == 0 and args.fb5logger is not None:
                    fb5logger.run_stop(nbatches - args.warmup_steps, args.mini_batch_size)
                    
                if args.mlperf_logging:
                    mlperf_logger.barrier()
                    mlperf_logger.log_end(
                        key=mlperf_logger.constants.EPOCH_STOP,
                        metadata={mlperf_logger.constants.EPOCH_NUM: (k + 1)},
                    )
                    mlperf_logger.barrier()
                    mlperf_logger.log_end(
                        key=mlperf_logger.constants.BLOCK_STOP,
                        metadata={mlperf_logger.constants.FIRST_EPOCH_NUM: (k + 1)},
                    )
                k += 1  # nepochs
            if args.mlperf_logging and best_auc_test <= args.mlperf_auc_threshold:
                mlperf_logger.barrier()
                mlperf_logger.log_end(
                    key=mlperf_logger.constants.RUN_STOP,
                    metadata={
                        mlperf_logger.constants.STATUS: mlperf_logger.constants.ABORTED
                    },
                )
        else:
            print("Testing for inference only")
            inference(
                args,
                dlrm,
                best_acc_test,
                best_auc_test,
                test_ld,
                device,
                use_gpu,
            )

    # profiling
    if args.enable_profiling:
        time_stamp = str(datetime.datetime.now()).replace(" ", "_")
        with open("dlrm_s_pytorch" + time_stamp + "_shape.prof", "w") as prof_f:
            prof_f.write(
                prof.key_averages(group_by_input_shape=True).table(
                    sort_by="self_cpu_time_total"
                )
            )
        with open("dlrm_s_pytorch" + time_stamp + "_total.prof", "w") as prof_f:
            prof_f.write(prof.key_averages().table(sort_by="self_cpu_time_total"))
        prof.export_chrome_trace("dlrm_s_pytorch" + time_stamp + ".json")
        # print(prof.key_averages().table(sort_by="cpu_time_total"))

    # plot compute graph
    if args.plot_compute_graph:
        sys.exit(
            "ERROR: Please install pytorchviz package in order to use the"
            + " visualization. Then, uncomment its import above as well as"
            + " three lines below and run the code again."
        )
        # V = Z.mean() if args.inference_only else E
        # dot = make_dot(V, params=dict(dlrm.named_parameters()))
        # dot.render('dlrm_s_pytorch_graph') # write .pdf file

    # test prints
    if not args.inference_only and args.debug_mode:
        print("updated parameters (weights and bias):")
        dlrm.print_weights()

    # export the model in onnx
    if args.save_onnx:
        """
        # workaround 1: tensor -> list
        if torch.is_tensor(lS_i_onnx):
            lS_i_onnx = [lS_i_onnx[j] for j in range(len(lS_i_onnx))]
        # workaound 2: list -> tensor
        lS_i_onnx = torch.stack(lS_i_onnx)
        """
        # debug prints
        # print("inputs", X_onnx, lS_o_onnx, lS_i_onnx)
        # print("output", dlrm_wrap(X_onnx, lS_o_onnx, lS_i_onnx, use_gpu, device))
        dlrm_pytorch_onnx_file = "dlrm_s_pytorch.onnx"
        print("X_onnx.shape", X_onnx.shape)
        if torch.is_tensor(lS_o_onnx):
            print("lS_o_onnx.shape", lS_o_onnx.shape)
        else:
            for oo in lS_o_onnx:
                print("oo.shape", oo.shape)
        if torch.is_tensor(lS_i_onnx):
            print("lS_i_onnx.shape", lS_i_onnx.shape)
        else:
            for ii in lS_i_onnx:
                print("ii.shape", ii.shape)

        # name inputs and outputs
        o_inputs = (
            ["offsets"]
            if torch.is_tensor(lS_o_onnx)
            else ["offsets_" + str(i) for i in range(len(lS_o_onnx))]
        )
        i_inputs = (
            ["indices"]
            if torch.is_tensor(lS_i_onnx)
            else ["indices_" + str(i) for i in range(len(lS_i_onnx))]
        )
        all_inputs = ["dense_x"] + o_inputs + i_inputs
        # debug prints
        print("inputs", all_inputs)

        # create dynamic_axis dictionaries
        do_inputs = (
            [{"offsets": {1: "batch_size"}}]
            if torch.is_tensor(lS_o_onnx)
            else [
                {"offsets_" + str(i): {0: "batch_size"}} for i in range(len(lS_o_onnx))
            ]
        )
        di_inputs = (
            [{"indices": {1: "batch_size"}}]
            if torch.is_tensor(lS_i_onnx)
            else [
                {"indices_" + str(i): {0: "batch_size"}} for i in range(len(lS_i_onnx))
            ]
        )
        dynamic_axes = {"dense_x": {0: "batch_size"}, "pred": {0: "batch_size"}}
        for do in do_inputs:
            dynamic_axes.update(do)
        for di in di_inputs:
            dynamic_axes.update(di)
        # debug prints
        print(dynamic_axes)
        # export model
        torch.onnx.export(
            dlrm,
            (X_onnx, lS_o_onnx, lS_i_onnx),
            dlrm_pytorch_onnx_file,
            verbose=True,
            use_external_data_format=True,
            opset_version=11,
            input_names=all_inputs,
            output_names=["pred"],
            dynamic_axes=dynamic_axes,
        )
        # recover the model back
        dlrm_pytorch_onnx = onnx.load("dlrm_s_pytorch.onnx")
        # check the onnx model
        onnx.checker.check_model(dlrm_pytorch_onnx)
    total_time_end = time_wrap(use_gpu)


if __name__ == "__main__":
    run()