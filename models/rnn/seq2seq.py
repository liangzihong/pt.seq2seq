import torch
import torch.nn as nn
import torch.nn.functional as F
from const import *
from .encdec import Encoder, AttnDecoder


class Seq2Seq(nn.Module):
    """ Seq2Seq model
        - vanilla seq2seq
        - seq2seq + attention
    """
    def __init__(self, in_dim, emb_dim, h_dim, out_dim, enc_layers, dec_layers,
                 enc_bidirect, attention, max_len, dropout):
        super().__init__()
        self.encoder = Encoder(in_dim, emb_dim, h_dim, enc_layers,
                               bidirect=enc_bidirect, dropout=dropout)
        enc_h_dim = h_dim * self.encoder.n_direct
        self.decoder = AttnDecoder(emb_dim, h_dim, out_dim, enc_h_dim, dec_layers,
                                   attention=attention, dropout=dropout)
        self.max_len = max_len

    def forward(self, src, src_lens, tgt, tgt_lens, teacher_forcing):
        B = src.size(0)
        # encoder
        # enc_out: every encoder hiddens
        # context: last encoder hidden
        enc_out, context = self.encoder(src, src_lens)

        # decoder
        dec_in = torch.full([B, 1], SOS_idx, dtype=torch.long, device='cuda')
        dec_h = context
        use_teacher_forcing = torch.rand(1).item() < teacher_forcing
        # [B, enc_len]
        # src_len == MAX_LENGTH, but we only need to enc_max_len (== enc_out.size(1))
        #enc_max_len = enc_out.size(1) # == max(src_lens)
        enc_max_len = src_lens.max()
        attn_mask = (src[:, :enc_max_len] != PAD_idx).unsqueeze_(1) # [B, 1, src_len]
        attn_ws = []

        if use_teacher_forcing:
            # Teacher forcing: Feed the target as the next input
            dec_in = tgt[:, :-1]
            # attn_w: [B, dec_len, enc_len]
            dec_outs, dec_h, attn_ws = self.decoder(dec_in, dec_h, enc_out, attn_mask)
        else:
            # Without Teacher forcing: use its own predictions as the next input
            # tgt_lens include SOS and EOS => -1
            # max_len does not include SOS and EOS => +1
            dec_max_len = tgt_lens.max()-1 if tgt_lens is not None else self.max_len+1
            dec_outs = []
            for i in range(dec_max_len):
                # [B, 1, out_lang.n_words], [1, B, h_dim], [B, 1, enc_len]
                dec_out, dec_h, attn_w = self.decoder(dec_in, dec_h, enc_out, attn_mask)
                topv, topi = dec_out.topk(1) # [B, 1, 1]
                dec_in = topi.squeeze(2).detach() # [B, 1]

                dec_outs.append(dec_out)
                attn_ws.append(attn_w)

            dec_outs = torch.cat(dec_outs, dim=1)
            attn_ws = torch.cat(attn_ws, dim=1)

        return dec_outs, attn_ws

    def generate(self, src, src_lens):
        return self.forward(src, src_lens, None, None, teacher_forcing=0.)
