#!/usr/bin/env python

# Copyright 2017 Johns Hopkins University (Shinji Watanabe)
#  Apache 2.0  (http://www.apache.org/licenses/LICENSE-2.0)

# This code is ported from the following implementation written in Torch.
# https://github.com/chainer/chainer/blob/master/examples/ptb/train_ptb_custom_loop.py

from __future__ import division
from __future__ import print_function

import argparse
import copy
import logging
import numpy as np
import os
import random

import chainer
import torch
import torch.nn as nn
import torch.nn.functional as F

from chainer import reporter
from torch.autograd import Variable

from e2e_asr_attctc_th import set_forget_bias_to_one
from e2e_asr_attctc_th import th_accuracy


class ClassifierWithState(nn.Module):
    compute_accuracy = True

    def __init__(self, predictor,
                 lossfun=F.cross_entropy,
                 accfun=th_accuracy,
                 label_key=-1):
        if not (isinstance(label_key, (int, str))):
            raise TypeError('label_key must be int or str, but is %s' %
                            type(label_key))
        super(ClassifierWithState, self).__init__()
        self.lossfun = lossfun
        self.accfun = accfun
        self.y = None
        self.loss = None
        self.accuracy = None
        self.label_key = label_key
        self.predictor = predictor

    def forward(self, state, *args, **kwargs):
        """Computes the loss value for an input and label pair.az

        It also computes accuracy and stores it to the attribute.

        Args:
            args (list of ~chainer.Variable): Input minibatch.
            kwargs (dict of ~chainer.Variable): Input minibatch.

        When ``label_key`` is ``int``, the correpoding element in ``args``
        is treated as ground truth labels. And when it is ``str``, the
        element in ``kwargs`` is used.
        The all elements of ``args`` and ``kwargs`` except the ground trush
        labels are features.
        It feeds features to the predictor and compare the result
        with ground truth labels.

        Returns:
            ~chainer.Variable: Loss value.

        """

        if isinstance(self.label_key, int):
            if not (-len(args) <= self.label_key < len(args)):
                msg = 'Label key %d is out of bounds' % self.label_key
                raise ValueError(msg)
            t = args[self.label_key]
            if self.label_key == -1:
                args = args[:-1]
            else:
                args = args[:self.label_key] + args[self.label_key + 1:]
        elif isinstance(self.label_key, str):
            if self.label_key not in kwargs:
                msg = 'Label key "%s" is not found' % self.label_key
                raise ValueError(msg)
            t = kwargs[self.label_key]
            del kwargs[self.label_key]

        self.y = None
        self.loss = None
        self.accuracy = None
        state, self.y = self.predictor(state, *args, **kwargs)
        self.loss = self.lossfun(self.y, t)
        reporter.report({'loss': self.loss}, self)
        if self.compute_accuracy:
            self.accuracy = self.accfun(self.y, t)
            reporter.report({'accuracy': self.accuracy}, self)
        return state, self.loss


class RNNLM(nn.Module):

    def __init__(self, n_vocab, n_units):
        super(RNNLM, self).__init__()
        self.n_vocab = n_vocab
        self.n_units = n_units
        self.embed = torch.nn.Embedding(n_vocab, n_units)
        self.l1 = torch.nn.LSTMCell(n_units, n_units)
        self.l2 = torch.nn.LSTMCell(n_units, n_units)
        self.lo = torch.nn.Linear(n_units, n_vocab)

        # initialize parameters from uniform distribution
        for param in self.parameters():
            param.data.uniform_(-0.1, 0.1)

        # set forget bias to 1.0
        set_forget_bias_to_one(self.l1.bias_ih)
        set_forget_bias_to_one(self.l2.bias_ih)

    def zero_state(self, batchsize):
        return Variable(torch.zeros(batchsize, self.n_units).zero_()).float()

    def forward(self, state, x):
        h0 = self.embed(x)
        h1, c1 = self.l1(F.dropout(h0), (state['h1'], state['c1']))
        h2, c2 = self.l2(F.dropout(h1), (state['h2'], state['c2']))
        y = self.lo(F.dropout(h2))
        state = {'c1': c1, 'h1': h1, 'c2': c2, 'h2': h2}
        return state, y


# Dataset iterator to create a batch of sequences at different positions.
# This iterator returns a pair of current words and the next words. Each
# example is a part of sequences starting from the different offsets
# equally spaced within the whole sequence.
class ParallelSequentialIterator(chainer.dataset.Iterator):

    def __init__(self, dataset, batch_size, repeat=True):
        self.dataset = dataset
        self.batch_size = batch_size  # batch size
        # Number of completed sweeps over the dataset. In this case, it is
        # incremented if every word is visited at least once after the last
        # increment.
        self.epoch = 0
        # True if the epoch is incremented at the last iteration.
        self.is_new_epoch = False
        self.repeat = repeat
        length = len(dataset)
        # Offsets maintain the position of each sequence in the mini-batch.
        self.offsets = [i * length // batch_size for i in range(batch_size)]
        # NOTE: this is not a count of parameter updates. It is just a count of
        # calls of ``__next__``.
        self.iteration = 0
        # use -1 instead of None internally
        self._previous_epoch_detail = -1.

    def __next__(self):
        # This iterator returns a list representing a mini-batch. Each item
        # indicates a different position in the original sequence. Each item is
        # represented by a pair of two word IDs. The first word is at the
        # "current" position, while the second word at the next position.
        # At each iteration, the iteration count is incremented, which pushes
        # forward the "current" position.
        length = len(self.dataset)
        if not self.repeat and self.iteration * self.batch_size >= length:
            # If not self.repeat, this iterator stops at the end of the first
            # epoch (i.e., when all words are visited once).
            raise StopIteration
        cur_words = self.get_words()
        self._previous_epoch_detail = self.epoch_detail
        self.iteration += 1
        next_words = self.get_words()

        epoch = self.iteration * self.batch_size // length
        self.is_new_epoch = self.epoch < epoch
        if self.is_new_epoch:
            self.epoch = epoch

        return list(zip(cur_words, next_words))

    @property
    def epoch_detail(self):
        # Floating point version of epoch.
        return self.iteration * self.batch_size / len(self.dataset)

    @property
    def previous_epoch_detail(self):
        if self._previous_epoch_detail < 0:
            return None
        return self._previous_epoch_detail

    def get_words(self):
        # It returns a list of current words.
        return [self.dataset[(offset + self.iteration) % len(self.dataset)]
                for offset in self.offsets]

    def serialize(self, serializer):
        # It is important to serialize the state to be recovered on resume.
        self.iteration = serializer('iteration', self.iteration)
        self.epoch = serializer('epoch', self.epoch)
        try:
            self._previous_epoch_detail = serializer(
                'previous_epoch_detail', self._previous_epoch_detail)
        except KeyError:
            # guess previous_epoch_detail for older version
            self._previous_epoch_detail = self.epoch + \
                (self.current_position - self.batch_size) / len(self.dataset)
            if self.epoch_detail > 0:
                self._previous_epoch_detail = max(
                    self._previous_epoch_detail, 0.)
            else:
                self._previous_epoch_detail = -1.


def main():
    parser = argparse.ArgumentParser()
    # general configuration
    parser.add_argument('--gpu', '-g', default='-1', type=int,
                        help='GPU ID (negative value indicates CPU)')
    parser.add_argument('--outdir', type=str, required=True,
                        help='Output directory')
    parser.add_argument('--debugmode', default=1, type=int,
                        help='Debugmode')
    parser.add_argument('--dict', type=str, required=True,
                        help='Dictionary')
    parser.add_argument('--seed', default=1, type=int,
                        help='Random seed')
    parser.add_argument('--minibatches', '-N', type=int, default='-1',
                        help='Process only N minibatches (for debug)')
    parser.add_argument('--verbose', '-V', default=0, type=int,
                        help='Verbose option')
    # task related
    parser.add_argument('--train-label', type=str, required=True,
                        help='Filename of train label data (json)')
    parser.add_argument('--valid-label', type=str, required=True,
                        help='Filename of validation label data (json)')
    # LSTMLM training configuration
    parser.add_argument('--batchsize', '-b', type=int, default=2048,
                        help='Number of examples in each mini-batch')
    parser.add_argument('--bproplen', '-l', type=int, default=35,
                        help='Number of words in each mini-batch '
                             '(= length of truncated BPTT)')
    parser.add_argument('--epoch', '-e', type=int, default=20,
                        help='Number of sweeps over the dataset to train')
    parser.add_argument('--gradclip', '-c', type=float, default=5,
                        help='Gradient norm threshold to clip')
    parser.add_argument('--unit', '-u', type=int, default=650,
                        help='Number of LSTM units in each layer')
    args = parser.parse_args()

    # logging info
    if args.verbose > 0:
        logging.basicConfig(
            level=logging.INFO, format='%(asctime)s (%(module)s:%(lineno)d) %(levelname)s: %(message)s')
    else:
        logging.basicConfig(
            level=logging.WARN, format='%(asctime)s (%(module)s:%(lineno)d) %(levelname)s: %(message)s')
        logging.warning('Skip DEBUG/INFO messages')

    # display PYTHONPATH
    logging.info('python path = ' + os.environ['PYTHONPATH'])

    # display torch version
    logging.info('torch version = ' + torch.__version__)

    # seed setting (chainer seed may not need it)
    nseed = args.seed
    random.seed(nseed)
    np.random.seed(nseed)
    torch.manual_seed(nseed)
    logging.info('torch seed = ' + str(nseed))

    # debug mode setting
    # 0 would be fastest, but 1 seems to be reasonable
    # by considering reproducability
    # use determinisitic computation or not
    if args.debugmode < 1:
        torch.backends.cudnn.deterministic = False
        logging.info('torch cudnn deterministic is disabled')
    else:
        torch.backends.cudnn.deterministic = True

    # load dictionary for debug log
    with open(args.dict, 'rb') as f:
        dictionary = f.readlines()
    char_list = [entry.decode('utf-8').split(' ')[0] for entry in dictionary]
    char_list.insert(0, '<blank>')
    char_list.append('<eos>')
    char_list_dict = {x: i for i, x in enumerate(char_list)}

    # check cuda and cudnn availability
    if not torch.cuda.is_available():
        logging.warning('cuda is not available')

    def evaluate(model, iter, bproplen=100):
        # Evaluation routine to be used for validation and test.
        model.predictor.eval()
        evaluator = copy.deepcopy(model)  # to use different state
        state = {
            'c1': model.predictor.zero_state(args.batchsize),
            'h1': model.predictor.zero_state(args.batchsize),
            'c2': model.predictor.zero_state(args.batchsize),
            'h2': model.predictor.zero_state(args.batchsize)
        }
        if args.gpu >= 0:
            for key in state.iterkeys():
                state[key] = state[key].cuda(args.gpu)
        evaluator.predictor.eval()
        sum_perp = 0
        data_count = 0
        for batch in copy.copy(iter):
            batch = np.array(batch)
            x = Variable(torch.from_numpy(batch[0]).long(), volatile=True)
            t = Variable(torch.from_numpy(batch[1]).long(), volatile=True)
            if args.gpu >= 0:
                x = x.cuda(args.gpu)
                t = t.cuda(args.gpu)
            state, loss = evaluator(state, x, t)
            sum_perp += loss.data
            if data_count % bproplen == 0:
                # detach all states
                for key in state.iterkeys():
                    state[key] = state[key].detach()
            data_count += 1
        model.predictor.train()
        return np.exp(float(sum_perp) / data_count)

    with open(args.train_label, 'rb') as f:
        train = np.array([char_list_dict[char] if char in char_list_dict else char_list_dict['<unk>']
                          for char in f.readline().decode('utf-8').split()], dtype=np.int32)
    with open(args.valid_label, 'rb') as f:
        valid = np.array([char_list_dict[char] if char in char_list_dict else char_list_dict['<unk>']
                          for char in f.readline().decode('utf-8').split()], dtype=np.int32)
    n_vocab = len(char_list)

    # for debug, small data
    # train = train[:100000]
    # valid = valid[:100]

    # for debug, ptb data
    # train, valid, _ = chainer.datasets.get_ptb_words()
    # n_vocab = max(train) + 1  # train is just an array of integers

    logging.info('#vocab = ' + str(n_vocab))
    logging.info('#words in the training data = ' + str(len(train)))
    logging.info('#words in the validation data = ' + str(len(valid)))
    logging.info('#iterations per epoch = ' + str(len(train) // (args.batchsize * args.bproplen)))
    logging.info('#total iterations = ' + str(args.epoch * len(train) // (args.batchsize * args.bproplen)))

    # Create the dataset iterators
    train_iter = ParallelSequentialIterator(train, args.batchsize)
    valid_iter = ParallelSequentialIterator(valid, args.batchsize, repeat=False)

    # Prepare an RNNLM model
    rnn = RNNLM(n_vocab, args.unit)
    model = ClassifierWithState(rnn)
    model.compute_accuracy = False  # we only want the perplexity
    if args.gpu >= 0:
        # Make the specified GPU current
        model.cuda(args.gpu)

    # Set up an optimizer
    optimizer = torch.optim.SGD(model.parameters(), lr=1.0)

    sum_perp = 0
    count = 0
    iteration = 0
    epoch_now = 0
    best_valid = 100000000
    state = {
        'c1': model.predictor.zero_state(args.batchsize),
        'h1': model.predictor.zero_state(args.batchsize),
        'c2': model.predictor.zero_state(args.batchsize),
        'h2': model.predictor.zero_state(args.batchsize)
    }
    if args.gpu >= 0:
        for key in state.iterkeys():
            state[key] = state[key].cuda(args.gpu)
    while train_iter.epoch < args.epoch:
        loss = 0
        iteration += 1
        # Progress the dataset iterator for bprop_len words at each iteration.
        for i in range(args.bproplen):
            # Get the next batch (a list of tuples of two word IDs)
            batch = train_iter.__next__()
            # Concatenate the word IDs to matrices and send them to the device
            # self.converter does this job
            # (it is chainer.dataset.concat_examples by default)
            batch = np.array(batch)
            x = Variable(torch.from_numpy(batch[:, 0]).long())
            t = Variable(torch.from_numpy(batch[:, 1]).long())
            if args.gpu >= 0:
                x = x.cuda(args.gpu)
                t = t.cuda(args.gpu)
            # Compute the loss at this time step and accumulate it
            state, loss_batch = model(state, x, t)
            loss += loss_batch
            count += 1

        sum_perp += loss.data
        model.zero_grad()  # Clear the parameter gradients
        loss.backward()  # Backprop
        # detach all states
        for key in state.iterkeys():
            state[key] = state[key].detach()
        nn.utils.clip_grad_norm(model.parameters(), args.gradclip)
        optimizer.step()  # Update the parameters

        if iteration % 100 == 0:
            logging.info('iteration: ' + str(iteration))
            logging.info('training perplexity: ' + str(np.exp(float(sum_perp) / count)))
            sum_perp = 0
            count = 0

        if train_iter.epoch > epoch_now:
            valid_perp = evaluate(model, valid_iter)
            logging.info('epoch: ' + str(train_iter.epoch))
            logging.info('validation perplexity: ' + str(valid_perp))

            # Save the model and the optimizer
            logging.info('save the model')
            torch.save(model.state_dict(), args.outdir + '/rnnlm.model.' + str(epoch_now))
            logging.info('save the optimizer')
            torch.save(optimizer.state_dict(), args.outdir + '/rnnlm.state.' + str(epoch_now))

            if valid_perp < best_valid:
                dest = args.outdir + '/rnnlm.model.best'
                if os.path.lexists(dest):
                    os.remove(dest)
                os.symlink('rnnlm.model.' + str(epoch_now), dest)
                best_valid = valid_perp

            epoch_now = train_iter.epoch


if __name__ == '__main__':
    main()
