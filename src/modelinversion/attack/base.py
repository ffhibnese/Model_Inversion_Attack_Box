import os
import yaml
import warnings
from abc import ABCMeta, abstractmethod
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Union, Optional, Tuple

import torch
from torch import Tensor, LongTensor
from torchvision.utils import save_image
from tqdm import tqdm

from ..models import *
from ..foldermanager import FolderManager
from ..metrics.base import *
from ..utils import DictAccumulator, Accumulator, TorchLoss, batch_apply, print_as_yaml, print_split_line, get_random_string
from ..enums import TqdmStrategy
from ..trainer import BaseGANTrainArgs, BaseGANTrainer




    

@dataclass
class ImageClassifierAttackConfig:
    
    # sample latent
    sample_latents_fn: Callable[[int], None] = None
    
    # initial selection
    initial_num: Optional[int] = None
    initial_latents_score_fn: Optional[Callable[[Tensor, LongTensor], Tensor]] = None
    initial_select_batch_size: Optional[int] = None
    
    # optimzation & generate images
    optimize_num: Optional[int] = None
    optimize_batch_size: int = 5
    optimize_fn: Callable[[Tensor, LongTensor], Tuple[Tensor, LongTensor]] = None
    
    # final selection
    final_num: int = 50
    final_images_score_fn: Optional[Callable[[Tensor, LongTensor], Tensor]] = None
    final_select_batch_size: Optional[int] = None
    
    # save
    save_dir: Optional[str] = None
    save_optimized_images: bool = False
    save_final_images: bool = False
    save_normalize = True
    
    

    
class ImageClassifierAttacker(ABCMeta):
    
    def __init__(self, config: ImageClassifierAttackConfig, metrics: list[ImageClassifierAttackMetric]) -> None:
        self.config = self._preprocess_config(config)
        self.metrics = metrics
        
        self.optimized_images = []
        self.optimized_labels = []
        
    
    def _preprocess_config(self, config: ImageClassifierAttackConfig):
        
        if (config.save_optimized_images or config.save_final_images) and not config.save_dir:
            raise RuntimeError('`save_dir` is not set')
        
        if config.sample_latents_fn is None:
            raise RuntimeError('`sample_latents_fn` cannot be None')
        
        if config.optimize_fn is None:
            raise RuntimeError('`optimize_fn` cannot be None')
        
        if config.final_num is None:
            raise RuntimeError('`final_num` cannot be None')
        
        if config.initial_num is None:
            config.initial_num = config.final_num
            
        if config.optimize_num is None:
            config.optimize_num = config.final_num
            
        if config.final_num > config.optimize_num:
            warnings.warn('the final number is larger than the optimize number, automatically set the latter to the fronter')
            config.optimize_num = config.final_num
            
        if config.optimize_num > config.initial_num:
            warnings.warn('the optimize number is larger than the initial number, automatically set the latter to the fronter')
            config.initial_num = config.optimize_num
            
        if config.initial_select_batch_size is None:
            config.initial_select_batch_size = config.optimize_batch_size
            
        if config.final_select_batch_size is None:
            config.final_select_batch_size = config.final_select_batch_size
            
        return config
    
    def initial_latents(self, batch_size: int, sample_num: int, select_num: int, labels: list[int], latent_score_fn: Optional[Callable] = None) -> dict[int, Tensor]:
        
        if isinstance(labels, Tensor):
            labels = labels.tolist()
        
        if sample_num < select_num:
            warnings.warn('sample_num < select_num. set sample_num = select_num')
            sample_num = select_num
        
        if latent_score_fn is None or sample_num == select_num:
            if sample_num > select_num:
                warnings.warn('no score function, automatically sample `select_num` latents')

            latents = self.config.sample_latents_fn(select_num)
            {label: latents.detach().clone() for label in labels}
        
        raw_latents = self.config.sample_latents_fn(sample_num)
        
        scores = batch_apply(latent_score_fn, raw_latents, labels, batch_size=batch_size)
        
        results = {}
        
        for i in range(labels):
            label = labels[i]
            _, topk_idx = torch.topk(scores, k=select_num)
            results[label] = raw_latents[topk_idx]
        return self.concat_tensor_labels(results)
    
    def concat_tensor_labels(self, target_dict):
        tensors = []
        labels = []
        for target, latents in target_dict:
            tensors.append(latents)
            labels += [target] * len(latents)
        
        labels = LongTensor(labels)
        tensors = torch.cat(tensors, dim=0)
        raise tensors, labels
    
    def concat_optimized_images(self):
        optimized_images = torch.cat(self.optimized_images, dim=0)
        optimized_labels = torch.cat(self.optimized_labels, dim=0)
        return optimized_images, optimized_labels
        
    def final_selection(self, batch_size: int, final_num: int, images: Tensor, labels: LongTensor, image_score_fn: Optional[Callable]=None):
        
        assert len(images) == len(labels)
        
        if final_num != len(images) and image_score_fn is None:
            warnings.warn('no score function but final num is not equal to the number of latents')
            final_num = len(images)
                 
        if final_num == len(images):
            return images
        
        print('execute final selection')
        scores = batch_apply(self, image_score_fn, images, labels, batch_size=batch_size)
        
        targets = set(labels.tolist())
        
        results = {}
        
        for target in targets:
            indices = torch.where(labels == target)
            target_images = images[indices]
            target_scores = scores[indices]
            _, topk_idx = torch.topk(target_scores, k=final_num)
            results[target] = target_images[topk_idx]
            
        return self.concat_tensor_labels(results)
    
    def update_optimized_images(self, images: Tensor, labels: LongTensor):
        assert len(images) == len(labels)
        self.optimized_images.append(images)
        self.optimized_labels.append(labels)
    
    def batch_optimize(self, init_latents: Tensor, labels: Tensor):
        images, labels = self.config.optimize_fn(init_latents, labels)
        self.update_optimized_images(images, labels)
        
        if self.config.save_optimized_images:
            self.save_images(os.path.join(self.config.save_dir, 'optimized_images'), images=images, labels = labels)
    
    def _evaluation(self, images, labels, description):
        
        result = OrderedDict()
        for metric in self.metrics:
            for k, v in metric(images, labels).items():
                result[k] = v
                
        print_split_line(description)
        print_as_yaml(result)
        print_split_line()
        
    def save_images(self, root_dir: str, images: Tensor, labels: LongTensor):
        assert len(images) == len(labels)
        
        for i in range(len(images)):
            image = images[i].detach()
            label = labels[i].item()
            save_dir = os.path.join(root_dir, f'{label}')
            os.makedirs(save_dir, exist_ok=True)
            random_str = get_random_string(length=6)
            save_path = os.path.join(save_dir, f'{label}_{random_str}.png')
            save_image(image, save_path, normalize=self.config.save_normalize)
            
    
    def attack(self, target_list: list[int], eval_optimized = False):
        config = self.config
        os.makedirs(config.save_dir, exist_ok=True)
        
        print_split_line('Attack Config')
        print_as_yaml()
        print_split_line()
        
        # initial selection for each target
        init_latents, init_labels = self.initial_latents(
            config.initial_select_batch_size, 
            config.initial_num, 
            config.optimize_num, 
            target_list, 
            config.initial_latents_score_fn
        )

        # execute optimize
        batch_apply(self.batch_optimize, init_latents, init_labels, batch_size=config.optimize_batch_size, description='Optimized Batch')
        
        # concat optimized images and labels
        optimized_images, optimized_labels = self.concat_optimized_images()
            
                
        if eval_optimized:
            self._evaluation(optimized_images, optimized_labels, 'Optimized Image Evaluation')
        
        # final selection
        final_images, final_labels = self.final_selection(config.final_select_batch_size, config.final_num, optimized_images, optimized_labels, config.final_images_score_fn)
        
        if config.save_final_images:
            self.save_images(os.path.join(config.save_dir, 'final_images'), final_images, final_labels)
        
        self._evaluation(final_images, final_labels, 'Final Image Evaluation')
        
    
    
    
    
    
    
    
    
    
    