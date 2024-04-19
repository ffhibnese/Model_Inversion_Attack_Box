from abc import abstractmethod
from collections import OrderedDict
from typing import Callable, Any, Optional, Iterable, Sequence


import torch
from torch import nn, Tensor, LongTensor
from torch.nn import functional as F
from ..utils import (
    TorchLoss,
    reparameterize,
    DeepInversionBNFeatureHook,
    traverse_module,
)
from ..models import (
    BaseImageClassifier,
    GmiDiscriminator64,
    GmiDiscriminator256,
    KedmiDiscriminator64,
    KedmiDiscriminator256,
    HOOK_NAME_FEATURE,
)


class BaseImageLoss(nn.Module):

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)

    @abstractmethod
    def forward(self, images: Tensor, labels: LongTensor, *args, **kwargs):
        pass


class ImageAugmentClassificationLoss(BaseImageLoss):

    def __init__(
        self,
        classifier: BaseImageClassifier,
        loss_fn: str | Callable[[Tensor, LongTensor], Tensor] = 'cross_entropy',
        create_aug_images_fn: Optional[Callable[[Tensor], Iterable[Tensor]]] = None,
        *args,
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)
        if create_aug_images_fn is None:
            create_aug_images_fn = lambda x: [x]

        self.create_aug_images_fn = create_aug_images_fn
        self.classifier = classifier
        self.loss_fn = TorchLoss(loss_fn, *args, **kwargs)

    def forward(self, images, labels, *args, **kwargs):
        acc = 0
        loss = 0
        total_num = 0
        for aug_images in self.create_aug_images_fn(images):
            total_num += 1
            conf, _ = self.classifier(aug_images)
            pred_labels = torch.argmax(conf, dim=-1)
            loss += self.loss_fn(conf, labels)
            # print(pred_labels)
            # print(labels)
            # exit()
            acc += (pred_labels == labels).float().mean().item()

        return loss, OrderedDict(
            [['classification loss', loss.item()], ['target acc', acc / total_num]]
        )


class ClassificationWithFeatureDistributionLoss(ImageAugmentClassificationLoss):

    def __init__(
        self,
        classifier: BaseImageClassifier,
        feature_mean: Tensor,
        feature_std: Tensor,
        classification_loss_fn: (
            str | Callable[[Tensor, LongTensor], Tensor]
        ) = 'cross_entropy',
        create_aug_images_fn: Optional[Callable[[Tensor], Iterable[Tensor]]] = None,
        feature_loss_weight: float = 1.0,
        *args,
        **kwargs,
    ) -> None:
        super().__init__(
            classifier, classification_loss_fn, create_aug_images_fn, *args, **kwargs
        )

        self.feature_mean = feature_mean
        self.feature_std = feature_std
        self.feature_loss_weight = feature_loss_weight

    def _sample_distribution(self):
        return reparameterize(self.feature_mean, self.feature_std)

    def forward(self, images, labels, *args, **kwargs):

        acc = 0
        iden_loss = 0
        feature_loss = 0
        total_num = 0
        bs = len(images)
        for aug_images in self.create_aug_images_fn(images):
            total_num += 1
            conf, info_dict = self.classifier(aug_images)
            if HOOK_NAME_FEATURE not in info_dict:
                raise RuntimeError(
                    f'The addition info that the model outputs do not contains {HOOK_NAME_FEATURE}'
                )
            pred_labels = torch.argmax(conf, dim=-1)
            iden_loss += self.loss_fn(conf, labels)

            feature_dist_samples = self._sample_distribution()
            feature_loss += torch.mean(
                (
                    info_dict[HOOK_NAME_FEATURE].view(bs, -1)
                    - feature_dist_samples.view(1, -1)
                ).pow(2)
            )
            acc += (pred_labels == labels).float().mean().item()

        loss = iden_loss + self.feature_loss_weight * feature_loss

        return loss, OrderedDict(
            [
                ['loss', loss.item()],
                ['classification loss', iden_loss.item()],
                ['feature loss', feature_loss.item()],
                ['target acc', acc / total_num],
            ]
        )


class GmiDiscriminatorLoss(BaseImageLoss):

    def __init__(
        self, discriminator: GmiDiscriminator64 | GmiDiscriminator256, *args, **kwargs
    ) -> None:
        super().__init__(*args, **kwargs)
        self.discriminator = discriminator

    def forward(self, images, labels, *args, **kwargs):
        dis_res = self.discriminator(images)
        loss = -dis_res.mean()
        return loss, {'discriminator loss': loss.item()}


class KedmiDiscriminatorLoss(BaseImageLoss):

    def __init__(
        self,
        discriminator: KedmiDiscriminator64 | KedmiDiscriminator256,
        *args,
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.discriminator = discriminator

    def forward(self, images, labels, *args, **kwargs):
        _, dis_res = self.discriminator(images)
        logsumup = torch.logsumexp(dis_res, dim=-1)
        # loss = - dis_res.mean()
        loss = torch.mean(F.softplus(logsumup)) - torch.mean(logsumup)
        return loss, {'discriminator loss': loss.item()}


class ComposeImageLoss(BaseImageLoss):

    def __init__(
        self,
        losses: list[BaseImageLoss],
        weights: Optional[list[float]] = None,
        *args,
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)
        if weights is None:
            weights = [1] * len(losses)

        if len(losses) == 0:
            raise RuntimeError(f'losses should be at least one function.')

        if len(weights) != len(losses):
            raise RuntimeError(
                f'Expect the equal length of losses and weights, but found the fronter {len(losses)} and the latter {len(weights)}.'
            )

        self.losses = losses
        self.weights = weights

    def forward(self, images: Tensor, labels: LongTensor, *args, **kwargs):

        compose_loss = 0.0
        return_dict = OrderedDict()
        return_dict['compose loss'] = 0

        for lossfn, weight in zip(self.losses, self.weights):

            loss = lossfn(images, labels, *args, **kwargs)
            if not isinstance(loss, Tensor):
                loss, single_dict = loss
                for k, v in single_dict.items():
                    return_dict[k] = v
            compose_loss += weight * loss

        return_dict['compose loss'] = compose_loss.item()

        return compose_loss, return_dict


class ImagePixelPriorLoss(BaseImageLoss):

    def __init__(
        self, l1_weight: float = 0, l2_weight: float = 0, *args, **kwargs
    ) -> None:
        super().__init__(*args, **kwargs)

        self.l1_weight = l1_weight
        self.l2_weight = l2_weight

    def forward(self, images: Tensor, labels: LongTensor, *args, **kwargs):
        l1_loss = images.abs().mean()
        l2_loss = torch.norm(images).mean()
        loss = l1_loss * self.l1_weight + l2_loss * self.l2_weight
        return loss, OrderedDict(
            [
                ['l1 loss', l1_loss.item()],
                ['l2 loss', l2_loss.item()],
                ['loss', loss.item()],
            ]
        )


class ImageVariationPriorLoss(BaseImageLoss):

    def __init__(
        self, l1_weight: float = 0, l2_weight: float = 0, *args, **kwargs
    ) -> None:
        super().__init__(*args, **kwargs)

        self.l1_weight = l1_weight
        self.l2_weight = l2_weight

    def forward(self, images: Tensor, labels: LongTensor, *args, **kwargs):

        diff1 = images[..., :, :-1] - images[..., :, 1:]
        diff2 = images[..., :-1, :] - images[..., 1:, :]
        diff3 = images[..., 1:, :-1] - images[..., :-1, 1:]
        diff4 = images[..., :-1, :-1] - images[..., 1:, 1:]

        loss_var_l2 = (
            torch.norm(diff1)
            + torch.norm(diff2)
            + torch.norm(diff3)
            + torch.norm(diff4)
        ) / 4
        loss_var_l1 = (
            diff1.abs().mean()
            + diff2.abs().mean()
            + diff3.abs().mean()
            + diff4.abs().mean()
        ) / 4
        loss = loss_var_l1 * self.l1_weight + loss_var_l2 * self.l2_weight
        return loss, OrderedDict(
            [
                ['l1 var loss', loss_var_l1.item()],
                ['l2 var loss', loss_var_l2.item()],
                ['loss', loss.item()],
            ]
        )


class DeepInversionBatchNormPriorLoss(BaseImageLoss):

    def __init__(self, model, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)

        self.feature_hooks: list[DeepInversionBNFeatureHook] = []

        def _find_bn_fn(module):
            if isinstance(module, nn.BatchNorm2d):
                self.feature_hooks.append(DeepInversionBNFeatureHook(module))

        traverse_module(model, _find_bn_fn, call_middle=True)

    def forward(self, images: Tensor, labels: LongTensor, *args, **kwargs):

        r_features_losses = [hook.get_feature() for hook in self.feature_hooks]
        r_features_losses = [l.sum() for l in r_features_losses if l is not None]
        loss = sum(r_features_losses)
        return loss, OrderedDict(loss=loss.item())


class MultiModelOutputKLLoss(BaseImageLoss):

    def __init__(
        self,
        teacher: BaseImageClassifier,
        students: BaseImageClassifier | list[BaseImageClassifier],
        *args,
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)

        self.teacher = teacher
        if isinstance(students, BaseImageClassifier):
            students = [students]
        self.students = students

    def forward(self, images: Tensor, labels: LongTensor, *args, **kwargs):
        T = 3.0
        output_teacher = self.teacher(images)
        Q = nn.functional.softmax(output_teacher / T, dim=1)
        Q = torch.clamp(Q, 0.01, 0.99)

        loss = 0
        for student in self.students:
            output_student = student(images)

            # Jensen Shanon divergence:
            # another way to force KL between negative probabilities
            P = nn.functional.softmax(output_student / T, dim=1)
            M = 0.5 * (P + Q)

            P = torch.clamp(P, 0.01, 0.99)
            M = torch.clamp(M, 0.01, 0.99)
            eps = 0.0
            loss_verifier_cig = 0.5 * F.kl_div(torch.log(P + eps), M) + 0.5 * F.kl_div(
                torch.log(Q + eps), M
            )
            # JS criteria - 0 means full correlation, 1 - means completely different
            loss_verifier_cig = 1.0 - torch.clamp(loss_verifier_cig, 0.0, 1.0)
            loss += loss_verifier_cig

        return loss, {'loss': loss.item()}
