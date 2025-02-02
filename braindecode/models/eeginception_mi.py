# Authors: Cedric Rommel <cedric.rommel@inria.fr>
#
# License: BSD (3-clause)

import torch
from torch import nn

from .modules import Expression, Ensure4d


class EEGInceptionMI(nn.Module):
    """EEG Inception for Motor Imagery, as proposed in [1]_

    The model is strongly based on the original InceptionNet for computer
    vision. The main goal is to extract features in parallel with different
    scales. The network has two blocks made of 3 inception modules with a skip
    connection.

    The model is fully described in [1]_.

    Notes
    -----
    This implementation is not guaranteed to be correct, has not been checked
    by original authors, only reimplemented bosed on the paper [1]_.

    Parameters
    ----------
    in_channels : int
        Number of EEG channels.
    n_classes : int
        Number of classes.
    input_window_s : float, optional
        Size of the input, in seconds. Set to 4.5 s as in [1]_ for dataset
        BCI IV 2a.
    sfreq : float, optional
        EEG sampling frequency in Hz. Defaults to 250 Hz as in [1]_ for dataset
        BCI IV 2a.
    n_convs : int, optional
        Number of convolution per inception wide branching. Defaults to 5 as
        in [1]_ for dataset BCI IV 2a.
    n_filters : int, optional
        Number of convolutional filters for all layers of this type. Set to 48
        as in [1]_ for dataset BCI IV 2a.
    kernel_unit_s : float, optional
        Size in seconds of the basic 1D convolutional kernel used in inception
        modules. Each convolutional layer in such modules have kernels of
        increasing size, odd multiples of this value (e.g. 0.1, 0.3, 0.5, 0.7,
        0.9 here for `n_convs`=5). Defaults to 0.1 s.
    activation: nn.Module
        Activation function. Defaults to ReLU activation.

    References
    ----------
    .. [1] Zhang, C., Kim, Y. K., & Eskandarian, A. (2021).
           EEG-inception: an accurate and robust end-to-end neural network
           for EEG-based motor imagery classification.
           Journal of Neural Engineering, 18(4), 046014.
    """

    def __init__(
            self,
            in_channels,
            n_classes,
            input_window_s=4.5,
            sfreq=250,
            n_convs=5,
            n_filters=48,
            kernel_unit_s=0.1,
            activation=nn.ReLU(),
    ):
        super().__init__()

        self.in_channels = in_channels
        self.n_classes = n_classes
        self.input_window_s = input_window_s
        self.input_window_samples = int(input_window_s * sfreq)
        self.sfreq = sfreq
        self.n_convs = n_convs
        self.n_filters = n_filters
        self.kernel_unit_s = kernel_unit_s
        self.activation = activation

        self.ensuredims = Ensure4d()
        self.dimshuffle = Expression(_transpose_to_b_c_1_t)

        # ======== Inception branches ========================

        self.initial_inception_module = _InceptionModuleMI(
            in_channels=self.in_channels,
            n_filters=self.n_filters,
            n_convs=self.n_convs,
            kernel_unit_s=self.kernel_unit_s,
            sfreq=self.sfreq,
            activation=self.activation,
        )

        intermediate_in_channels = (self.n_convs + 1) * self.n_filters

        self.intermediate_inception_modules_1 = nn.ModuleList([
            _InceptionModuleMI(
                in_channels=intermediate_in_channels,
                n_filters=self.n_filters,
                n_convs=self.n_convs,
                kernel_unit_s=self.kernel_unit_s,
                sfreq=self.sfreq,
                activation=self.activation,
            ) for _ in range(2)
        ])

        self.residual_block_1 = _ResidualModuleMI(
            in_channels=self.in_channels,
            n_filters=intermediate_in_channels,
            activation=self.activation,
        )

        self.intermediate_inception_modules_2 = nn.ModuleList([
            _InceptionModuleMI(
                in_channels=intermediate_in_channels,
                n_filters=self.n_filters,
                n_convs=self.n_convs,
                kernel_unit_s=self.kernel_unit_s,
                sfreq=self.sfreq,
                activation=self.activation,
            ) for _ in range(3)
        ])

        self.residual_block_2 = _ResidualModuleMI(
            in_channels=intermediate_in_channels,
            n_filters=intermediate_in_channels,
            activation=self.activation,
        )

        # XXX The paper mentions a final average pooling but does not indicate
        # the kernel size... The only info available is figure1 showing a
        # final AveragePooling layer and the table3 indicating the spatial and
        # channel dimensions are unchanged by this layer... This could indicate
        # a stride=1 as for MaxPooling layers. Howevere, when we look at the
        # number of parameters of the linear layer following the average
        # pooling, we see a small number of parameters, potentially indicating
        # that the whole time dimension is averaged on this stage for each
        # channel. We follow this last hypothesis here to comply with the
        # number of parameters reported in the paper.
        self.ave_pooling = nn.AvgPool2d(
            kernel_size=(1, self.input_window_samples),
        )

        self.flat = nn.Flatten()
        self.fc = nn.Linear(
            # in_features=self.input_window_samples * intermediate_in_channels,
            in_features=intermediate_in_channels,
            out_features=self.n_classes,
            bias=True,
        )

        self.softmax = nn.LogSoftmax(dim=1)

    def forward(
        self,
        X: torch.Tensor,
    ) -> torch.Tensor:
        X = self.ensuredims(X)
        X = self.dimshuffle(X)

        res1 = self.residual_block_1(X)

        out = self.initial_inception_module(X)
        for layer in self.intermediate_inception_modules_1:
            out = layer(out)

        out = out + res1

        res2 = self.residual_block_2(out)

        for layer in self.intermediate_inception_modules_2:
            out = layer(out)

        out = res2 + out

        out = self.ave_pooling(out)
        out = self.flat(out)
        out = self.fc(out)
        return self.softmax(out)


class _InceptionModuleMI(nn.Module):
    def __init__(
        self,
        in_channels,
        n_filters,
        n_convs,
        kernel_unit_s=0.1,
        sfreq=250,
        activation=nn.ReLU(),
    ):
        super().__init__()
        self.in_channels = in_channels
        self.n_filters = n_filters
        self.n_convs = n_convs
        self.kernel_unit_s = kernel_unit_s
        self.sfreq = sfreq

        self.bottleneck = nn.Conv2d(
            in_channels=self.in_channels,
            out_channels=self.n_filters,
            kernel_size=1,
            bias=True,
        )

        kernel_unit = int(self.kernel_unit_s * self.sfreq)

        # XXX Maxpooling is usually used to reduce spatial resolution, with a
        # stride equal to the kernel size... But it seems the authors use
        # stride=1 in their paper according to the output shapes from Table3,
        # although this is not clearly specified in the paper text.
        self.pooling = nn.MaxPool2d(
            kernel_size=(1, kernel_unit),
            stride=1,
            padding=(0, int(kernel_unit // 2)),
        )

        self.pooling_conv = nn.Conv2d(
            in_channels=self.in_channels,
            out_channels=self.n_filters,
            kernel_size=1,
            bias=True,
        )

        self.conv_list = nn.ModuleList([
            nn.Conv2d(
                in_channels=self.n_filters,
                out_channels=self.n_filters,
                kernel_size=(1, (n_units * 2 + 1) * kernel_unit),
                padding="same",
                bias=True,
            ) for n_units in range(self.n_convs)
        ])

        self.bn = nn.BatchNorm2d(self.n_filters * (self.n_convs + 1))

        self.activation = activation

    def forward(
        self,
        X: torch.Tensor,
    ) -> torch.Tensor:
        X1 = self.bottleneck(X)

        X1 = [conv(X1) for conv in self.conv_list]

        X2 = self.pooling(X)
        X2 = self.pooling_conv(X2)

        out = torch.cat(X1 + [X2], 1)

        out = self.bn(out)
        return self.activation(out)


class _ResidualModuleMI(nn.Module):
    def __init__(
        self,
        in_channels,
        n_filters,
        activation=nn.ReLU()
    ):
        super().__init__()
        self.in_channels = in_channels
        self.n_filters = n_filters
        self.activation = activation

        self.bn = nn.BatchNorm2d(self.n_filters)
        self.conv = nn.Conv2d(
            in_channels=self.in_channels,
            out_channels=self.n_filters,
            kernel_size=1,
            bias=True,
        )

    def forward(
        self,
        X: torch.Tensor,
    ) -> torch.Tensor:
        out = self.conv(X)
        out = self.bn(out)
        return self.activation(out)


def _transpose_to_b_c_1_t(x):
    return x.permute(0, 1, 3, 2)
