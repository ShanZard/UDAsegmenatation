from datetime import datetime
from operator import imod
import os
import os.path as osp
import numpy as np 
import random

# PyTorch includes
import torch
from torchvision import transforms
from torch.utils.data import DataLoader
import argparse
import yaml
from train_process import Trainer
import torch.nn 
import torch.backends.cudnn as cudnn
# Custom includes
from dataloaders import dataloader as DL
from dataloaders import custom_transforms as tr
from models import create_model
# from model import get_model
from networks.deeplabv2 import get_deeplab_v2,get_fc_discriminator
here = osp.dirname(osp.abspath(__file__))

def main():
    parser = argparse.ArgumentParser(
            formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        )
    parser.add_argument('--gpus', type=list, default=[0,1], help='gpu id')
    parser.add_argument('--resume', default=None, help='checkpoint path')

    # configurations (same configuration as original work)
    # https://github.com/shelhamer/fcn.berkeleyvision.org
    parser.add_argument(
        '--datasetdir', type=str, default='/root/root/DAdataset/dadataset', help='test folder id contain images ROIs to test'
    )
    parser.add_argument(
        '--batch-size', type=int, default=8, help='batch size for training the model'
    )
    parser.add_argument(
        '--group-num', type=int, default=1, help='group number for group normalization'
    )
    parser.add_argument(
        '--max-epoch', type=int, default=200, help='max epoch'
    )
    parser.add_argument(
        '--stop-epoch', type=int, default=200, help='stop epoch'
    )
    parser.add_argument(
        '--interval-validate', type=int, default=1, help='interval epoch number to valide the model'
    )
    parser.add_argument(
        '--lr-model', type=float, default=2e-4, help='learning rate'
    )
    parser.add_argument(
        '--seed',type=int,default=26,help='set random seed'
    )
    parser.add_argument(
        '--lr-decrease-rate', type=float, default=0.95, help='ratio multiplied to initial lr',
    )
    parser.add_argument(
        '--weight-decay', type=float, default=0.0005, help='weight decay',
    )
    parser.add_argument(
        '--momentum', type=float, default=0.99, help='momentum',
    )
    parser.add_argument(
        '--warmup_epoch',type=int,default=-1,help='warmup_epoch'
    )
    parser.add_argument(
        '--pretrain',type=str,default='/root/BEAL/prepth/DeepLab_resnet_pretrained_imagenet.pth',help='warmup_epoch'
    )   
    parser.add_argument(
        '--datasetdirT',type=str,default='/root/root/DAdataset/VOC',help='warmup_epoch'
    ) 
    args = parser.parse_args()


    now = datetime.now()
    args.out = osp.join(here, 'logs', now.strftime('%Y%m%d_%H%M%S.%f'))

    os.makedirs(args.out)
    with open(osp.join(args.out, 'config.yaml'), 'w') as f:
        yaml.safe_dump(args.__dict__, f, default_flow_style=False)



    cuda = torch.cuda.is_available()
    torch.cuda.device_count()
    os.environ['CUDA_VISIBLE_DEVICES'] = str(args.gpus)
    torch.manual_seed(args.seed)
    if cuda:
        torch.cuda.manual_seed_all(args.seed)
    random.seed(args.seed)
    np.random.seed(args.seed)


    # 1. dataset
    composed_transforms_tr = transforms.Compose([
        tr.RandomScaleCrop(512),
        tr.RandomRotate(),
        tr.RandomFlip(),
        tr.elastic_transform(),
        tr.add_salt_pepper_noise(),
        tr.adjust_light(),
        tr.eraser(),
        tr.Normalize_tf(),        
        tr.ToTensor()
    ])

    composed_transforms_ts = transforms.Compose([
        tr.RandomScaleCrop(512),
        tr.Normalize_tf(),
        tr.ToTensor()
    ])

    mydataset = DL.Segmentation(base_dir=args.datasetdir, split='train',
                                                         transform=composed_transforms_tr)
    mydataloader = DataLoader(mydataset, batch_size=args.batch_size, shuffle=True, num_workers=18, pin_memory=True)
    mydatasetT=DL.Segmentation(base_dir=args.datasetdirT, split='train',
                                                         transform=composed_transforms_tr)
    mydataloaderT=DataLoader(mydatasetT,batch_size=args.batch_size, shuffle=True, num_workers=18, pin_memory=True)
    mydataset_val = DL.Segmentation( base_dir=args.datasetdirT ,split='test',
                                       transform=composed_transforms_ts)
    mydataloader_val = DataLoader(mydataset_val, batch_size=args.batch_size, shuffle=False, num_workers=18, pin_memory=True)

    # 2. model
    assert osp.exists(args.pretrain), f'Missing init model {args.pretrain}'

    model = get_deeplab_v2(2, multi_level=True)
    saved_state_dict = torch.load(args.pretrain)
    if 'DeepLab_resnet_pretrained_imagenet' in args.pretrain:
            new_params = model.state_dict().copy()
            for i in saved_state_dict:
                i_parts = i.split('.')
                if not i_parts[1] == 'layer5':
                    new_params['.'.join(i_parts[1:])] = saved_state_dict[i]
            model.load_state_dict(new_params)
    else:
            model.load_state_dict(saved_state_dict)
  
    print('Model loaded')  
    #model=create_model('DeepLabV3Plus',encoder_name='resnet34', encoder_depth=5, encoder_weights='imagenet', encoder_output_stride=16, decoder_channels=256, decoder_atrous_rates=(12, 24, 36), in_channels=3, classes=2, activation=None, upsampling=4, aux_params=None)
    model=torch.nn.DataParallel(model.cuda(),device_ids=args.gpus)
    cudnn.benchmark = True
    cudnn.enabled = True

    # model.cuda()
    d_aux = get_fc_discriminator(num_classes=2)
    d_aux=torch.nn.DataParallel(d_aux.cuda(),device_ids=args.gpus)

    # seg maps, i.e. output, level
    d_main = get_fc_discriminator(num_classes=2)
    d_main=torch.nn.DataParallel(d_main.cuda(),device_ids=args.gpus)




    start_epoch = 0
    start_iteration = 0

    # 3. optimizer

    optim_model =torch.optim.SGD(model.parameters(),
                          lr=2.5e-4,
                          momentum=0.9,
                          weight_decay=0.0005)
    optim_dm=torch.optim.Adam(
        d_main.parameters(),lr=1e-4,
        betas=(0.9,0.99)
    )
    optim_da=torch.optim.Adam(
        d_aux.parameters(),lr=1e-4,
        betas=(0.9,0.99)
    )
    if args.resume:
        checkpoint = torch.load(args.resume)
        pretrained_dict = checkpoint['model_state_dict']
        model_dict = model.state_dict()
        # 1. filter out unnecessary keys
        pretrained_dict = {k: v for k, v in pretrained_dict.items() if k in model_dict}
        # 2. overwrite entries in the existing state dict
        model_dict.update(pretrained_dict)
        # 3. load the new state dict
        model.load_state_dict(model_dict)
        start_epoch = checkpoint['epoch'] + 1
        start_iteration = checkpoint['iteration'] + 1
        optim_model.load_state_dict(checkpoint['optim_state_dict'])

    trainer = Trainer.Trainer(
        cuda=cuda,
        model=model,
        d_main=d_main,
        d_aux=d_aux,
        optimizer_model=optim_model,
        optimdm=optim_dm,
        optimda=optim_da,
        lr_gen=args.lr_model,
        lr_decrease_rate=args.lr_decrease_rate,
        loader=mydataloader,
        loaderT=mydataloaderT,
        val_loader=mydataloader_val,
        out=args.out,
        max_epoch=args.max_epoch,
        stop_epoch=args.stop_epoch,
        interval_validate=args.interval_validate,
        batch_size=args.batch_size,
        warmup_epoch=args.warmup_epoch,
    )
    trainer.epoch = start_epoch
    trainer.iteration = start_iteration
    trainer.train()

if __name__ == '__main__':
    main()
