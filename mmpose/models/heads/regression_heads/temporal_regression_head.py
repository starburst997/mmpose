# Copyright (c) OpenMMLab. All rights reserved.
from typing import Optional, Sequence, Tuple, Union

import numpy as np
import torch
from torch import Tensor, nn

from mmpose.evaluation.functional import keypoint_pck_accuracy
from mmpose.registry import KEYPOINT_CODECS, MODELS
from mmpose.utils.tensor_utils import to_numpy
from mmpose.utils.typing import (ConfigType, OptConfigType, OptSampleList,
                                 Predictions)
from ..base_head import BaseHead

OptIntSeq = Optional[Sequence[int]]


@MODELS.register_module()
class TemporalRegressionHead(BaseHead):
    """Temporal Regression head of `VideoPose3D`_ by Dario et al (CVPR'2019).

    Args:
        in_channels (int | sequence[int]): Number of input channels
        num_joints (int): Number of joints
        loss (Config): Config for keypoint loss. Defaults to use
            :class:`SmoothL1Loss`
        decoder (Config, optional): The decoder config that controls decoding
            keypoint coordinates from the network output. Defaults to ``None``
        init_cfg (Config, optional): Config to control the initialization. See
            :attr:`default_init_cfg` for default settings

    .. _`VideoPose3D`: https://arxiv.org/abs/1811.11742
    """

    _version = 2

    def __init__(self,
                 in_channels: Union[int, Sequence[int]],
                 num_joints: int,
                 loss: ConfigType = dict(
                     type='MSELoss', use_target_weight=True),
                 decoder: OptConfigType = None,
                 init_cfg: OptConfigType = None):

        if init_cfg is None:
            init_cfg = self.default_init_cfg

        super().__init__(init_cfg)

        self.in_channels = in_channels
        self.num_joints = num_joints
        self.loss_module = MODELS.build(loss)
        if decoder is not None:
            self.decoder = KEYPOINT_CODECS.build(decoder)
        else:
            self.decoder = None

        # Define fully-connected layers
        self.conv = nn.Conv1d(in_channels, self.num_joints * 3, 1)

    def forward(self, feats: Tuple[Tensor]) -> Tensor:
        """Forward the network. The input is multi scale feature maps and the
        output is the coordinates.

        Args:
            feats (Tuple[Tensor]): Multi scale feature maps.

        Returns:
            Tensor: output coordinates(and sigmas[optional]).
        """
        x = feats[-1]

        x = self.conv(x)

        return x.reshape(-1, self.num_joints, 3)

    def predict(self,
                feats: Union[Tensor, Tuple[Tensor]],
                batch_data_samples: OptSampleList,
                test_cfg: ConfigType = {}) -> Predictions:
        """Predict results from outputs."""

        batch_coords = self.forward(feats)  # (B, K, D)

        batch_coords.unsqueeze_(dim=1)  # (B, N, K, D)

        # Restore global position with target_root
        target_root = batch_data_samples[0].metainfo.get('target_root', None)
        if target_root is not None:
            target_root = torch.stack(
                [m['target_root'] for m in batch_data_samples[0].metainfo])

        preds = self.decode(batch_coords, target_root)

        return preds

    def loss(self,
             inputs: Tuple[Tensor],
             batch_data_samples: OptSampleList,
             train_cfg: ConfigType = {}) -> dict:
        """Calculate losses from a batch of inputs and data samples."""

        pred_outputs = self.forward(inputs)

        keypoint_labels = torch.cat(
            [d.gt_instance_labels.keypoint_labels for d in batch_data_samples])
        keypoint_weights = torch.cat([
            d.gt_instance_labels.keypoint_weights for d in batch_data_samples
        ])

        # calculate losses
        losses = dict()
        loss = self.loss_module(pred_outputs, keypoint_labels,
                                keypoint_weights.unsqueeze(-1))

        losses.update(loss_kpt=loss)

        # calculate accuracy
        _, avg_acc, _ = keypoint_pck_accuracy(
            pred=to_numpy(pred_outputs),
            gt=to_numpy(keypoint_labels),
            mask=to_numpy(keypoint_weights) > 0,
            thr=0.05,
            norm_factor=np.ones((pred_outputs.size(0), 2), dtype=np.float32))

        acc_pose = torch.tensor(avg_acc, device=keypoint_labels.device)
        losses.update(acc_pose=acc_pose)

        return losses

    @property
    def default_init_cfg(self):
        init_cfg = [dict(type='Normal', layer=['Linear'], std=0.01, bias=0)]
        return init_cfg
