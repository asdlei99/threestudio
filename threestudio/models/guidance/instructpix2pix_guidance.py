from dataclasses import dataclass, field

import torch
import cv2
import numpy as np
from torch import nn
import torch.nn.functional as F
from tqdm import tqdm
from diffusers import (
    DDIMScheduler,
    StableDiffusionInstructPix2PixPipeline,
)

import threestudio
from threestudio.models.prompt_processors.base import PromptProcessorOutput
from threestudio.utils.base import BaseObject
from threestudio.utils.misc import C, parse_version
from threestudio.utils.typing import *

IMG_DIM = 512
CONST_SCALE = 0.18215


@threestudio.register("instructpix2pix-guidance")
class InstructPix2PixGuidance(BaseObject):
    @dataclass
    class Config(BaseObject.Config):
        cache_dir: str = None
        # pretrained_model_name_or_path: str = "runwayml/stable-diffusion-v1-5"
        ddim_scheduler_name_or_path: str = "CompVis/stable-diffusion-v1-4"
        ip2p_name_or_path: str = "timbrooks/instruct-pix2pix"

        enable_memory_efficient_attention: bool = False
        enable_sequential_cpu_offload: bool = False
        enable_attention_slicing: bool = False
        enable_channels_last_format: bool = False
        guidance_scale: float = 7.5
        condition_scale: float  = 1.5
        # grad_clip: Optional[
        #     Any
        # ] = None  # field(default_factory=lambda: [0, 2.0, 8.0, 1000])
        half_precision_weights: bool = True

        min_step_percent: float = 0.02
        max_step_percent: float = 0.98

        diffusion_steps: int = 20

        # use_sjc: bool = False
        # var_red: bool = True
        # weighting_strategy: str = "sds"

        token_merging: bool = False
        token_merging_params: Optional[dict] = field(default_factory=dict)

        # view_dependent_prompting: bool = True
    
    cfg: Config

    def configure(self) -> None:
        threestudio.info(f"Loading InstructPix2Pix ...")

        self.weights_dtype = (
            torch.float16 if self.cfg.half_precision_weights else torch.float32
        )

        pipe_kwargs = {
            "safety_checker": None,
            "feature_extractor": None,
            "requires_safety_checker": False,
            "torch_dtype": self.weights_dtype,
            "cache_dir": self.cfg.cache_dir
        }

        self.pipe = StableDiffusionInstructPix2PixPipeline.from_pretrained(
            self.cfg.ip2p_name_or_path,
            **pipe_kwargs).to(self.device)
        self.scheduler = DDIMScheduler.from_pretrained(
            self.cfg.ddim_scheduler_name_or_path, 
            subfolder="scheduler", 
            torch_dtype=self.weights_dtype, 
            cache_dir=self.cfg.cache_dir)
        self.scheduler.set_timesteps(self.cfg.diffusion_steps)

        if self.cfg.enable_memory_efficient_attention:
            if parse_version(torch.__version__) >= parse_version("2"):
                threestudio.info(
                    "PyTorch2.0 uses memory efficient attention by default."
                )
            elif not is_xformers_available():
                threestudio.warn(
                    "xformers is not available, memory efficient attention is not enabled."
                )
            else:
                self.pipe.enable_xformers_memory_efficient_attention()

        if self.cfg.enable_sequential_cpu_offload:
            self.pipe.enable_sequential_cpu_offload()

        if self.cfg.enable_attention_slicing:
            self.pipe.enable_attention_slicing(1)

        if self.cfg.enable_channels_last_format:
            self.pipe.unet.to(memory_format=torch.channels_last)

        # Create model
        self.vae = self.pipe.vae.eval()
        self.unet = self.pipe.unet.eval()

        for p in self.vae.parameters():
            p.requires_grad_(False)
        for p in self.unet.parameters():
            p.requires_grad_(False)

        if self.cfg.token_merging:
            import tomesd

            tomesd.apply_patch(self.unet, **self.cfg.token_merging_params)

        self.num_train_timesteps = self.scheduler.config.num_train_timesteps
        self.min_step = int(self.num_train_timesteps * self.cfg.min_step_percent)
        self.max_step = int(self.num_train_timesteps * self.cfg.max_step_percent)

        self.alphas: Float[Tensor, "..."] = self.scheduler.alphas_cumprod.to(
            self.device
        )

        self.grad_clip_val: Optional[float] = None

        threestudio.info(f"Loaded InstructPix2Pix!")
    
    @torch.cuda.amp.autocast(enabled=False)
    def forward_unet(
        self,
        latents: Float[Tensor, "..."],
        t: Float[Tensor, "..."],
        encoder_hidden_states: Float[Tensor, "..."],
    ) -> Float[Tensor, "..."]:
        input_dtype = latents.dtype
        return self.unet(
            latents.to(self.weights_dtype),
            t.to(self.weights_dtype),
            encoder_hidden_states=encoder_hidden_states.to(self.weights_dtype),
        ).sample.to(input_dtype)
    
    @torch.cuda.amp.autocast(enabled=False)
    def encode_images(
        self, imgs: Float[Tensor, "B 3 512 512"]
    ) -> Float[Tensor, "B 4 64 64"]:
        input_dtype = imgs.dtype
        imgs = imgs * 2.0 - 1.0
        posterior = self.vae.encode(imgs.to(self.weights_dtype)).latent_dist
        latents = posterior.sample() * self.vae.config.scaling_factor
        return latents.to(input_dtype)
    
    @torch.cuda.amp.autocast(enabled=False)
    def encode_cond_images(
        self, imgs: Float[Tensor, "B 3 512 512"]
    ) -> Float[Tensor, "B 4 64 64"]:
        input_dtype = imgs.dtype
        imgs = imgs * 2.0 - 1.0
        posterior = self.vae.encode(imgs.to(self.weights_dtype)).latent_dist
        latents = posterior.mode()
        uncond_image_latents = torch.zeros_like(latents)
        latents = torch.cat([latents, latents, uncond_image_latents], dim=0)
        return latents.to(input_dtype)
    
    @torch.cuda.amp.autocast(enabled=False)
    def decode_latents(
        self,
        latents: Float[Tensor, "B 4 H W"],
        latent_height: int = 64,
        latent_width: int = 64,
    ) -> Float[Tensor, "B 3 512 512"]:
        input_dtype = latents.dtype
        latents = F.interpolate(
            latents, (latent_height, latent_width), mode="bilinear", align_corners=False
        )
        latents = 1 / self.vae.config.scaling_factor * latents
        image = self.vae.decode(latents.to(self.weights_dtype)).sample
        image = (image * 0.5 + 0.5).clamp(0, 1)
        return image.to(input_dtype)

    def edit_latents(
        self,
        text_embeddings: Float[Tensor, "BB 77 768"],
        latents: Float[Tensor, "B 4 64 64"],
        image_cond_latents: Float[Tensor, "B 4 64 64"],
        t: Int[Tensor, "B"]
    ) -> Float[Tensor, "B 4 64 64"]:
        self.scheduler.config.num_train_timesteps = t.item()
        self.scheduler.set_timesteps(self.cfg.diffusion_steps)
        with torch.no_grad():
            # add noise
            noise = torch.randn_like(latents)
            latents = self.scheduler.add_noise(latents, noise, t)  # type: ignore

            # sections of code used from https://github.com/huggingface/diffusers/blob/main/src/diffusers/pipelines/stable_diffusion/pipeline_stable_diffusion_instruct_pix2pix.py
            for i, t in tqdm(enumerate(self.scheduler.timesteps)):

                # predict the noise residual with unet, NO grad!
                with torch.no_grad():
                    # pred noise
                    latent_model_input = torch.cat([latents] * 3)
                    latent_model_input = torch.cat([latent_model_input, image_cond_latents], dim=1)

                    noise_pred = self.forward_unet(latent_model_input, t, encoder_hidden_states=text_embeddings)

                # perform classifier-free guidance
                noise_pred_text, noise_pred_image, noise_pred_uncond = noise_pred.chunk(3)
                noise_pred = (
                    noise_pred_uncond
                    + self.cfg.guidance_scale * (noise_pred_text - noise_pred_image)
                    + self.cfg.condition_scale * (noise_pred_image - noise_pred_uncond)
                )

                # get previous sample, continue loop
                latents = self.scheduler.step(noise_pred, t, latents).prev_sample
        return latents
    
    def __call__(
        self,
        rgb: Float[Tensor, "B H W C"],
        cond_rgb: Float[Tensor, "B H W C"],
        text_embeddings,
        **kwargs,
    ):
        batch_size = rgb.shape[0]

        rgb_BCHW = rgb.permute(0, 3, 1, 2)
        latents: Float[Tensor, "B 4 64 64"]
        rgb_BCHW_512 = F.interpolate(
            rgb_BCHW, (512, 512), mode="bilinear", align_corners=False
        )
        latents = self.encode_images(rgb_BCHW_512)

        cond_rgb_BCHW = cond_rgb.permute(0, 3, 1, 2)
        latents: Float[Tensor, "B 4 64 64"]
        cond_rgb_BCHW_512 = F.interpolate(
            cond_rgb_BCHW, (512, 512), mode="bilinear", align_corners=False
        )
        cond_latents = self.encode_cond_images(cond_rgb_BCHW_512)

        # text_embeddings = prompt_utils.get_text_embeddings(
        #     elevation, azimuth, camera_distances, self.cfg.view_dependent_prompting
        # )

        # timestep ~ U(0.02, 0.98) to avoid very high/low noise level
        t = torch.randint(
            self.min_step,
            self.max_step + 1,
            [batch_size],
            dtype=torch.long,
            device=self.device,
        )

        edit_latents = self.edit_latents(text_embeddings, latents, cond_latents, t)
        edit_images = self.decode_latents(edit_latents)

        return {
            "edit_images": edit_images
        }


if __name__ == '__main__':
    from threestudio.utils.config import ExperimentConfig, load_config
    from threestudio.utils.typing import Optional
    cfg = load_config("configs/experimental/instructpix2pix.yaml")
    guidance = threestudio.find(cfg.system.guidance_type)(cfg.system.guidance)
    # prompt_processor = threestudio.find(cfg.system.prompt_processor_type)(cfg.system.prompt_processor)
    text_embeddings = guidance.pipe._encode_prompt(
        cfg.system.prompt_processor.prompt, 
        device=guidance.device, 
        num_images_per_prompt=1, 
        do_classifier_free_guidance=True, 
        negative_prompt=cfg.system.prompt_processor.negative_prompt
    )
    rgb_image = cv2.imread('assets/face.jpg')[:, :, ::-1].copy() / 255
    rgb_image = cv2.resize(rgb_image, (512, 512))
    rgb_image = torch.FloatTensor(rgb_image).unsqueeze(0).to(guidance.device)
    # prompt_utils = prompt_processor()
    guidance_out = guidance(
        rgb_image, rgb_image, text_embeddings
    )
    edit_image = (guidance_out['edit_images'][0].permute(1, 2, 0).detach().cpu().clip(0, 1).numpy()*255).astype(np.uint8)[:, :, ::-1].copy()
    import os
    os.makedirs('.threestudio_cache', exist_ok=True)
    cv2.imwrite('.threestudio_cache/edit_image.jpg', edit_image)