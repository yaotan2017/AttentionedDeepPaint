import torch
import torch.nn as nn
import torch.optim as optim

import random

from torchvision import transforms
from torchvision import models

from PIL import Image

from trainer.trainer import ModelTrainer

from models import PatchGAN
from models import AttentionPaintGenerator

from utils import GANLoss
from utils import load_checkpoints, save_checkpoints
from utils import AverageTracker, ImagePooling

from preprocess import re_scale, scale
from preprocess import save_image


class AttentionPaintTrainer(ModelTrainer):
    def __init__(self, *args):
        super(AttentionPaintTrainer, self).__init__(*args)

        # build model
        self.resolution = self.args.resolution
        self.generator = AttentionPaintGenerator().to(self.device)
        self.discriminator = PatchGAN(
            dim=64, sigmoid=self.args.no_mse).to(self.device)

        # set optimizers
        self.optimizers = self._set_optimizers()

        # set loss functions
        self.losses = self._set_losses()

        # set vgg
        vgg = models.vgg19_bn(True).to(self.device)
        self.vgg_features = vgg.features
        self.vgg_fc1 = vgg.classifier[0]
        for param in self.vgg_features.parameters():
            param.requires_grad = False
        for param in self.vgg_fc1.parameters():
            param.requires_grad = False

        # set image pooler
        self.image_pool = ImagePooling(50)

        # load pretrained model
        if self.args.pretrainedG != '':
            if self.args.verbose:
                print('load pretrained generator...')
            load_checkpoints(self.args.pretrainedG, self.generator,
                             self.optimizers['G'])
        if self.args.pretrainedD != '':
            if self.args.verbose:
                print('load pretrained discriminator...')
            load_checkpoints(self.args.pretrainedD, self.discriminator,
                             self.optimizers['D'])

        if self.device.type == 'cuda':
            # enable parallel computation
            self.generator = nn.DataParallel(self.generator)
            self.discriminator = nn.DataParallel(self.discriminator)

        # loss values for tracking
        self.loss_G_gan = AverageTracker('loss_G_gan')
        self.loss_G_l1 = AverageTracker('loss_G_l1')
        self.loss_D_real = AverageTracker('loss_D_real')
        self.loss_D_fake = AverageTracker('loss_D_fake')

        # image value
        self.imageA = None
        self.imageB = None
        self.fakeB = None

    def train(self, last_iteration):
        """
        Run single epoch
        """
        average_trackers = [
            self.loss_G_gan, self.loss_D_fake, self.loss_D_real, self.loss_G_l1
        ]
        self.generator.train()
        self.discriminator.train()
        for tracker in average_trackers:
            tracker.initialize()
        for i, datas in enumerate(self.data_loader, last_iteration):
            imageA, imageB, colors, resized = datas
            if self.args.mode == 'B2A':
                # swap
                imageA, imageB = imageB, imageA

            self.imageA = imageA.to(self.device)
            self.imageB = imageB.to(self.device)
            colors = colors.to(self.device)
            resized = resized.to(self.device)

            # run forward propagation
            self.fakeB = self.generator(
                self.imageA,
                colors,
                self.extract_vgg_features(resized),
            )

            self._update_discriminator()
            self._update_generator()

            if self.args.verbose and i % self.args.print_every == 0:
                print('%s = %f, %s = %f, %s = %f, %s = %f' % (
                    self.loss_D_real.name,
                    self.loss_D_real(),
                    self.loss_D_fake.name,
                    self.loss_D_fake(),
                    self.loss_G_gan.name,
                    self.loss_G_gan(),
                    self.loss_G_l1.name,
                    self.loss_G_l1(),
                ))

        return i

    def validate(self, dataset, epoch, samples=3):
        #  self.generator.eval()
        #  self.discriminator.eval()
        length = len(dataset)

        # sample images
        idxs = random.sample(range(0, length - 1), samples * 2)
        styles = idxs[samples:]
        targets = idxs[0:samples]

        result = Image.new('RGB',
                           (5 * self.resolution, samples * self.resolution))

        toPIL = transforms.ToPILImage()

        G_loss_gan = []
        G_loss_l1 = []
        D_loss_real = []
        D_loss_fake = []
        l1_loss = self.losses['L1']
        gan_loss = self.losses['GAN']
        for i, (target, style) in enumerate(zip(targets, styles)):
            sub_result = Image.new('RGB',
                                   (5 * self.resolution, self.resolution))
            imageA, imageB, _, _ = dataset[target]
            styleA, styleB, colors, resized = dataset[style]

            if self.args.mode == 'B2A':
                imageA, imageB = imageB, imageA
                styleA, styleB = styleB, styleA

            imageA = imageA.unsqueeze(0).to(self.device)
            imageB = imageB.unsqueeze(0).to(self.device)
            styleB = styleB.unsqueeze(0).to(self.device)
            colors = colors.unsqueeze(0).to(self.device)
            resized = resized.unsqueeze(0).to(self.device)

            with torch.no_grad():
                fakeB = self.generator(
                    imageA,
                    colors,
                    self.extract_vgg_features(resized),
                )
                fakeAB = torch.cat([imageA, fakeB], 1)
                realAB = torch.cat([imageA, imageB], 1)

                G_loss_l1.append(l1_loss(fakeB, imageB).item())
                G_loss_gan.append(
                    gan_loss(self.discriminator(fakeAB), True).item())

                D_loss_real.append(
                    gan_loss(self.discriminator(realAB), True).item())
                D_loss_fake.append(
                    gan_loss(self.discriminator(fakeAB), False).item())

            styleB = styleB.squeeze()
            fakeB = fakeB.squeeze()
            imageA = imageA.squeeze()
            imageB = imageB.squeeze()
            colors = colors.squeeze()

            imageA = toPIL(re_scale(imageA).detach().cpu())
            imageB = toPIL(re_scale(imageB).detach().cpu())
            styleB = toPIL(re_scale(styleB).detach().cpu())
            fakeB = toPIL(re_scale(fakeB).detach().cpu())

            # synthesize top-4 colors
            color1 = toPIL(re_scale(colors[0:3].detach().cpu()))
            color2 = toPIL(re_scale(colors[3:6].detach().cpu()))
            color3 = toPIL(re_scale(colors[6:9].detach().cpu()))
            color4 = toPIL(re_scale(colors[9:12].detach().cpu()))

            color_result = Image.new('RGB', (self.resolution, self.resolution))
            color_result.paste(
                color1.crop((0, 0, self.resolution, self.resolution // 4)),
                (0, 0))
            color_result.paste(
                color2.crop((0, 0, self.resolution, self.resolution // 4)),
                (0, 512 // 4))
            color_result.paste(
                color3.crop((0, 0, self.resolution, self.resolution // 4)),
                (0, 512 // 4 * 2))
            color_result.paste(
                color4.crop((0, 0, self.resolution, self.resolution // 4)),
                (0, 512 // 4 * 3))

            sub_result.paste(imageA, (0, 0))
            sub_result.paste(styleB, (512, 0))
            sub_result.paste(fakeB, (2 * 512, 0))
            sub_result.paste(imageB, (3 * 512, 0))
            sub_result.paste(color_result, (4 * 512, 0))

            result.paste(sub_result, (0, 0 + self.resolution * i))

        print(
            'Validate D_loss_real = %f, D_loss_fake = %f, G_loss_l1 = %f, G_loss_gan = %f'
            % (
                sum(D_loss_real) / samples,
                sum(D_loss_fake) / samples,
                sum(G_loss_l1) / samples,
                sum(G_loss_gan) / samples,
            ))

        save_image(result, 'attentionpaint_val_%03d' % epoch,
                   './data/pair_niko/result')

    def test(self):
        raise NotImplementedError

    def save_model(self, name, epoch):
        save_checkpoints(
            self.generator,
            name + 'G',
            epoch,
            optimizer=self.optimizers['G'],
        )
        save_checkpoints(
            self.discriminator,
            name + 'D',
            epoch,
            optimizer=self.optimizers['D'])

    def extract_vgg_features(self, image):
        """
        def resize_in_pil(image):
            image = re_scale(image)
            image = transforms.ToPILImage()(image)
            image = transforms.Resize(224)(image)
            image = transforms.ToTensor()(image)
            image = scale(image)
            return image
        image = image.detach().cpu()
        image = list(map(lambda img: resize_in_pil(img), image))
        image = torch.stack(image, 0).to(self.device)
        """

        with torch.no_grad():
            image = self.vgg_features(image)
            image = image.reshape(image.shape[0], -1)
            image = self.vgg_fc1(image)
        return image

    def _set_optimizers(self):
        optimG = optim.Adam(
            self.generator.parameters(),
            lr=self.args.learning_rate,
            betas=(self.args.beta1, 0.999))
        optimD = optim.Adam(
            self.discriminator.parameters(),
            lr=self.args.learning_rate,
            betas=(self.args.beta1, 0.999))

        return {'G': optimG, 'D': optimD}

    def _set_losses(self):
        gan_loss = GANLoss(not self.args.no_mse).to(self.device)
        l1_loss = nn.L1Loss().to(self.device)
        content_loss = nn.MSELoss().to(self.device)

        return {'GAN': gan_loss, 'L1': l1_loss, 'Content': content_loss}

    def _update_generator(self):
        optimG = self.optimizers['G']
        gan_loss = self.losses['GAN']
        l1_loss = self.losses['L1']
        batch_size = self.imageA.shape[0]

        optimG.zero_grad()
        fake_AB = torch.cat([self.imageA, self.fakeB], 1)
        logit_fake = self.discriminator(fake_AB)
        loss_G_gan = gan_loss(logit_fake, True)

        loss_G_l1 = l1_loss(self.fakeB, self.imageB) * self.args.lambd

        self.loss_G_gan.update(loss_G_gan.item(), batch_size)
        self.loss_G_l1.update(loss_G_l1.item(), batch_size)

        loss_G = loss_G_gan + loss_G_l1

        loss_G.backward()
        optimG.step()

    def _update_discriminator(self):
        optimD = self.optimizers['D']
        gan_loss = self.losses['GAN']
        batch_size = self.imageA.shape[0]

        optimD.zero_grad()

        # for real image
        real_AB = torch.cat([self.imageA, self.imageB], 1)
        logit_real = self.discriminator(real_AB)
        loss_D_real = gan_loss(logit_real, True)
        self.loss_D_real.update(loss_D_real.item(), batch_size)

        # for fake image
        fake_AB = torch.cat([self.imageA, self.fakeB], 1)
        fake_AB = self.image_pool(fake_AB)
        logit_fake = self.discriminator(fake_AB.detach())
        loss_D_fake = gan_loss(logit_fake, False)
        self.loss_D_fake.update(loss_D_fake.item(), batch_size)

        loss_D = (loss_D_real + loss_D_fake) * 0.5
        loss_D.backward()
        optimD.step()
