# coding: utf-8
from __future__ import with_statement, print_function, absolute_import

from .version import __version__

import math
import numpy as np

import torch
from torch import nn
from torch.autograd import Variable
from torch.nn import functional as F

from .modules import Conv1d1x1, Conv1dGLU


def _expand_global_features(B, T, g, bct=True):
    """Expand global conditioning features to all time steps

    Args:
        g (Variable): Global features, (B x C) or (B x C x 1).
        bct (bool) : returns (B x C x T) if True, otherwise (B x T x C)

    Returns:
        Variable: B x C x T
    """
    if g is None:
        return None
    g = g.unsqueeze(-1) if g.dim() == 2 else g
    if bct:
        g_bct = g.expand(B, -1, T)
        return g_bct.contiguous()
    else:
        g_btc = g.expand(B, -1, T).transpose(1, 2)
        return g_btc.contiguous()


class WaveNet(nn.Module):
    """WaveNet
    """

    def __init__(self, labels=256, channels=64, layers=12, stacks=2,
                 kernel_size=3, dropout=1 - 0.95,
                 cin_channels=None, gin_channels=None):
        super(WaveNet, self).__init__()
        self.labels = labels
        assert layers % stacks == 0
        layers_per_stack = layers // stacks
        C = channels
        self.first_conv = Conv1d1x1(labels, C)
        self.conv_layers = nn.ModuleList()
        for stack in range(stacks):
            for layer in range(layers_per_stack):
                dilation = 2**layer
                conv = Conv1dGLU(C, C, kernel_size=kernel_size,
                                 bias=True,  # magenda uses bias, but musyoku doesn't
                                 dilation=dilation, dropout=dropout,
                                 cin_channels=cin_channels,
                                 gin_channels=gin_channels)
                self.conv_layers.append(conv)
        self.last_conv_layers = nn.ModuleList([
            nn.ReLU(inplace=True),
            Conv1d1x1(C, C),
            nn.ReLU(inplace=True),
            Conv1d1x1(C, labels),
        ])

    def forward(self, x, c=None, g=None, softmax=False):
        """Forward step

        Args:
            x (Variable): One-hot encoded audio signal, shape (B x C x T)
            c (Variable): Local conditioning features, shape (B x C' x T)
            g (Variable): Global conditioning features, shape (B x C'')
            softmax (bool): Whether applies softmax or not.

        Returns:
            Variable: outupt, shape B x labels x T
        """
        # Expand global conditioning features to all time steps
        B, _, T = x.size()
        g_bct = _expand_global_features(B, T, g, bct=True)

        # Feed data to network
        x = self.first_conv(x)
        skips = None
        for f in self.conv_layers:
            x, h = f(x, c, g_bct)
            skips = h if skips is None else (skips + h) * math.sqrt(0.5)

        x = skips
        for f in self.last_conv_layers:
            x = f(x)

        x = F.softmax(x, dim=1) if softmax else x

        return x

    def incremental_forward(self, initial_input=None, c=None, g=None,
                            T=100, test_inputs=None,
                            tqdm=lambda x: x, softmax=True, quantize=True):
        """Incremental forward

        Due to linearized convolutions, inputs of shape (B x C x T) are reshaped
        to (B x T x C) internally and fed to the network for each time step.
        Input of each time step will be of shape (B x 1 x C).

        Args:
            initial_input (Variable): Initial decoder input, (B x C x 1)
            c (Variable): Local conditioning features, shape (B x C' x T)
            g (Variable): Global conditioning features, shape (B x C'' or B x C''x 1)
            T (int): Number of time steps to generate.
            test_inputs (Variable): Teacher forcing inputs (for debugging)
            tqdm (lamda) : tqdm
            softmax (bool) : Whether applies softmax or not
            quantize (bool): Whether quantize softmax output before feeding the
              network output to input for the next time step.

        Returns:
            Variable: Generated one-hot encoded samples. B x C x T
        """
        self.clear_buffer()
        B = 1

        # Note: shape should be **(B x T x C)**, not (B x C x T) opposed to
        # batch forward due to linealized convolution
        if test_inputs is not None:
            if test_inputs.size(1) == self.labels:
                test_inputs = test_inputs.transpose(1, 2).contiguous()
            B = test_inputs.size(0)
            if T is None:
                T = test_inputs.size(1)
            else:
                T = max(T, test_inputs.size(1))

        # Global conditioning
        g_btc = _expand_global_features(B, T, g, bct=False)
        # Local
        if c is not None and c.size(-1) == T:
            c = c.transpose(1, 2).contiguous()

        outputs = []
        if initial_input is None:
            initial_input = Variable(torch.zeros(B, 1, self.labels))
            initial_input[:, :, 127] = 1  # TODO: is this ok?
            # https://github.com/pytorch/pytorch/issues/584#issuecomment-275169567
            if next(self.parameters()).is_cuda:
                initial_input = initial_input.cuda()
        else:
            if initial_input.size(1) == self.labels:
                initial_input = initial_input.transpose(1, 2).contiguous()

        current_input = initial_input
        for t in tqdm(range(T)):
            if test_inputs is not None and t < test_inputs.size(1):
                current_input = test_inputs[:, t, :].unsqueeze(1)
            else:
                if t > 0:
                    current_input = outputs[-1]

            # Conditioning features for single time step
            ct = None if c is None else c[:, t, :].unsqueeze(1)
            gt = None if g is None else g_btc[:, t, :].unsqueeze(1)

            x = current_input
            x = self.first_conv.incremental_forward(x)
            skips = None
            for f in self.conv_layers:
                x, h = f.incremental_forward(x, ct, gt)
                skips = h if skips is None else (skips + h) * math.sqrt(0.5)
            x = skips
            for f in self.last_conv_layers:
                try:
                    x = f.incremental_forward(x)
                except AttributeError:
                    x = f(x)

            x = F.softmax(x.view(B, -1), dim=1) if softmax else x.view(B, -1)
            if quantize:
                sample = np.random.choice(
                    np.arange(self.labels), p=x.view(-1).data.cpu().numpy())
                x.zero_()
                x[:, sample] = 1.0
            outputs += [x]

        # T x B x C
        outputs = torch.stack(outputs)
        # B x C x T
        outputs = outputs.transpose(0, 1).transpose(1, 2).contiguous()

        self.clear_buffer()
        return outputs

    def clear_buffer(self):
        self.first_conv.clear_buffer()
        for f in self.conv_layers:
            f.clear_buffer()
        for f in self.last_conv_layers:
            try:
                f.clear_buffer()
            except AttributeError:
                pass
