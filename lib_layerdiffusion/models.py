import torch.nn as nn
import torch
import cv2
import numpy as np
import logging

from tqdm import tqdm
from typing import Optional, Tuple
from diffusers.configuration_utils import ConfigMixin, register_to_config
from diffusers.models.modeling_utils import ModelMixin
import importlib.metadata
from packaging.version import parse

DEBUG_ENABLED = False

diffusers_version = importlib.metadata.version('diffusers')

def check_diffusers_version(min_version="0.25.0"):
    assert parse(diffusers_version) >= parse(
        min_version
    ), f"diffusers>={min_version} requirement not satisfied. Please install correct diffusers version."

check_diffusers_version()

if parse(diffusers_version) >= parse("0.29.0"):
    from diffusers.models.unets.unet_2d_blocks import UNetMidBlock2D, get_down_block, get_up_block
else:
    from diffusers.models.unet_2d_blocks import UNetMidBlock2D, get_down_block, get_up_block


def zero_module(module):
    """
    Zero out the parameters of a module and return it.
    """
    for p in module.parameters():
        p.detach().zero_()
    return module


class LatentTransparencyOffsetEncoder(torch.nn.Module):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.blocks = torch.nn.Sequential(
            torch.nn.Conv2d(4, 32, kernel_size=3, padding=1, stride=1),
            nn.SiLU(),
            torch.nn.Conv2d(32, 32, kernel_size=3, padding=1, stride=1),
            nn.SiLU(),
            torch.nn.Conv2d(32, 64, kernel_size=3, padding=1, stride=2),
            nn.SiLU(),
            torch.nn.Conv2d(64, 64, kernel_size=3, padding=1, stride=1),
            nn.SiLU(),
            torch.nn.Conv2d(64, 128, kernel_size=3, padding=1, stride=2),
            nn.SiLU(),
            torch.nn.Conv2d(128, 128, kernel_size=3, padding=1, stride=1),
            nn.SiLU(),
            torch.nn.Conv2d(128, 256, kernel_size=3, padding=1, stride=2),
            nn.SiLU(),
            torch.nn.Conv2d(256, 256, kernel_size=3, padding=1, stride=1),
            nn.SiLU(),
            zero_module(torch.nn.Conv2d(256, 4, kernel_size=3, padding=1, stride=1)),
        )

    def __call__(self, x):
        return self.blocks(x)


# 1024 * 1024 * 3 -> 16 * 16 * 512 -> 1024 * 1024 * 3
class UNet1024(ModelMixin, ConfigMixin):
    @register_to_config
    def __init__(
        self,
        in_channels: int = 3,
        out_channels: int = 3,
        down_block_types: Tuple[str] = (
            "DownBlock2D",
            "DownBlock2D",
            "DownBlock2D",
            "DownBlock2D",
            "AttnDownBlock2D",
            "AttnDownBlock2D",
            "AttnDownBlock2D",
        ),
        up_block_types: Tuple[str] = (
            "AttnUpBlock2D",
            "AttnUpBlock2D",
            "AttnUpBlock2D",
            "UpBlock2D",
            "UpBlock2D",
            "UpBlock2D",
            "UpBlock2D",
        ),
        block_out_channels: Tuple[int] = (32, 32, 64, 128, 256, 512, 512),
        layers_per_block: int = 2,
        mid_block_scale_factor: float = 1,
        downsample_padding: int = 1,
        downsample_type: str = "conv",
        upsample_type: str = "conv",
        dropout: float = 0.0,
        act_fn: str = "silu",
        attention_head_dim: Optional[int] = 8,
        norm_num_groups: int = 4,
        norm_eps: float = 1e-5,
    ):
        super().__init__()

        # input
        self.conv_in = nn.Conv2d(
            in_channels, block_out_channels[0], kernel_size=3, padding=(1, 1)
        )
        self.latent_conv_in = zero_module(
            nn.Conv2d(4, block_out_channels[2], kernel_size=1)
        )

        self.down_blocks = nn.ModuleList([])
        self.mid_block = None
        self.up_blocks = nn.ModuleList([])

        # down
        output_channel = block_out_channels[0]
        for i, down_block_type in enumerate(down_block_types):
            input_channel = output_channel
            output_channel = block_out_channels[i]
            is_final_block = i == len(block_out_channels) - 1

            down_block = get_down_block(
                down_block_type,
                num_layers=layers_per_block,
                in_channels=input_channel,
                out_channels=output_channel,
                temb_channels=None,
                add_downsample=not is_final_block,
                resnet_eps=norm_eps,
                resnet_act_fn=act_fn,
                resnet_groups=norm_num_groups,
                attention_head_dim=(
                    attention_head_dim
                    if attention_head_dim is not None
                    else output_channel
                ),
                downsample_padding=downsample_padding,
                resnet_time_scale_shift="default",
                downsample_type=downsample_type,
                dropout=dropout,
            )
            self.down_blocks.append(down_block)

        # mid
        self.mid_block = UNetMidBlock2D(
            in_channels=block_out_channels[-1],
            temb_channels=None,
            dropout=dropout,
            resnet_eps=norm_eps,
            resnet_act_fn=act_fn,
            output_scale_factor=mid_block_scale_factor,
            resnet_time_scale_shift="default",
            attention_head_dim=(
                attention_head_dim
                if attention_head_dim is not None
                else block_out_channels[-1]
            ),
            resnet_groups=norm_num_groups,
            attn_groups=None,
            add_attention=True,
        )

        # up
        reversed_block_out_channels = list(reversed(block_out_channels))
        output_channel = reversed_block_out_channels[0]
        for i, up_block_type in enumerate(up_block_types):
            prev_output_channel = output_channel
            output_channel = reversed_block_out_channels[i]
            input_channel = reversed_block_out_channels[
                min(i + 1, len(block_out_channels) - 1)
            ]

            is_final_block = i == len(block_out_channels) - 1

            up_block = get_up_block(
                up_block_type,
                num_layers=layers_per_block + 1,
                in_channels=input_channel,
                out_channels=output_channel,
                prev_output_channel=prev_output_channel,
                temb_channels=None,
                add_upsample=not is_final_block,
                resnet_eps=norm_eps,
                resnet_act_fn=act_fn,
                resnet_groups=norm_num_groups,
                attention_head_dim=(
                    attention_head_dim
                    if attention_head_dim is not None
                    else output_channel
                ),
                resnet_time_scale_shift="default",
                upsample_type=upsample_type,
                dropout=dropout,
            )
            self.up_blocks.append(up_block)
            prev_output_channel = output_channel

        # out
        self.conv_norm_out = nn.GroupNorm(
            num_channels=block_out_channels[0], num_groups=norm_num_groups, eps=norm_eps
        )
        self.conv_act = nn.SiLU()
        self.conv_out = nn.Conv2d(
            block_out_channels[0], out_channels, kernel_size=3, padding=1
        )

    def forward(self, x, latent):
        sample_latent = self.latent_conv_in(latent)
        sample = self.conv_in(x)
        emb = None

        down_block_res_samples = (sample,)
        for i, downsample_block in enumerate(self.down_blocks):
            if i == 3:
                sample = sample + sample_latent

            sample, res_samples = downsample_block(hidden_states=sample, temb=emb)
            down_block_res_samples += res_samples

        sample = self.mid_block(sample, emb)

        for upsample_block in self.up_blocks:
            res_samples = down_block_res_samples[-len(upsample_block.resnets) :]
            down_block_res_samples = down_block_res_samples[
                : -len(upsample_block.resnets)
            ]
            sample = upsample_block(sample, res_samples, emb)

        sample = self.conv_norm_out(sample)
        sample = self.conv_act(sample)
        sample = self.conv_out(sample)
        return sample


def checkerboard(shape):
    return np.indices(shape).sum(axis=0) % 2


def fill_checkerboard_bg(y: torch.Tensor) -> torch.Tensor:
    alpha = y[..., :1]
    fg = y[..., 1:]
    B, H, W, C = fg.shape
    cb = checkerboard(shape=(H // 64, W // 64))
    cb = cv2.resize(cb, (W, H), interpolation=cv2.INTER_NEAREST)
    cb = (0.5 + (cb - 0.5) * 0.1)[None, ..., None]
    cb = torch.from_numpy(cb).to(fg)
    vis = fg * alpha + cb * (1 - alpha)
    return vis


class TransparentVAEDecoder:
    def __init__(self, sd, device, dtype):
        self.load_device = device
        self.dtype = dtype

        model = UNet1024(in_channels=3, out_channels=4)
        model.load_state_dict(sd, strict=True)
        model.to(self.load_device, dtype=self.dtype)
        model.eval()
        self.model = model

    @torch.no_grad()
    def estimate_single_pass(self, pixel, latent):
        """Run a single forward pass through the UNet model."""
        y = self.model(pixel, latent)
        return y

    @torch.no_grad()
    def estimate_augmented(self, pixel, latent):
        """Apply augmentations (flips and rotations) and aggregate results.

        Uses 8 hardcoded augmentations (4 rotations with/without horizontal flip).
        Replaced torch.median with torch.mean to avoid empty tensor issues on DirectML.
        """
        args = [
            [False, 0],
            [False, 1],
            [False, 2],
            [False, 3],
            [True, 0],
            [True, 1],
            [True, 2],
            [True, 3],
        ]  # Hardcoded 8 augmentations as in original implementation

        result = []
        for flip, rok in tqdm(args):
            feed_pixel = pixel.clone()
            feed_latent = latent.clone()

            if flip:
                feed_pixel = torch.flip(feed_pixel, dims=(3,))
                feed_latent = torch.flip(feed_latent, dims=(3,))

            feed_pixel = torch.rot90(feed_pixel, k=rok, dims=(2, 3))
            feed_latent = torch.rot90(feed_latent, k=rok, dims=(2, 3))

            eps = self.estimate_single_pass(feed_pixel, feed_latent).clip(0, 1)
            eps = torch.rot90(eps, k=-rok, dims=(2, 3))

            if flip:
                eps = torch.flip(eps, dims=(3,))

            result.append(eps)
            if DEBUG_ENABLED:
                logging.debug(f"estimate_augmented: single_pass eps shape={eps.shape}, dtype={eps.dtype}")

        result = torch.stack(result, dim=0)  # Shape: [8, B, C, H, W]
        if DEBUG_ENABLED:
            logging.debug(f"estimate_augmented: stacked result shape={result.shape}, dtype={result.dtype}")

        # Check for NaN or inf values to catch data issues
        if torch.isnan(result).any() or torch.isinf(result).any():
            logging.error("estimate_augmented: stacked tensor contains NaN or inf values")
            raise ValueError("Stacked tensor contains NaN or inf values")

        # Use mean instead of median for stability, especially on DirectML
        y = torch.mean(result, dim=0)  # Shape: [B, C, H, W]
        if DEBUG_ENABLED:
            logging.debug(f"estimate_augmented: y shape={y.shape}, dtype={y.dtype}")

        return y

    @torch.no_grad()
    def decode_pixel(
        self, pixel: torch.TensorType, latent: torch.TensorType
    ) -> torch.TensorType:
        """Decode pixel and latent tensors to produce an RGBA image.

        Args:
            pixel: Input RGB image tensor of shape [B, 3, H, W].
            latent: Latent representation tensor of shape [B, 4, H/8, W/8].

        Returns:
            Tensor of shape [B, 4, H, W] containing RGBA channels.
        """
        assert pixel.shape[1] == 3, f"Expected pixel.shape[1] == 3, got {pixel.shape[1]}"
        pixel_device = pixel.device
        pixel_dtype = pixel.dtype
        
        if DEBUG_ENABLED:
            logging.debug(f"decode_pixel: pixel shape={pixel.shape}, dtype={pixel.dtype}")
            logging.debug(f"decode_pixel: latent shape={latent.shape}, dtype={latent.dtype}")

        pixel = pixel.to(device=self.load_device, dtype=self.dtype)
        latent = latent.to(device=self.load_device, dtype=self.dtype)
        y = self.estimate_augmented(pixel, latent)
        if DEBUG_ENABLED:
            logging.debug(f"decode_pixel: y shape={y.shape}, dtype={y.dtype}")

        if len(y.shape) < 2:
            logging.error(f"decode_pixel: y has insufficient dimensions, shape={y.shape}")
            raise ValueError(f"Expected y to have at least 2 dimensions, got {y.shape}")

        y = y.clip(0, 1)  # Ensure output is in [0, 1] range
        assert y.shape[1] == 4, f"Expected y.shape[1] == 4, got {y.shape[1]}"
        return y.to(pixel_device, dtype=pixel_dtype)