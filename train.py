import pickle
import random
import os
import sys
import argparse
import time

import torch
import torch.nn as nn
import torch.nn.functional as F
#from torch.utils.tensorboard import SummaryWriter
from tensorboardX import SummaryWriter
import torch.optim as optim
import numpy as np
from yaml_config import YAMLConfig
from warmup import WarmupLR

import utils
from models import get_model
from evaluate import random_eval, evaluateAndShowAttentions
from logger import Logger
from const import *
from bleu import BLEU
from dataset import get_data


### Setup: load config, logger, and tb writer ###
# arg parser
parser = YAMLConfig.default_parser("Seq2Seq")
parser.add_argument("--param_tracing", action="store_true", default=False)
parser.add_argument("--log_lv", default="info")
cfg, args = YAMLConfig.from_parser(parser)

# prepare
timestamp = utils.timestamp()
utils.makedirs('logs')
utils.makedirs('runs')

# logger
logger_path = os.path.join('logs', "{}_{}.log".format(timestamp, args.name))
logger = Logger.get(file_path=logger_path, level=args.log_lv)
# tb
tb_path = os.path.join('runs', "{}_{}".format(timestamp, args.name))
writer = SummaryWriter(tb_path)


def get_lens(tensor, max_len):
    """ get first position (index) of EOS_idx in tensor
        = length of each sentence
    tensor: [B, T]
    """
    # assume that former idx coming earlier in nonzero().
    # tensor 가 [B, T] 이므로 nonzero 함수도 [i, j] 형태의 tuple 을 결과로 내놓는데,
    # 이 결과가 i => j 순으로 sorting 되어 있다고 가정.
    # e.g) nonzero() => [[1,1], [1,2], [2,1], [2,3], [2,5], ...]

    lens = torch.full([tensor.size(0)], max_len, dtype=torch.long).cuda()
    nz = (tensor == EOS_idx).nonzero()

    if nz.numel() > 0:
        is_first = nz[:-1, 0] != nz[1:, 0]
        is_first = torch.cat([torch.cuda.ByteTensor([1]), is_first]) # first mask

        # convert is_first from mask to indice by nonzero()
        first_nz = nz[is_first.nonzero().flatten()]
        lens[first_nz[:, 0]] = first_nz[:, 1]

    return lens


def parse(example):
    src, src_lens = example.src
    tgt, tgt_lens = example.trg
    return src, src_lens, tgt, tgt_lens


def train(loader, seq2seq, optimizer, lr_scheduler, criterion, teacher_forcing, epoch,
          grad_clip=0.):
    losses = utils.AverageMeter()
    ppls = utils.AverageMeter()
    seq2seq.train()
    N = len(loader)

    for i, example in enumerate(loader):
        src, src_lens, tgt, tgt_lens = parse(example)
        B = src.size(0)

        dec_outs, attn_ws = seq2seq(src, src_lens, tgt, tgt_lens, teacher_forcing)

        optimizer.zero_grad()
        loss, ppl = criterion(dec_outs, tgt[:, 1:]) # remove <sos>
        loss.backward()
        if grad_clip > 0.:
            torch.nn.utils.clip_grad_norm_(seq2seq.parameters(), grad_clip)
        optimizer.step()

        losses.update(loss, B)
        ppls.update(ppl, B)
        cur_step = N*epoch + i
        writer.add_scalar('train/loss', loss, cur_step)
        writer.add_scalar('train/ppl', ppl, cur_step)
        writer.add_scalar('train/lr', optimizer.param_groups[0]["lr"], cur_step)

        # step lr scheduler
        if isinstance(lr_scheduler, optim.lr_scheduler.ReduceLROnPlateau):
            lr_scheduler.step(val_loss)
        else:
            lr_scheduler.step()

    return losses.avg, ppls.avg


def evaluate(loader, seq2seq, criterion, max_len):
    losses = utils.AverageMeter()
    ppls = utils.AverageMeter()
    seq2seq.eval()
    bleu = BLEU()

    tot_st = time.time()
    bleu_time = 0.

    with torch.no_grad():
        for i, example in enumerate(loader):
            src, src_lens, tgt, tgt_lens = parse(example)
            B = src.size(0)

            dec_outs, attn_ws = seq2seq(src, src_lens, tgt, tgt_lens, teacher_forcing=0.)
            loss, ppl = criterion(dec_outs, tgt[:, 1:])
            losses.update(loss, B)
            ppls.update(ppl, B)

            # BLEU
            bleu_st = time.time()
            # convert logits to preds
            preds = dec_outs.max(-1)[1]
            # get pred lens by finding EOS token
            pred_lens = get_lens(preds, max_len)

            for pred, target, pred_len, target_len in zip(preds, tgt, pred_lens, tgt_lens):
                # target_len include SOS & EOS token => 1:target_len-1.
                bleu.add_sentence(pred[:pred_len].cpu().numpy(), target[1:target_len-1].cpu().numpy())

            bleu_time += time.time() - bleu_st
    total_time = time.time() - tot_st

    logger.debug("TIME: tot = {:.3f}\t bleu = {:.3f}".format(total_time, bleu_time))

    return losses.avg, ppls.avg, bleu.score()


def criterion(logits, targets):
    """ Cross-entropy with intra-batch summation and inter-batch meaning
    logits: [B, max_len, out_lang.n_words]
    targets: [B, max_len]
    """
    losses = F.cross_entropy(logits.flatten(end_dim=1), targets.flatten(), reduction='none',
                             ignore_index=PAD_idx)
    losses = losses.view(targets.shape)

    sum_loss = losses.sum(1).mean()
    n_words = (targets != PAD_idx).sum() # # of words without padding
    avg_loss = losses.sum() / n_words
    perplexity = torch.exp(avg_loss).item()

    return sum_loss, perplexity


if __name__ == "__main__":
    ### configuration
    logger.info("### Configuration ###")
    logger.nofmt(cfg.str())
    writer.add_text("config", cfg.markdown())
    # train
    batch_size = cfg['train']['batch_size']
    epochs = cfg['train']['epochs']
    teacher_forcing = cfg['train']['teacher_forcing']
    grad_clip = cfg['train']['grad_clip']
    # model
    model_type = cfg['model']['type']
    # eval
    N_eval = cfg['eval']['N']
    VIZ_ATTN = cfg['eval']['viz_attn']
    # data (preproc)
    data_name = cfg['data']['name']
    max_len = cfg['data']['max_len']
    min_freq = cfg['data']['min_freq']

    ### data
    logger.info("Loading data ...")
    batch_sort = model_type == 'rnn' # sort batch by length only for RNN seq packing
    train_loader, valid_loader, test_loader = get_data(data_name, max_len, min_freq, batch_size,
                                                       batch_sort)
    train_data = train_loader.dataset
    valid_data = valid_loader.dataset
    SRC = train_data.fields['src']
    TRG = train_data.fields['trg']
    in_dim = len(SRC.vocab)
    out_dim = len(TRG.vocab)
    # setup eval data
    L = len(valid_data)
    eval_batch = [valid_data[i] for i in range(L//4-1, L, L//4)]

    ### build model
    logger.info("### Build model ###")
    seq2seq = get_model(model_type, in_dim, out_dim, max_len, cfg['model'])
    seq2seq.cuda()

    ### init params
    # NOTE no bias init ...
    for p in seq2seq.parameters():
        if p.dim() > 1:
            nn.init.xavier_uniform_(p)

    K = 1024
    n_params = utils.num_params(seq2seq) / K / K
    logger.nofmt(seq2seq)
    logger.info("# of params = {:.1f} M".format(n_params))

    # parameter size tracing
    if args.param_tracing:
        # recursive tracing
        def param_trace(name, module, depth, max_depth=999, threshold=0):
            if depth > max_depth:
                return
            prefix = "  " * depth
            n_params = utils.num_params(module)
            if n_params > threshold:
                print("{:60s}\t{:10.2f}M".format(prefix + name, n_params / K / K))
            for n, m in module.named_children():
                if depth == 0:
                    child_name = n
                else:
                    child_name = "{}.{}".format(name, n)
                param_trace(child_name, m, depth+1, max_depth, threshold)

        param_trace('seq2seq', seq2seq, 0, max_depth=5, threshold=K*100)
        exit()

    ## optimizer
    T_ep = len(train_loader)
    optimizer = optim.Adam(seq2seq.parameters(), lr=3e-4)
    lr_scheduler = optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=T_ep*epochs, eta_min=3e-6)
    if 'warmup' in cfg['train']:
        warmup_ep = cfg['train']['warmup']
        lr_scheduler = WarmupLR(optimizer, init_scale=1e-3, T_max=T_ep*warmup_ep,
                                after=lr_scheduler)

    ## training
    if VIZ_ATTN:
        utils.makedirs('evals')
        evaluateAndShowAttentions(eval_batch, seq2seq, valid_data, epoch=0, print_attn=True,
                                  writer=writer)

    best_ppl = utils.BestTracker('min')
    best_loss = utils.BestTracker('min')
    best_bleu = utils.BestTracker('max')
    for epoch in range(epochs):
        logger.info("Epoch {}/{}, LR = {}".format(epoch+1, epochs, optimizer.param_groups[0]["lr"]))

        # train
        trn_loss, trn_ppl = train(train_loader, seq2seq, optimizer, lr_scheduler, criterion,
                                  teacher_forcing=teacher_forcing, epoch=epoch, grad_clip=grad_clip)
        logger.info("\ttrain: Loss {:7.3f}  PPL {:7.3f}".format(trn_loss, trn_ppl))

        # validation
        val_loss, val_ppl, val_bleu = evaluate(valid_loader, seq2seq, criterion, max_len)
        logger.info("\tvalid: Loss {:7.3f}  PPL {:7.3f}  BLEU {:7.3f}".format(
            val_loss, val_ppl, val_bleu))

        cur_step = len(train_loader) * (epoch+1)
        writer.add_scalar('val/loss', val_loss, cur_step)
        writer.add_scalar('val/ppl', val_ppl, cur_step)
        writer.add_scalar('val/bleu', val_bleu, cur_step)

        best_ppl.check(val_ppl, epoch+1)
        best_loss.check(val_loss, epoch+1)
        best_bleu.check(val_bleu, epoch+1)

        # evaluation & attention visualization
        logger.info("Random eval:")
        random_eval(valid_data, seq2seq, N_eval)
        if VIZ_ATTN:
            evaluateAndShowAttentions(eval_batch, seq2seq, valid_data, epoch=epoch+1,
                                      print_attn=True, writer=writer)
        logger.info("")

    logger.info("Name: {}".format(args.name))
    logger.info("Best: Loss {loss.val:7.3f} ({loss.ep})  PPL {ppl.val:7.3f} ({ppl.ep})  "
                "BLEU {bleu.val:7.3f} ({bleu.ep})".format(
                    loss=best_loss, ppl=best_ppl, bleu=best_bleu))
