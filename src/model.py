# Copyright 2021 Huawei Technologies Co., Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ============================================================================

"""According to the GRU operator source code and the AUGRU operator formula in the paper
the AUGRU operator is customized"""

from mindspore.parallel._utils import (_get_device_num, _get_gradients_mean, _get_parallel_mode)
from mindspore import nn
from mindspore import ops as P
from mindspore.context import ParallelMode
from mindspore.nn import DistributedGradReducer
from mindspore.ops import functional as F
from mindspore import dtype as mstype
import mindspore.common.initializer as init
from mindspore.common.initializer import Zero
from mindspore import Tensor
from mindspore import numpy as np
from mindspore import ParameterTuple

from src.gru import AUGRU, GRU


class EmbeddingLayer(nn.Cell):
    """embedding layer"""

    def __init__(self, n_uid, n_mid, n_cat, embedding_size):
        super(EmbeddingLayer, self).__init__()
        self.embedding_uid = nn.Embedding(vocab_size=n_uid, embedding_size=embedding_size,
                                          use_one_hot=False, embedding_table='xavier_uniform')
        self.embedding_mid = nn.Embedding(n_mid, embedding_size, False, 'xavier_uniform')
        self.embedding_cat = nn.Embedding(n_cat, embedding_size, False,
                                          'xavier_uniform')
        self.concat1 = P.Concat(axis=1)
        self.concat2 = P.Concat(axis=2)
        self.concat_neg1 = P.Concat(axis=-1)
        self.reducesum = P.ReduceSum()
        self.reshape = P.Reshape()
        self.shape = P.Shape()

    def construct(self, uid_batch_ph, mid_batch_ph,
                  cat_batch_ph, mid_his_batch_ph, cat_his_batch_ph, noclk_mid_batch_ph=None, noclk_cat_batch_ph=None):
        """# User ID Sequence"""
        uid_batch_embedded = self.embedding_uid(uid_batch_ph)
        # Item ID Sequence
        mid_batch_embedded = self.embedding_mid(mid_batch_ph)
        mid_his_batch_embedded = self.embedding_mid(mid_his_batch_ph)
        # Item Category Sequence
        cat_batch_embedded = self.embedding_cat(cat_batch_ph)
        cat_his_batch_embedded = self.embedding_cat(cat_his_batch_ph)

        # Splice embedding, combine various embedding vectors
        # Positive samples contain item and category, concat the Item ID Sequence and Item Category Sequence
        item_eb = self.concat1([mid_batch_embedded, cat_batch_embedded])
        item_his_eb = self.concat2([mid_his_batch_embedded, cat_his_batch_embedded])
        item_his_eb_sum = self.reducesum(item_his_eb, 1)
        if noclk_mid_batch_ph is not None and noclk_cat_batch_ph is not None:
            noclk_mid_his_batch_embedded = self.embedding_mid(noclk_mid_batch_ph)
            noclk_cat_his_batch_embedded = self.embedding_cat(noclk_cat_batch_ph)
            noclk_item_his_eb = self.concat_neg1(
                [noclk_mid_his_batch_embedded[:, :, 0, :], noclk_cat_his_batch_embedded[:, :, 0, :]])
            noclk_item_his_eb = self.reshape(noclk_item_his_eb,
                                             (-1, self.shape(noclk_mid_his_batch_embedded)[1],
                                              36))
            noclk_his_eb = self.concat_neg1([noclk_mid_his_batch_embedded, noclk_cat_his_batch_embedded])
            noclk_his_eb_sum_1 = self.reducesum(noclk_his_eb, 2)
            noclk_his_eb_sum = self.reducesum(noclk_his_eb_sum_1, 1)
            noclk_his_eb_sum = noclk_his_eb_sum
            return uid_batch_embedded, item_eb, item_his_eb, item_his_eb_sum, noclk_item_his_eb
        return uid_batch_embedded, item_eb, item_his_eb, item_his_eb_sum


class auxiliary_loss(nn.Cell):
    """auxiliary loss"""

    def __init__(self):
        super(auxiliary_loss, self).__init__()
        self.bn1 = nn.BatchNorm1d(num_features=72, eps=1e-3, momentum=0.99)
        self.sigmoid = nn.Sigmoid()
        self.softmax = nn.Softmax()
        self.cast = P.Cast()
        self.concat_neg1 = P.Concat(axis=-1)
        self.concat0 = P.Concat(axis=0)
        self.expand_dims = P.ExpandDims()
        self.reshape = P.Reshape()
        self.shape = P.Shape()
        self.log = P.Log()
        self.reducemean = P.ReduceMean()
        self.denselayer1 = nn.Dense(in_channels=72, out_channels=100, weight_init='xavier_uniform')
        self.denselayer2 = nn.Dense(in_channels=100, out_channels=50, weight_init='xavier_uniform')
        self.denselayer3 = nn.Dense(in_channels=50, out_channels=2, weight_init='xavier_uniform')

    def auxiliary_net(self, in_):
        y_hat = self.bn1(in_)
        y_hat = self.denselayer1(y_hat)
        y_hat = self.sigmoid(y_hat)
        y_hat = self.denselayer2(y_hat)
        y_hat = self.sigmoid(y_hat)
        y_hat = self.denselayer3(y_hat)
        y_hat = self.softmax(y_hat) + 0.00000001
        return y_hat

    def construct(self, h_states, click_seq, noclick_seq, mask):
        """construct"""
        mask = self.cast(mask, mstype.float32)
        # concat the end dimension
        click_input_ = self.concat_neg1([h_states, click_seq])
        noclick_input_ = self.concat_neg1([h_states, noclick_seq])
        click_prop_list = []
        noclick_prop_list = []
        for i in range(128):
            click_prop_item = self.auxiliary_net(click_input_[i])
            noclick_prop_item = self.auxiliary_net(noclick_input_[i])
            click_prop_list.append(self.expand_dims(click_prop_item, 0))
            noclick_prop_list.append(self.expand_dims(noclick_prop_item, 0))
        click_prop_ = click_prop_list[0]
        noclick_prop_ = noclick_prop_list[0]
        for i in range(127):
            click_prop_ = self.concat0((click_prop_, click_prop_list[i + 1]))
            noclick_prop_ = self.concat0((noclick_prop_, noclick_prop_list[i + 1]))
        # Get the last y_hat of positive sample
        click_prop_ = click_prop_[:, :, 0]
        # Get the last y_hat of negative sample
        noclick_prop_ = noclick_prop_[:, :, 0]
        # calculate log loss, and mask the real historical behavior
        click_loss_ = -self.reshape(self.log(click_prop_), (-1, self.shape(click_seq)[1])) * mask
        noclick_loss_ = -self.reshape(self.log(1.0 - noclick_prop_), (-1, self.shape(noclick_seq)[1])) * mask
        loss_ = self.reducemean(click_loss_ + noclick_loss_)
        return loss_


class din_fcn_attention(nn.Cell):
    """din_fcn_attention"""

    def __init__(self):
        super(din_fcn_attention, self).__init__()
        self.concat2 = P.Concat(axis=2)
        self.expand_dims = P.ExpandDims()
        self.transpose = P.Transpose()
        self.equal = P.Equal()
        self.prelu = nn.PReLU(w=0.1)
        self.tile = P.Tile()
        self.reshape = P.Reshape()
        self.concat_neg1 = P.Concat(axis=-1)
        self.shape = P.Shape()
        self.ones_like = P.OnesLike()
        self.matmul = P.MatMul()
        self.softmax = P.Softmax()
        self.dense0 = nn.Dense(in_channels=36, out_channels=36, weight_init='xavier_uniform')
        self.dense1 = nn.Dense(in_channels=144, out_channels=80, weight_init='xavier_uniform')
        self.dense2 = nn.Dense(in_channels=80, out_channels=40, weight_init='xavier_uniform')
        self.dense3 = nn.Dense(in_channels=40, out_channels=1, weight_init='xavier_uniform')

    def construct(self, query, facts, attension_size, mask, mode=0, softmax_stag=1, time_major=False,
                  return_alphas=False, forCnn=False):
        """# In case of Bi-RNN, concatenate the forward and the backward RNN outputs."""
        if isinstance(facts, tuple):
            facts = self.concat2(facts)
        if len(facts.shape) == 2:
            facts = self.expand_dims(facts, 1)

        if time_major:
            # (T,B,D) => (B,T,D)
            facts = self.transpose(facts, [1, 0, 2])
        # Trainable parameters
        mask = self.equal(mask, self.ones_like(mask))
        query = self.dense0(query)
        query = self.prelu(query)
        queries = self.tile(query, (1, self.shape(facts)[1]))
        queries = self.reshape(queries, self.shape(facts))
        din_all = self.concat_neg1([queries, facts, queries - facts, queries * facts])
        d_layers_1_all = self.dense1(din_all)
        d_layers_2_all = self.dense2(d_layers_1_all)
        d_layers_3_all = self.dense3(d_layers_2_all)
        d_layers_3_all = self.reshape(d_layers_3_all, (-1, 1, self.shape(facts)[1]))
        scores = d_layers_3_all

        # Mask
        # key_masks = tf.sequence_mask(facts_length, tf.shape(facts)[1])   # [B, T]
        key_masks = self.expand_dims(mask, 1)
        paddings = self.ones_like(scores) * (-2 ** 32 + 1)
        # return key_masks, scores, paddings
        if not forCnn:
            scores = np.where(key_masks, scores, paddings)

        # Scale
        # scores = scores / (facts.get_shape().as_list()[-1] ** 0.5)

        # Activation
        if softmax_stag:
            scores = self.softmax(scores)

        # Weighted sum
        if mode == 0:
            output = self.matmul(scores, facts)
            # output=self.reshape(output,[-1,self.shape(facts)[-1]])
        else:
            scores = self.reshape(scores, (-1, self.shape(facts)[1]))
            output = facts * self.expand_dims(scores, -1)
            output = self.reshape(output, self.shape(facts))
        if return_alphas:
            return output, scores
        return output


class fcn_layer(nn.Cell):
    """full connection layer"""

    def __init__(self, use_dice=False):
        super(fcn_layer, self).__init__()
        self.use_dice = use_dice
        self.bn1 = nn.BatchNorm1d(num_features=162, eps=1e-3, momentum=0.99)
        self.dnn1 = nn.Dense(in_channels=162, out_channels=200, weight_init='xavier_uniform')
        self.dnn2 = nn.Dense(in_channels=200, out_channels=80, weight_init='xavier_uniform')
        self.dnn3 = nn.Dense(in_channels=80, out_channels=2, weight_init='xavier_uniform')
        self.softmax = nn.Softmax()
        self.prelu = nn.PReLU(w=0.1)
        self.dice1 = Dice(200)
        self.dice2 = Dice(80)

    def construct(self, y):
        """construct"""
        y = self.bn1(y)
        y = self.dnn1(y)
        if self.use_dice:
            y = self.dice1(y)
        else:
            y = self.prelu(y)
        y = self.dnn2(y)
        if self.use_dice:
            y = self.dice2(y)
        else:
            y = self.prelu(y)
        y = self.dnn3(y)
        y = self.softmax(y) + 0.00000001
        return y


class Dice(nn.Cell):
    """activation function Dice/Prelu"""

    def __init__(self, feat_dim):
        super(Dice, self).__init__()
        self.feat_dim = feat_dim
        self.alphas = init.initializer(Zero(), [feat_dim], mstype.float32)
        self.beta = init.initializer(Zero(), [feat_dim], mstype.float32)
        self.bn = nn.BatchNorm1d(num_features=feat_dim, eps=1e-3, momentum=0.99)
        self.reduce_mean = P.ReduceMean()
        self.reshape = P.Reshape()
        self.square = P.Square()
        self.sqrt = P.Sqrt()
        self.sigmoid = P.Sigmoid()

    def construct(self, _x, axis=-1, epsilon=0.000000001):
        """construct"""
        reduction_axes = []
        for i in range(len(_x.shape) - 1):
            reduction_axes.append(i)
        broadcast_shape = (1,) * (len(_x.shape) - 1)
        broadcast_shape += (self.feat_dim,)

        mean = self.reduce_mean(_x, reduction_axes)
        broadcast_mean = self.reshape(mean, broadcast_shape)
        std = self.reduce_mean(self.square(_x - broadcast_mean) + epsilon, reduction_axes)
        std = self.sqrt(std)
        broadcast_std = self.reshape(std, broadcast_shape)

        x_normed = (_x - broadcast_mean) / (broadcast_std + epsilon)
        x_p = self.sigmoid(x_normed)

        return self.alphas * (1.0 - x_p) * _x + x_p * _x


class DIEN(nn.Cell):
    """DIEN"""

    def __init__(self, n_uid, n_mid, n_cat, embedding_size):
        super(DIEN, self).__init__()
        self.embeddinglayer = EmbeddingLayer(n_uid, n_mid, n_cat, embedding_size)
        self.aux_loss = auxiliary_loss()
        self.attention = din_fcn_attention()
        self.transpose = P.Transpose()
        self.fcn = fcn_layer(use_dice=True)
        self.gru = GRU(input_size=36, hidden_size=36, num_layers=1, has_bias=True, batch_first=True, dropout=0.0,
                       bidirectional=False)
        self.augru = AUGRU(input_size=36, hidden_size=36, num_layers=1, has_bias=True, batch_first=True, dropout=0.0,
                           bidirectional=False)
        self.concat1 = P.Concat(axis=1)
        """# Initialize the initial network state of GRU and AUGRU operators"""
        self.h0 = Tensor(np.zeros([1 * 1, 128, 36]).astype(np.float32))

    def construct(self, mask, uid_batch_ph, mid_batch_ph,
                  cat_batch_ph, mid_his_batch_ph, cat_his_batch_ph, noclk_mid_batch_ph=None,
                  noclk_cat_batch_ph=None
                  ):
        """construct"""
        uid_batch_embedded, item_eb, item_his_eb, item_his_eb_sum, noclk_item_his_eb = self.embeddinglayer(
            uid_batch_ph, mid_batch_ph,
            cat_batch_ph, mid_his_batch_ph, cat_his_batch_ph, noclk_mid_batch_ph, noclk_cat_batch_ph)

        rnn_outputs, hn = self.gru(item_his_eb, self.h0)
        aux_loss = self.aux_loss(rnn_outputs[:, :-1, :], item_his_eb[:, 1:, :], noclk_item_his_eb[:, 1:, :],
                                 mask[:, 1:])
        att_outputs, att_score = self.attention(item_eb, rnn_outputs, 36, mask, mode=1, softmax_stag=1,
                                                time_major=False,
                                                return_alphas=True, forCnn=False)
        att_score = self.transpose(att_score, (1, 0))
        rnn_outputs2, final_state2 = self.augru(att_score, rnn_outputs, self.h0)
        inp = self.concat1([uid_batch_embedded, item_eb, item_his_eb_sum, item_eb * item_his_eb_sum,
                            final_state2[0]])
        y_hat = self.fcn(inp)
        hn = hn
        att_outputs = att_outputs
        rnn_outputs2 = rnn_outputs2
        return y_hat, aux_loss


class Ctr_Loss(nn.Cell):
    """Ctr loss"""

    def __init__(self):
        super(Ctr_Loss, self).__init__()
        self.reducemean = P.ReduceMean()
        self.log = P.Log()

    def construct(self, y_hat, target_ph, aux_loss):
        loss = -self.reducemean(self.log(y_hat) * target_ph) + aux_loss
        return loss


class Accuracy(nn.Cell):
    """accuracy"""

    def __init__(self):
        super(Accuracy, self).__init__()
        self.reduce_mean = P.ReduceMean()
        self.cast = P.Cast()
        self.equal = P.Equal()
        self.round = P.Round()

    def construct(self, y_hat, target):
        accuracy = self.reduce_mean(self.cast(self.equal(self.round(y_hat), target), mstype.float32))
        return accuracy


class CustomWithLossCell(nn.Cell):
    """custom with loss cell"""

    def __init__(self, backbone, loss_fn):
        super(CustomWithLossCell, self).__init__(auto_prefix=False)
        self._backbone = backbone
        self._loss_fn = loss_fn

    def construct(self, mid_mask, uids, mids, cats, mid_his, cat_his, noclk_mids,
                  noclk_cats, target):
        output, aux_loss = self._backbone(mid_mask, uids, mids,
                                          cats, mid_his, cat_his, noclk_mids,
                                          noclk_cats)
        return self._loss_fn(output, target, aux_loss)


class TrainOneStepCell(nn.Cell):
    """train one step cell"""

    def __init__(self, network, optimizer):
        super(TrainOneStepCell, self).__init__(auto_prefix=False)
        self.network = network
        self.weights = ParameterTuple(network.trainable_params())
        self.optimizer = optimizer
        self.grad = P.GradOperation(get_by_list=True)
        self.loss = Ctr_Loss()
        self.reduce_flag = False
        self.grad_reducer = F.identity
        self.parallel_mode = _get_parallel_mode()
        self.reducer_flag = self.parallel_mode in (ParallelMode.DATA_PARALLEL, ParallelMode.HYBRID_PARALLEL)
        if self.reducer_flag:
            self.mean = _get_gradients_mean()
            self.degree = _get_device_num()
            self.grad_reducer = DistributedGradReducer(self.weights, self.mean, self.degree)

    def construct(self, mid_mask, uids, mids, cats, mid_his, cat_his, noclk_mids,
                  noclk_cats, target):
        weights = self.weights
        loss = self.network(mid_mask, uids, mids, cats, mid_his, cat_his, noclk_mids,
                            noclk_cats, target)
        grads = self.grad(self.network, weights)(mid_mask, uids, mids, cats, mid_his, cat_his, noclk_mids,
                                                 noclk_cats, target)
        grads = self.grad_reducer(grads)
        loss = F.depend(loss, self.optimizer(grads))
        return loss
