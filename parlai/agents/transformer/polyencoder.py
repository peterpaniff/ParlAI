#!/usr/bin/env python3
# Copyright (c) Facebook, Inc. and its affiliates.
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

# hack to make sure -m transformer/generator works as expected
"""
Poly-encoder Agent.
"""

from functools import reduce
from typing import List, Optional, Tuple

import torch
from torch import nn

from parlai.agents.image_seq2seq.modules import ContextWithImageEncoder
from parlai.core.torch_ranker_agent import TorchRankerAgent
from .biencoder import AddLabelFixedCandsTRA
from .modules import (
    BasicAttention,
    MultiHeadAttention,
    TransformerEncoder,
    get_n_positions_from_options,
)
from .transformer import TransformerRankerAgent


class PolyencoderAgent(TorchRankerAgent):
    """
    Poly-encoder Agent.

    Equivalent of bert_ranker/polyencoder and biencoder_multiple_output but does not
    rely on an external library (hugging face).
    """

    @classmethod
    def add_cmdline_args(cls, argparser):
        """
        Add command-line arguments specifically for this agent.
        """
        TransformerRankerAgent.add_cmdline_args(argparser)
        agent = argparser.add_argument_group('Polyencoder Arguments')
        agent.add_argument(
            '--polyencoder-type',
            type=str,
            default='codes',
            choices=['codes', 'n_first'],
            help='Type of polyencoder, either we compute'
            'vectors using codes + attention, or we '
            'simply take the first N vectors.',
            recommended='codes',
        )
        agent.add_argument(
            '--polyencoder-image-encoder-num-layers',
            type=int,
            default=0,
            help='If >0, number of linear layers to encode image features with in the context',
        )
        agent.add_argument(
            '--polyencoder-image-features-dim',
            type=int,
            default=2048,
            help='For passing in image features of the given dim in the context',
        )
        agent.add_argument(
            '--polyencoder-image-combination-mode',
            type=str,
            default='postpend',
            choices=['add', 'postpend', 'prepend'],
            help='How to combine image embedding (if used) with context embedding',
        )
        agent.add_argument(
            '--poly-n-codes',
            type=int,
            default=64,
            help='number of vectors used to represent the context'
            'in the case of n_first, those are the number'
            'of vectors that are considered.',
            recommended=64,
        )
        agent.add_argument(
            '--poly-attention-type',
            type=str,
            default='basic',
            choices=['basic', 'sqrt', 'multihead'],
            help='Type of the top aggregation layer of the poly-'
            'encoder (where the candidate representation is'
            'the key)',
            recommended='basic',
        )
        agent.add_argument(
            '--poly-attention-num-heads',
            type=int,
            default=4,
            help='In case poly-attention-type is multihead, '
            'specify the number of heads',
        )

        # Those arguments are here in case where polyencoder type is 'code'
        agent.add_argument(
            '--codes-attention-type',
            type=str,
            default='basic',
            choices=['basic', 'sqrt', 'multihead'],
            help='Type ',
            recommended='basic',
        )
        agent.add_argument(
            '--codes-attention-num-heads',
            type=int,
            default=4,
            help='In case codes-attention-type is multihead, '
            'specify the number of heads',
        )
        return agent

    def __init__(self, opt, shared=None):
        super().__init__(opt, shared)
        self.rank_loss = torch.nn.CrossEntropyLoss(reduce=True, size_average=True)
        if self.use_cuda:
            self.rank_loss.cuda()
        self.data_parallel = opt.get('data_parallel') and self.use_cuda
        if self.data_parallel:
            from parlai.utils.distributed import is_distributed

            if is_distributed():
                raise ValueError('Cannot combine --data-parallel and distributed mode')
            if shared is None:
                self.model = torch.nn.DataParallel(self.model)

    def build_model(self, states=None):
        """
        Return built model.
        """
        return PolyEncoderModule(self.opt, self.dict, self.NULL_IDX)

    def vectorize(self, *args, **kwargs):
        """
        Add the start and end token to the labels.
        """
        kwargs['add_start'] = True
        kwargs['add_end'] = True
        obs = super().vectorize(*args, **kwargs)
        return obs

    def _set_text_vec(self, *args, **kwargs):
        """
        Add the start and end token to the text.
        """
        obs = super()._set_text_vec(*args, **kwargs)
        if 'text_vec' in obs and 'added_start_end_tokens' not in obs:
            obs.force_set(
                'text_vec', self._add_start_end_tokens(obs['text_vec'], True, True)
            )
            obs['added_start_end_tokens'] = True
        return obs

    def vectorize_fixed_candidates(self, *args, **kwargs):
        """
        Vectorize fixed candidates.

        Override to add start and end token when computing the candidate encodings in
        interactive mode.
        """
        kwargs['add_start'] = True
        kwargs['add_end'] = True
        return super().vectorize_fixed_candidates(*args, **kwargs)

    def _make_candidate_encs(self, vecs):
        """
        Make candidate encs.

        The polyencoder module expects cand vecs to be 3D while torch_ranker_agent
        expects it to be 2D. This requires a little adjustment (used in interactive mode
        only)
        """
        rep = super()._make_candidate_encs(vecs)
        return rep.transpose(0, 1).contiguous()

    def encode_candidates(self, padded_cands):
        """
        Encode candidates.
        """
        padded_cands = padded_cands.unsqueeze(1)
        _, _, cand_rep = self.model(cand_tokens=padded_cands)
        return cand_rep

    def score_candidates(self, batch, cand_vecs, cand_encs=None):
        """
        Score candidates.

        The Poly-encoder encodes the candidate and context independently. Then, the
        model applies additional attention before ultimately scoring a candidate.
        """
        bsz = batch.text_vec.size(0)
        ctxt_rep, ctxt_rep_mask, _ = self.model(
            ctxt_tokens=batch.text_vec, ctxt_image=batch.image
        )

        if cand_encs is not None:
            if bsz == 1:
                cand_rep = cand_encs
            else:
                cand_rep = cand_encs.expand(bsz, cand_encs.size(1), -1)
        # bsz x num cands x seq len
        elif len(cand_vecs.shape) == 3:
            _, _, cand_rep = self.model(cand_tokens=cand_vecs)
        # bsz x seq len (if batch cands) or num_cands x seq len (if fixed cands)
        elif len(cand_vecs.shape) == 2:
            _, _, cand_rep = self.model(cand_tokens=cand_vecs.unsqueeze(1))
            num_cands = cand_rep.size(0)  # will be bsz if using batch cands
            cand_rep = cand_rep.expand(num_cands, bsz, -1).transpose(0, 1).contiguous()

        scores = self.model(
            ctxt_rep=ctxt_rep, ctxt_rep_mask=ctxt_rep_mask, cand_rep=cand_rep
        )
        return scores

    def load_state_dict(self, state_dict):
        """
        Override to account for codes.
        """
        if self.model.type == 'codes' and 'codes' not in state_dict:
            state_dict['codes'] = self.model.codes
        super().load_state_dict(state_dict)


class PolyEncoderModule(torch.nn.Module):
    """
    Poly-encoder model.

    See https://arxiv.org/abs/1905.01969 for more details
    """

    def __init__(self, opt, dict, null_idx):
        super(PolyEncoderModule, self).__init__()
        self.null_idx = null_idx
        self.use_image_features = opt.get('polyencoder_image_encoder_num_layers', 0) > 0
        if self.use_image_features:
            self.encoder_ctxt = self.get_context_with_image_encoder(
                opt=opt, dict=dict, null_idx=null_idx
            )
        else:
            self.encoder_ctxt = self.get_encoder(
                opt=opt, dict=dict, null_idx=null_idx, reduction_type=None
            )
        self.encoder_cand = self.get_encoder(opt, dict, null_idx, opt['reduction_type'])

        self.type = opt['polyencoder_type']
        self.n_codes = opt['poly_n_codes']
        self.attention_type = opt['poly_attention_type']
        self.attention_num_heads = opt['poly_attention_num_heads']
        self.codes_attention_type = opt['codes_attention_type']
        self.codes_attention_num_heads = opt['codes_attention_num_heads']
        embed_dim = opt['embedding_size']

        # In case it's a polyencoder with code.
        if self.type == 'codes':
            # experimentally it seems that random with size = 1 was good.
            codes = torch.empty(self.n_codes, embed_dim)
            codes = torch.nn.init.uniform_(codes)
            self.codes = torch.nn.Parameter(codes)

            # The attention for the codes.
            if self.codes_attention_type == 'multihead':
                self.code_attention = MultiHeadAttention(
                    self.codes_attention_num_heads, embed_dim, opt['dropout']
                )
            elif self.codes_attention_type == 'sqrt':
                self.code_attention = PolyBasicAttention(
                    self.type, self.n_codes, dim=2, attn='sqrt', get_weights=False
                )
            elif self.codes_attention_type == 'basic':
                self.code_attention = PolyBasicAttention(
                    self.type, self.n_codes, dim=2, attn='basic', get_weights=False
                )

        # The final attention (the one that takes the candidate as key)
        if self.attention_type == 'multihead':
            self.attention = MultiHeadAttention(
                self.attention_num_heads, opt['embedding_size'], opt['dropout']
            )
        else:
            self.attention = PolyBasicAttention(
                self.type,
                self.n_codes,
                dim=2,
                attn=self.attention_type,
                get_weights=False,
            )

    def get_encoder(self, opt, dict, null_idx, reduction_type):
        """
        Return encoder, given options.

        :param opt:
            opt dict
        :param dict:
            dictionary agent
        :param null_idx:
            null/pad index into dict
        :reduction_type:
            reduction type for the encoder

        :return:
            a TransformerEncoder, initialized correctly
        """
        n_positions = get_n_positions_from_options(opt)
        embeddings = self._get_embeddings(
            dict=dict, null_idx=null_idx, embedding_size=opt['embedding_size']
        )
        return TransformerEncoder(
            n_heads=opt['n_heads'],
            n_layers=opt['n_layers'],
            embedding_size=opt['embedding_size'],
            ffn_size=opt['ffn_size'],
            vocabulary_size=len(dict),
            embedding=embeddings,
            dropout=opt['dropout'],
            attention_dropout=opt['attention_dropout'],
            relu_dropout=opt['relu_dropout'],
            padding_idx=null_idx,
            learn_positional_embeddings=opt['learn_positional_embeddings'],
            embeddings_scale=opt['embeddings_scale'],
            reduction_type=reduction_type,
            n_positions=n_positions,
            n_segments=2,
            activation=opt['activation'],
            variant=opt['variant'],
            output_scaling=opt['output_scaling'],
        )

    def get_context_with_image_encoder(self, opt, dict, null_idx):
        """
        Return encoder that allows for image features to be passed in, given options.

        :param opt:
            opt dict
        :param dict:
            dictionary agent
        :param null_idx:
            null/pad index into dict
        :return:
            a ContextWithImageEncoder, initialized correctly
        """
        n_positions = get_n_positions_from_options(opt)
        embeddings = self._get_embeddings(
            dict=dict, null_idx=null_idx, embedding_size=opt['embedding_size']
        )
        return ContextWithImageEncoder(
            n_heads=opt['n_heads'],
            n_layers=opt['n_layers'],
            embedding_size=opt['embedding_size'],
            ffn_size=opt['ffn_size'],
            vocabulary_size=len(dict),
            embedding=embeddings,
            dropout=opt['dropout'],
            attention_dropout=opt['attention_dropout'],
            relu_dropout=opt['relu_dropout'],
            padding_idx=null_idx,
            learn_positional_embeddings=opt['learn_positional_embeddings'],
            embeddings_scale=opt['embeddings_scale'],
            n_positions=n_positions,
            n_segments=2,
            activation=opt['activation'],
            variant=opt['variant'],
            output_scaling=opt['output_scaling'],
            image_encoder_num_layers=opt['polyencoder_image_encoder_num_layers'],
            image_features_dim=opt['polyencoder_image_features_dim'],
            image_combination_mode=opt['polyencoder_image_combination_mode'],
        )

    def _get_embeddings(self, dict, null_idx, embedding_size):
        embeddings = torch.nn.Embedding(len(dict), embedding_size, padding_idx=null_idx)
        torch.nn.init.normal_(embeddings.weight, 0, embedding_size ** -0.5)
        return embeddings

    def attend(self, attention_layer, queries, keys, values, mask):
        """
        Apply attention.

        :param attention_layer:
            nn.Module attention layer to use for the attention
        :param queries:
            the queries for attention
        :param keys:
            the keys for attention
        :param values:
            the values for attention
        :param mask:
            mask for the attention keys

        :return:
            the result of applying attention to the values, with weights computed
            wrt to the queries and keys.
        """
        if keys is None:
            keys = values
        if isinstance(attention_layer, PolyBasicAttention):
            return attention_layer(queries, keys, mask_ys=mask, values=values)
        elif isinstance(attention_layer, MultiHeadAttention):
            return attention_layer(queries, keys, values, mask)
        else:
            raise Exception('Unrecognized type of attention')

    def encode(
        self,
        ctxt_tokens: Optional[torch.Tensor],
        ctxt_image: Optional[List[Optional[torch.Tensor]]],
        cand_tokens: Optional[torch.Tensor],
    ):
        """
        Encode a text sequence.

        :param ctxt_tokens:
            2D long tensor, batchsize x sent_len
        :param ctxt_image:
            List of tensors (or Nones) of length batchsize
        :param cand_tokens:
            3D long tensor, batchsize x num_cands x sent_len
            Note this will actually view it as a 2D tensor
        :return:
            (ctxt_rep, ctxt_mask, cand_rep)
            - ctxt_rep 3D float tensor, batchsize x n_codes x dim
            - ctxt_mask byte:  batchsize x n_codes (all 1 in case
            of polyencoder with code. Which are the vectors to use
            in the ctxt_rep)
            - cand_rep (3D float tensor) batchsize x num_cands x dim
        """
        cand_embed = None
        ctxt_rep = None
        ctxt_rep_mask = None
        if cand_tokens is not None:
            assert len(cand_tokens.shape) == 3
            bsz = cand_tokens.size(0)
            num_cands = cand_tokens.size(1)
            cand_embed = self.encoder_cand(cand_tokens.view(bsz * num_cands, -1))
            cand_embed = cand_embed.view(bsz, num_cands, -1)

        if ctxt_tokens is not None:
            assert len(ctxt_tokens.shape) == 2
            bsz = ctxt_tokens.size(0)
            # get context_representation. Now that depends on the cases.
            if self.use_image_features is not None:
                assert ctxt_image is None or len(ctxt_image) == bsz
                ctxt_out, ctxt_mask = self.encoder_ctxt(
                    ctxt_tokens, image_features=ctxt_image
                )
            else:
                ctxt_out, ctxt_mask = self.encoder_ctxt(ctxt_tokens)
            dim = ctxt_out.size(2)

            if self.type == 'codes':
                ctxt_rep = self.attend(
                    self.code_attention,
                    queries=self.codes.repeat(bsz, 1, 1),
                    keys=ctxt_out,
                    values=ctxt_out,
                    mask=ctxt_mask,
                )
                ctxt_rep_mask = ctxt_rep.new_ones(bsz, self.n_codes).byte()

            elif self.type == 'n_first':
                # Expand the output if it is not long enough
                if ctxt_out.size(1) < self.n_codes:
                    difference = self.n_codes - ctxt_out.size(1)
                    extra_rep = ctxt_out.new_zeros(bsz, difference, dim)
                    ctxt_rep = torch.cat([ctxt_out, extra_rep], dim=1)
                    extra_mask = ctxt_mask.new_zeros(bsz, difference)
                    ctxt_rep_mask = torch.cat([ctxt_mask, extra_mask], dim=1)
                else:
                    ctxt_rep = ctxt_out[:, 0 : self.n_codes, :]
                    ctxt_rep_mask = ctxt_mask[:, 0 : self.n_codes]

        return ctxt_rep, ctxt_rep_mask, cand_embed

    def score(self, ctxt_rep, ctxt_rep_mask, cand_embed):
        """
        Score the candidates.

        :param ctxt_rep:
            3D float tensor, bsz x ctxt_len x dim
        :param ctxt_rep_mask:
            2D byte tensor, bsz x ctxt_len, in case there are some elements
            of the ctxt that we should not take into account.
        :param cand_embed: 3D float tensor, bsz x num_cands x dim

        :return: scores, 2D float tensor: bsz x num_cands
        """
        # reduces the context representation to a 3D tensor bsz x num_cands x dim
        ctxt_final_rep = self.attend(
            self.attention, cand_embed, ctxt_rep, ctxt_rep, ctxt_rep_mask
        )
        scores = torch.sum(ctxt_final_rep * cand_embed, 2)
        return scores

    def forward(
        self,
        ctxt_tokens=None,
        ctxt_image=None,
        cand_tokens=None,
        ctxt_rep=None,
        ctxt_rep_mask=None,
        cand_rep=None,
    ):
        """
        Forward pass of the model.

        Due to a limitation of parlai, we have to have one single model
        in the agent. And because we want to be able to use data-parallel,
        we need to have one single forward() method.
        Therefore the operation_type can be either 'encode' or 'score'.

        :param ctxt_tokens:
            tokenized contexts
        :param ctxt_image:
            image features in context
        :param cand_tokens:
            tokenized candidates
        :param ctxt_rep:
            (bsz x num_codes x hsz)
            encoded representation of the context. If self.type == 'codes', these
            are the context codes. Otherwise, they are the outputs from the
            encoder
        :param ctxt_rep_mask:
            mask for ctxt rep
        :param cand_rep:
            encoded representation of the candidates
        """
        if ctxt_tokens is not None or ctxt_image is not None or cand_tokens is not None:
            return self.encode(
                ctxt_tokens=ctxt_tokens, ctxt_image=ctxt_image, cand_tokens=cand_tokens
            )
        elif (
            ctxt_rep is not None and ctxt_rep_mask is not None and cand_rep is not None
        ):
            return self.score(ctxt_rep, ctxt_rep_mask, cand_rep)
        raise Exception('Unsupported operation')


class NewContextWithImageEncoder(TransformerEncoder):
    """Encodes image features and context, and combines by summing or concatenation."""

    def __init__(
        self,
        n_heads,
        n_layers,
        embedding_size,
        ffn_size,
        vocabulary_size,
        embedding=None,
        dropout=0.0,
        attention_dropout=0.0,
        relu_dropout=0.0,
        padding_idx=0,
        learn_positional_embeddings=False,
        embeddings_scale=False,
        n_positions=1024,
        activation='relu',
        variant='aiayn',
        n_segments=0,
        output_scaling=1.0,
        image_encoder_num_layers=1,
        image_features_dim=2048,
        image_combination_mode='postpend',
    ):

        self.n_img_layers = image_encoder_num_layers
        self.img_dim = image_features_dim
        self.image_combination_mode = image_combination_mode
        reduction_type = None  # Must pass back unreduced encoding and mask
        super().__init__(
            n_heads=n_heads,
            n_layers=n_layers,
            embedding_size=embedding_size,
            ffn_size=ffn_size,
            vocabulary_size=vocabulary_size,
            embedding=embedding,
            dropout=dropout,
            attention_dropout=attention_dropout,
            relu_dropout=relu_dropout,
            padding_idx=padding_idx,
            learn_positional_embeddings=learn_positional_embeddings,
            embeddings_scale=embeddings_scale,
            reduction_type=reduction_type,
            n_positions=n_positions,
            activation=activation,
            variant=variant,
            n_segments=n_segments,
            output_scaling=output_scaling,
        )
        self._build_image_encoder()
        self.dummy_image_enc = torch.nn.Parameter(
            torch.zeros((self.embedding_size)), requires_grad=False
        )
        self.ones_mask = torch.nn.Parameter(torch.ones(1).bool(), requires_grad=False)

    def _build_image_encoder(self):
        image_layers = [nn.Linear(self.img_dim, self.embedding_size)]
        for _ in range(self.n_img_layers - 1):
            image_layers += [
                nn.ReLU(),
                nn.Dropout(p=self.opt['dropout']),
                nn.Linear(self.img_dim, self.embedding_size),
            ]
        self.image_encoder = nn.Sequential(*image_layers)

    def encode_images(
        self, images: List[object]
    ) -> Tuple[Optional[List[int]], Optional[torch.Tensor]]:
        """
        Encode Images.

        Encodes images given in `images`, if the image can be encoded (i.e. it
        is a tensor).

        :param images:
            list of objects of length N, of which some maybe be None

        :return:
            a (image_encoded, image_mask) tuple, where:

            - image_enc is a torch.Tensor of dim N x self.img_dim,
              representing the encoded batch of images
            - image_mask is a torch.Tensor of dim N x 1
        """
        image_masks = image_encoded = None
        valid_inds = [
            i
            for i, img in enumerate(images)
            if img is not None and isinstance(img, torch.Tensor)
        ]

        if valid_inds:
            image_masks = []
            image_encoded = []

            valid_imgs = torch.stack([images[i] for i in valid_inds])
            valid_img_enc = self.image_encoder(valid_imgs)

            img_num = 0
            for i in range(len(images)):
                if i in valid_inds:
                    image_masks.append(self.ones_mask)
                    image_encoded.append(valid_img_enc[img_num, :])
                    img_num += 1
                else:
                    image_masks.append(~self.ones_mask)
                    image_encoded.append(self.dummy_image_enc)

            image_masks = torch.stack(image_masks)
            image_encoded = torch.stack(image_encoded).unsqueeze(1)

        return image_encoded, image_masks

    def forward(self, src_tokens, image_features):
        """
        Encode images with context.

        Encodes tokens (if given) and images (if given) separately.
        Combines via either addition, prepending, or postpending the image embedding to
        the context embedding.

        :param src_tokens:
            A bsz x seq_len tensor of src_tokens; possibly None
        :param image_features:
            A list of (torch.tensor)

        :return:
            A (full_enc, full_mask) tuple, which represents the encoded context
            and the mask
        """
        context_encoded = context_mask = None
        image_encoded = extra_masks = None
        if src_tokens is not None:
            context_encoded, context_mask = super().forward(src_tokens)
        if image_features is not None:
            image_encoded, extra_masks = self.encode_images(image_features)

        if all(enc is None for enc in [context_encoded, image_encoded]):
            raise RuntimeError(
                'You are providing Image+Seq2Seq with no input.\n'
                'If you are using a text-based task, make sure the first turn '
                'has text (e.g. a __SILENCE__ token if the model starts the convo).\n'
                'If you are using an image-based task, make sure --image-mode is '
                'set correctly.'
            )

        if self.image_combination_mode == 'add':
            full_enc = self.add([context_encoded, image_encoded])
            # image_encoded broadcasted along dim=1
            full_mask = context_mask
            import pdb

            pdb.set_trace()
            # TODO: remove
        elif self.image_combination_mode == 'postpend':
            full_enc = self.cat([context_encoded, image_encoded])
            full_mask = self.cat([context_mask, extra_masks])
        elif self.image_combination_mode == 'prepend':
            full_enc = self.cat([image_encoded, context_encoded])
            full_mask = self.cat([extra_masks, context_mask])
            import pdb

            pdb.set_trace()
            # TODO: remove
        else:
            raise ValueError('Image combination mode not recognized!')

        return full_enc, full_mask

    def add(self, tensors: List[torch.Tensor]) -> torch.Tensor:
        """
        Handle addition of None tensors.

        Smart addition. Adds tensors if they are not None.

        :param tensors:
            A list of torch.Tensor, with at least one non-null object

        :return:
            The result of adding all non-null objects in tensors
        """
        tensors = [t for t in tensors if t is not None]
        return reduce(lambda a, b: a + b, tensors)

    def cat(self, tensors: List[torch.Tensor]) -> torch.Tensor:
        """
        Handle concatenation of None tensors.

        Smart concatenation. Concatenates tensors if they are not None.

        :param tensors:
            A list of torch.Tensor, with at least one non-null object

        :return:
            The result of concatenating all non-null objects in tensors
        """
        tensors = [t for t in tensors if t is not None]
        return torch.cat([t for t in tensors], dim=1)


class PolyBasicAttention(BasicAttention):
    """
    Override basic attention to account for edge case for polyencoder.
    """

    def __init__(self, poly_type, n_codes, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.poly_type = poly_type
        self.n_codes = n_codes

    def forward(self, *args, **kwargs):
        """
        Forward pass.

        Account for accidental dimensionality reduction when num_codes is 1 and the
        polyencoder type is 'codes'
        """
        lhs_emb = super().forward(*args, **kwargs)
        if self.poly_type == 'codes' and self.n_codes == 1 and len(lhs_emb.shape) == 2:
            lhs_emb = lhs_emb.unsqueeze(self.dim - 1)
        return lhs_emb


class IRFriendlyPolyencoderAgent(AddLabelFixedCandsTRA, PolyencoderAgent):
    """
    Poly-encoder agent that allows for adding label to fixed cands.
    """

    @classmethod
    def add_cmdline_args(cls, argparser):
        """
        Add cmd line args.
        """
        super(AddLabelFixedCandsTRA, cls).add_cmdline_args(argparser)
        super(PolyencoderAgent, cls).add_cmdline_args(argparser)
