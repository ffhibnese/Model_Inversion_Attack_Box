
import torch
import torchvision
from torch import nn
from torch.nn import functional as F

from .modelresult import ModelResult


# class TorchVisionModelWrapper(nn.Module):
    
#     def __init__(self, model: nn.Module, num_classes, pretrained=False):
#         super().__init__()
#         model = torchvision.models.efficientnet.efficientnet_b0(pretrained=pretrained)
#         self.feature = nn.Sequential(*list(model.children())[:-1])
#         self.n_classes = model.num_classes
#         self.feat_dim = 1280
#         self.fc_layer = nn.Linear(self.feat_dim, self.n_classes)
            
#     def forward(self, x):
#         feature = self.feature(x)
#         feature = feature.view(feature.size(0), -1)
#         res = self.fc_layer(feature)
#         return  ModelResult(res, [feature])

class VibWrapper(nn.Module):
    
    def __init__(self, module: nn.Module, hidden_dim: int, output_dim: int, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        
        assert hidden_dim % 2 == 0
        
        self.module = module
        self.hidden_dim = hidden_dim
        self.output_dim = output_dim
        self.k = self.hidden_dim // 2
        self.fc_layer = nn.Linear(self.k, output_dim)
        
    def forward(self, *args, **kwargs):
        statis = self.module(*args, **kwargs).feat[-1]
        
        mu, std = statis[:, :self.k], statis[:, self.k:]

        std = F.softplus(std - 5, beta=1)
        
        eps = torch.FloatTensor(std.size()).normal_().to(std)
        feat = mu + std * eps
        out = self.fc_layer(feat)

        output = ModelResult(out, [feat], {'mu': mu, 'std': std})
        return output
    