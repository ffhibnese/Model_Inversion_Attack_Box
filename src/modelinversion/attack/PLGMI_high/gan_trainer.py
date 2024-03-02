
import os
import time
from abc import abstractmethod, ABCMeta
from collections import defaultdict, OrderedDict
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

import torch
import kornia
import numpy as np
from torch import nn, optim
from torch.nn import functional as F
from torch.utils.data import DataLoader
from torchvision import transforms as tv_trans
from torchvision.datasets import ImageFolder
from tqdm import tqdm
from PIL import Image

from modelinversion.metrics.base import DataLoader


from ..base import BaseGANTrainArgs, BaseGANTrainer
from ...models import *
from ...utils import walk_imgs, print_as_yaml
from .code.m_cgan import ResNetGenerator, SNResNetProjectionDiscriminator
         
@dataclass
class PlgmiGANTrainArgs(BaseGANTrainArgs):
    top_n: int = 30
    target_name: str = 'vgg16'
    target_dataset_name: str = 'celeba'
    # num_classes: int = 1000
    augment: Callable = field(default_factory=lambda: kornia.augmentation.container.ImageSequential(
        kornia.augmentation.RandomResizedCrop((256, 256), scale=(0.8, 1.0), ratio=(1.0, 1.0)),
        kornia.augmentation.ColorJitter(brightness=0.2, contrast=0.2, p=0.5),
        kornia.augmentation.RandomHorizontalFlip(),
        kornia.augmentation.RandomRotation(5),
    ))
    
    coef_inv_loss: float = 0.2
    lr: float= 0.0002
    # beta1: float = 0.0
    beta1: float = 0.0
    beta2: float = 0.9
    z_dim = 200
    gen_distribution = 'normal'
            
class PlgmiGANTrainer(BaseGANTrainer):
    
    def __init__(self, args: PlgmiGANTrainArgs, folder_manager, **kwargs) -> None:
        super().__init__(args, folder_manager, **kwargs)
        self.args: PlgmiGANTrainArgs
        self.num_classes = NUM_CLASSES[args.target_dataset_name]
        
        self.src_dataset_dir = os.path.join(folder_manager.config.dataset_dir, args.dataset_name, 'split', 'public')
        self.dst_dataset_dir = os.path.join(folder_manager.config.cache_dir, args.dataset_name, args.target_name)
        
    def get_tag(self) -> str:
        args = self.args
        return f'plgmi_high_{args.top_n}_{args.dataset_name}_{args.target_name}_{args.target_dataset_name}'
    
    def get_method_name(self) -> str:
        return 'PLGMI_high'
    
    def _check_select_topn(self):
        ret = True
        for i in range(self.num_classes):
            dirname = os.path.join(self.dst_dataset_dir, f'{i}')
            if not os.path.exists(dirname):
                ret = False
            elif len(os.listdir(dirname)) != self.args.top_n:
                os.system(f'rm -rf {dirname}')
                ret = False
            
        return ret
    
    def get_trainloader(self) -> DataLoader:
        dataset = ImageFolder(self.dst_dataset_dir, transform=tv_trans.Compose([
                # Image.open,
                tv_trans.ToTensor(), 
                # tv_trans.CenterCrop((800,800)),
                # tv_trans.Resize((256, 256), antialias=True)
            ]))
        dataloader = DataLoader(dataset, self.args.batch_size, shuffle=True)
        return dataloader
        
    def prepare_training(self):
        # return "maomao"
        args = self.args
        self.G = ResNetGenerator(dim_z=args.z_dim, num_classes=self.num_classes, distribution=args.gen_distribution).to(args.device)
        self.D = SNResNetProjectionDiscriminator(num_classes=self.num_classes).to(args.device)
        # self.G.load_state_dict(torch.load('checkpoints/PLGMI_high/plgmi_high_metfaces_resnet18_facescrub_G.pt')['state_dict'])
        # self.D.load_state_dict(torch.load('checkpoints/PLGMI_high/plgmi_high_metfaces_resnet18_facescrub_D.pt')['state_dict'])
        self.G = nn.DataParallel(self.G)
        self.D = nn.DataParallel(self.D)
        self.T = get_model(args.target_name, args.target_dataset_name, device=args.device, backbone_pretrain=False, defense_type=args.defense_type)
        # self.folder_manager.load_target_model_state_dict(self.T, args.target_dataset_name, args.target_name, device=args.device, defense_type=args.defense_type)
        self.T.load_state_dict(torch.load('/data/yuhongyao/Model_Inversion_Attack_ToolBox/checkpoints/target_eval/hdceleba/resnet152_celeba.pt', map_location=args.device)['state_dict'])
        self.T.eval()
        
        # self.folder_manager.load_state_dict(self.G, [self.method_name, f'{self.tag}_G.pt'], self.args.device, self.args.defense_type)
        # self.folder_manager.load_state_dict(self.D, [self.method_name, f'{self.tag}_D.pt'], self.args.device, self.args.defense_type)
        
        self.optim_G = torch.optim.Adam(self.G.parameters(), args.lr, (args.beta1, args.beta2))
        self.optim_D = torch.optim.Adam(self.D.parameters(), args.lr, (args.beta1, args.beta2))
        
        if not self._check_select_topn():
            print(f'start top n selection from {self.src_dataset_dir} to {self.dst_dataset_dir}')
            src_img_paths = walk_imgs(self.src_dataset_dir)
            
            trans = tv_trans.Compose([
                Image.open,
                tv_trans.ToTensor(), 
                # tv_trans.CenterCrop((800,800)),
                # tv_trans.Resize((256, 256), antialias=True)
            ])
            
            with torch.no_grad():
                src_imgs = [trans(p) for p in tqdm(src_img_paths)]
                src_imgs = torch.stack(src_imgs, dim=0)
                src_scores = []
                total_num = len(src_img_paths)
                for i in tqdm(range((total_num-1) // args.batch_size + 1)):
                    start_idx = i * args.batch_size
                    end_idx = min(start_idx + args.batch_size, total_num)
                    # batch_paths = src_img_paths[start_idx:end_idx]
                    batch_imgs = src_imgs[start_idx:end_idx].to(args.device)
                    batch_scores = self.T(batch_imgs).result.softmax(dim=-1).cpu()
                    src_scores.append(batch_scores)
                src_scores = torch.cat(src_scores, dim=0)
                
                i = 0
                for label in tqdm(range(self.num_classes)):
                    dst_dir = os.path.join(self.dst_dataset_dir, f'{label}')
                    os.makedirs(dst_dir, exist_ok=True)
                    scores = src_scores[:, label]
                    _, indice = torch.topk(scores, k=args.top_n)
                    # torch.save(src_imgs, os.path.join(dst_dir, f'{label}.pt'))
                    indice = indice.numpy().tolist()
                    for idx in indice:
                        torch
                        os.system(f'cp {src_img_paths[idx]} {dst_dir}/')
        exit()
                    
    
    def _sample(self, batch_size):
        args = self.args
        z = torch.randn((batch_size, args.z_dim), device=args.device)
        y = torch.randint(0, self.num_classes, (batch_size,), device=args.device)
        fake = self.G(z, y)
        return z, y, fake
    
    def _max_margin_loss(self, out, iden):
        real = out.gather(1, iden.unsqueeze(1)).squeeze(1)
        tmp1 = torch.argsort(out, dim=1)[:, -2:]
        new_y = torch.where(tmp1[:, -1] == iden, tmp1[:, -2], tmp1[:, -1])
        margin = out.gather(1, new_y.unsqueeze(1)).squeeze(1)

        return (-1 * real).mean() + margin.mean()
    
    def before_train(self):
        super().before_train()
        _, labels, fake = self._sample(5)
        print(fake.shape)
        labels = labels.cpu().tolist()
        fake = fake.cpu()
        # torch.save(f'epoch_{self.epoch}_{labels[0]}_{labels[1]}_{labels[2]}_{labels[3]}_{labels[4]}.png')
        import torchvision
        save_dir = os.path.join(self.folder_manager.config.cache_dir, 'train_sample')
        os.makedirs(save_dir, exist_ok=True)
        save_path = os.path.join(save_dir, f'epoch_{self.epoch}_{labels[0]}_{labels[1]}_{labels[2]}_{labels[3]}_{labels[4]}.png')
        torchvision.utils.save_image(fake, save_path, nrow=5, normalize=True)
    
    def train_gen_step(self, batch):
        
        
        args = self.args
        bs = len(batch[0])
        _, labels, fake = self._sample(bs)
        dis_loss = - self.D(fake).mean()
        
        aug_fake = args.augment(fake) if args.augment else fake
        
        pred = self.T(aug_fake).result
        inv_loss = self._max_margin_loss(pred, labels)
        
        loss = dis_loss + inv_loss * args.coef_inv_loss
        
        # print('aaa', bs)
        # while 1:
        #     pass
        
        super().loss_update(loss, self.optim_G)
        
        # return {
        #     'dis loss': dis_loss.item(),
        #     'inv loss': inv_loss.item(),
        #     'total loss': loss.item()
        # }
        return OrderedDict(
            dis_loss = dis_loss.item(),
            inv_loss = inv_loss.item(),
            total_loss = loss.item()
        )
        
    def train_dis_step(self, batch):
        args = self.args
        bs = len(batch[0])
        
        _, labels, fake = self._sample(bs)
        # print(fake.shape, labels.shape)
        dis_fake = self.D(fake, labels)
        dis_fake = torch.mean(torch.relu(1. + dis_fake))
        
        real_imgs, real_labels = batch
        real_imgs, real_labels = real_imgs.to(args.device), real_labels.to(args.device)
        # print(fake.shape, labels.shape)
        # print(real_imgs.shape, real_labels.shape)
        # print(real_labels[:5])
        dis_real = self.D(real_imgs, real_labels)
        dis_real = torch.mean(torch.relu(1. - dis_real))
        
        # exit()
        
        loss = dis_fake + dis_real
           
           
        super().loss_update(loss, self.optim_D)
        
        # return {
        #     'fake loss': dis_fake.item(),
        #     'real loss': dis_real.item(),
        #     'total loss': loss.item()
        # }
        return OrderedDict(
            fake_loss = dis_fake.item(),
            real_loss = dis_real.item(),
            total_loss = loss.item()
        )