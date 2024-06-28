# ---------------------------------------------
# Copyright (c) OpenMMLab. All rights reserved.
# ---------------------------------------------
#  Modified by Zhiqi Li
# ---------------------------------------------


import warnings
import torch
import torch.nn as nn
from mmcv.cnn import xavier_init, constant_init
from mmcv.cnn.bricks.registry import ATTENTION
import math
from mmcv.runner.base_module import BaseModule 


from mmcv.utils import ext_loader
ext_module = ext_loader.load_ext(
    '_ext', ['ms_deform_attn_backward', 'ms_deform_attn_forward'])

from msda import multi_scale_deformable_attn, _identity_
from tensor_warper import pt
from loguru import logger 

@ATTENTION.register_module()
class TemporalSelfAttention(BaseModule):
    """An attention module used in BEVFormer based on Deformable-Detr.

    `Deformable DETR: Deformable Transformers for End-to-End Object Detection.
    <https://arxiv.org/pdf/2010.04159.pdf>`_.

    Args:
        embed_dims (int): The embedding dimension of Attention.
            Default: 256.
        num_heads (int): Parallel attention heads. Default: 64.
        num_levels (int): The number of feature map used in
            Attention. Default: 4.
        num_points (int): The number of sampling points for
            each query in each head. Default: 4.
        im2col_step (int): The step used in image_to_column.
            Default: 64.
        dropout (float): A Dropout layer on `inp_identity`.
            Default: 0.1.
        batch_first (bool): Key, Query and Value are shape of
            (batch, n, embed_dim)
            or (n, batch, embed_dim). Default to True.
        norm_cfg (dict): Config dict for normalization layer.
            Default: None.
        init_cfg (obj:`mmcv.ConfigDict`): The Config for initialization.
            Default: None.
        num_bev_queue (int): In this version, we only use one history BEV and one currenct BEV.
         the length of BEV queue is 2.
    """

    def __init__(self,
                 embed_dims=256,
                 num_heads=8,
                 num_levels=4,
                 num_points=4,
                 num_bev_queue=2,
                 im2col_step=64,
                 dropout=0.1,
                 batch_first=True,
                 norm_cfg=None,
                 init_cfg=None):

        super().__init__(init_cfg)
        if embed_dims % num_heads != 0:
            raise ValueError(f'embed_dims must be divisible by num_heads, '
                             f'but got {embed_dims} and {num_heads}')
        dim_per_head = embed_dims // num_heads
        self.norm_cfg = norm_cfg
        self.dropout = nn.Dropout(dropout)
        self.batch_first = batch_first
        self.fp16_enabled = False

        # you'd better set dim_per_head to a power of 2
        # which is more efficient in the CUDA implementation
        def _is_power_of_2(n):
            if (not isinstance(n, int)) or (n < 0):
                raise ValueError(
                    'invalid input for _is_power_of_2: {} (type: {})'.format(
                        n, type(n)))
            return (n & (n - 1) == 0) and n != 0

        if not _is_power_of_2(dim_per_head):
            warnings.warn(
                "You'd better set embed_dims in "
                'MultiScaleDeformAttention to make '
                'the dimension of each attention head a power of 2 '
                'which is more efficient in our CUDA implementation.')

        self.im2col_step = im2col_step
        self.embed_dims = embed_dims
        self.num_levels = num_levels
        self.num_heads = num_heads
        self.num_points = num_points
        self.num_bev_queue = num_bev_queue
        self.sampling_offsets = nn.Linear(
            embed_dims*self.num_bev_queue, num_bev_queue*num_heads * num_levels * num_points * 2)
        self.attention_weights = nn.Linear(embed_dims*self.num_bev_queue,
                                           num_bev_queue*num_heads * num_levels * num_points)
        self.value_proj = nn.Linear(embed_dims, embed_dims)
        self.output_proj = nn.Linear(embed_dims, embed_dims)
        self.init_weights()
        self.spatial_shapes =torch.tensor([[20, 20]], dtype=torch.int64)     #  torch.Size([1, 2])  torch.int64  


    def init_weights(self):
        """Default initialization for Parameters of Module."""
        constant_init(self.sampling_offsets, 0.)
        thetas = torch.arange(
            self.num_heads,
            dtype=torch.float32) * (2.0 * math.pi / self.num_heads)
        grid_init = torch.stack([thetas.cos(), thetas.sin()], -1)
        grid_init = (grid_init /
                     grid_init.abs().max(-1, keepdim=True)[0]).view(
            self.num_heads, 1, 1,
            2).repeat(1, self.num_levels*self.num_bev_queue, self.num_points, 1)

        for i in range(self.num_points):
            grid_init[:, :, i, :] *= i + 1

        self.sampling_offsets.bias.data = grid_init.view(-1)
        constant_init(self.attention_weights, val=0., bias=0.)
        xavier_init(self.value_proj, distribution='uniform', bias=0.)
        xavier_init(self.output_proj, distribution='uniform', bias=0.)
        self._is_init = True

    def forward_check(self,
                query,
                # key=None,
                # value=None,
                # identity=None,
                query_pos=None,
                # key_padding_mask=None,
                reference_points=None,
                spatial_shapes=None,
                level_start_index=None
                ):
        """Forward Function of MultiScaleDeformAttention.

        Args:
            query (Tensor): Query of Transformer with shape
                (num_query, bs, embed_dims).
            key (Tensor): The key tensor with shape
                `(num_key, bs, embed_dims)`.
            value (Tensor): The value tensor with shape
                `(num_key, bs, embed_dims)`.
            identity (Tensor): The tensor used for addition, with the
                same shape as `query`. Default None. If None,
                `query` will be used.
            query_pos (Tensor): The positional encoding for `query`.
                Default: None.
            key_pos (Tensor): The positional encoding for `key`. Default
                None.
            reference_points (Tensor):  The normalized reference
                points with shape (bs, num_query, num_levels, 2),
                all elements is range in [0, 1], top-left (0,0),
                bottom-right (1, 1), including padding area.
                or (N, Length_{query}, num_levels, 4), add
                additional two dimensions is (w, h) to
                form reference boxes.
            key_padding_mask (Tensor): ByteTensor for `query`, with
                shape [bs, num_key].
            spatial_shapes (Tensor): Spatial shape of features in
                different levels. With shape (num_levels, 2),
                last dimension represents (h, w).
            level_start_index (Tensor): The start index of each level.
                A tensor has shape ``(num_levels, )`` and can be represented
                as [0, h_0*w_0, h_0*w_0+h_1*w_1, ...].

        Returns:
             Tensor: forwarded results with shape [num_query, bs, embed_dims].
        """
        spatial_shapes = self.spatial_shapes    

        # if value is None:
        assert self.batch_first
        bs, len_bev, c = query.shape
        value = torch.stack([query, query], 1).reshape(bs*2, len_bev, c)

        # if identity is None:
        identity = query

        # if query_pos is not None:
        #     query = query + query_pos

        bs,  num_query, embed_dims = query.shape
        _, num_value, _ = value.shape
        assert (spatial_shapes[:, 0] * spatial_shapes[:, 1]).sum() == num_value
        assert self.num_bev_queue == 2

        query = torch.cat([value[:bs], query], -1) 
        value = self.value_proj(value)
        logger.info(pt("query", query))    # torch.Size([1, 400, 512])  torch.float32  

        logger.info(pt("value", value))    # torch.Size([2, 400, 256])  torch.float32  
        value = value.reshape(bs*self.num_bev_queue, num_value, self.num_heads, -1)
        logger.info(pt("value", value))    # torch.Size([2, 400, 8, 32])  torch.float32  

        sampling_offsets = self.sampling_offsets(query)
        logger.info(pt("sampling_offsets", sampling_offsets))    # torch.Size([1, 400, 128])  torch.float32  

        sampling_offsets = sampling_offsets.view(
            bs, num_query, self.num_heads,  self.num_bev_queue, self.num_levels, self.num_points, 2)
        logger.info(pt("sampling_offsets", sampling_offsets))    # torch.Size([1, 400, 8, 2, 1, 4, 2])  torch.float32  
    
        sampling_offsets = sampling_offsets.permute(0, 3, 1, 2, 4, 5, 6)
        logger.info(pt("sampling_offsets", sampling_offsets))    # torch.Size([1, 2, 400, 8, 1, 4, 2])  torch.float32  

        sampling_offsets = sampling_offsets.reshape(bs*self.num_bev_queue, num_query, self.num_heads, self.num_levels, self.num_points, 2)
        logger.info(pt("sampling_offsets", sampling_offsets))    # torch.Size([2, 400, 8, 1, 4, 2])  torch.float32  

        attention_weights = self.attention_weights(query)
        logger.info(pt("attention_weights", attention_weights))    # torch.Size([1, 400, 64])  torch.float32  

        attention_weights = attention_weights.view(
            bs, num_query,  self.num_heads, self.num_bev_queue, self.num_levels * self.num_points)
        logger.info(pt("attention_weights", attention_weights))    # torch.Size([1, 400, 8, 2, 4])  torch.float32  

        attention_weights = attention_weights.softmax(-1)
        logger.info(pt("attention_weights", attention_weights))    # torch.Size([1, 400, 8, 2, 4])  torch.float32  

        attention_weights = attention_weights.view(bs, 
                                                   num_query,
                                                   self.num_heads,
                                                   self.num_bev_queue,
                                                   self.num_levels,
                                                   self.num_points)
        logger.info(pt("attention_weights", attention_weights))    # torch.Size([1, 400, 8, 2, 1, 4])  torch.float32  

        attention_weights = attention_weights.permute(0, 3, 1, 2, 4, 5) 
        logger.info(pt("attention_weights", attention_weights))    # torch.Size([1, 2, 400, 8, 1, 4])  torch.float32   false

        attention_weights = attention_weights.reshape(bs*self.num_bev_queue, num_query, self.num_heads, self.num_levels, self.num_points)#.contiguous()
        logger.info(pt("attention_weights", attention_weights))    # torch.Size([2, 400, 8, 1, 4])  torch.float32  
        return value, sampling_offsets, attention_weights
    

    def forward(self,
                query,
                # key=None,
                # value=None,
                # identity=None,
                query_pos=None,
                # key_padding_mask=None,
                reference_points=None,
                spatial_shapes=None,
                level_start_index=None
                ):
        """Forward Function of MultiScaleDeformAttention.

        Args:
            query (Tensor): Query of Transformer with shape
                (num_query, bs, embed_dims).
            key (Tensor): The key tensor with shape
                `(num_key, bs, embed_dims)`.
            value (Tensor): The value tensor with shape
                `(num_key, bs, embed_dims)`.
            identity (Tensor): The tensor used for addition, with the
                same shape as `query`. Default None. If None,
                `query` will be used.
            query_pos (Tensor): The positional encoding for `query`.
                Default: None.
            key_pos (Tensor): The positional encoding for `key`. Default
                None.
            reference_points (Tensor):  The normalized reference
                points with shape (bs, num_query, num_levels, 2),
                all elements is range in [0, 1], top-left (0,0),
                bottom-right (1, 1), including padding area.
                or (N, Length_{query}, num_levels, 4), add
                additional two dimensions is (w, h) to
                form reference boxes.
            key_padding_mask (Tensor): ByteTensor for `query`, with
                shape [bs, num_key].
            spatial_shapes (Tensor): Spatial shape of features in
                different levels. With shape (num_levels, 2),
                last dimension represents (h, w).
            level_start_index (Tensor): The start index of each level.
                A tensor has shape ``(num_levels, )`` and can be represented
                as [0, h_0*w_0, h_0*w_0+h_1*w_1, ...].

        Returns:
             Tensor: forwarded results with shape [num_query, bs, embed_dims].
        """
        # value_t, sampling_offsets_t, attention_weights_t = self.forward_check(
        #         query,
        #         # key=None,
        #         # value=None,
        #         # identity=None,
        #         query_pos=query_pos,
        #         # key_padding_mask=None,
        #         reference_points=reference_points,
        #         spatial_shapes=spatial_shapes,
        #         level_start_index=level_start_index
        #         )
        
        spatial_shapes = self.spatial_shapes    

        # if value is None:
        assert self.batch_first
        bs, len_bev, c = query.shape
        value = torch.stack([query, query], 1).reshape(bs*2, len_bev, c)

        # if identity is None:
        identity = query

        bs,  num_query, embed_dims = query.shape
        _, num_value, _ = value.shape
        assert (spatial_shapes[:, 0] * spatial_shapes[:, 1]).sum() == num_value
        assert self.num_bev_queue == 2
        logger.info(pt("value", value))    # torch.Size([2, 400, 256])  torch.float32  

        query = torch.cat([value[:bs], query], -1)[0] 
        value = self.value_proj(value)
        logger.info(pt("query", query))    # torch.Size([400, 512])  torch.float32  

        logger.info(pt("value", value))    # torch.Size([2, 400, 256])  torch.float32  
        value = value.reshape(bs*self.num_bev_queue, num_value, self.num_heads, -1)
        logger.info(pt("value", value))    # torch.Size([2, 400, 8, 32])  torch.float32  

        sampling_offsets = self.sampling_offsets(query)
        logger.info(pt("sampling_offsets", sampling_offsets))    # torch.Size([400, 128])  torch.float32  

        sampling_offsets = sampling_offsets.view(
            num_query*self.num_heads, self.num_bev_queue, self.num_levels*self.num_points*2)
        logger.info(pt("sampling_offsets", sampling_offsets))    # torch.Size([400*8, 2, 1*4*2])  torch.float32  
    
        sampling_offsets = sampling_offsets.permute(1, 0, 2)
        logger.info(pt("sampling_offsets", sampling_offsets))    # torch.Size([2, 400*8, 1*4*2])  torch.float32  

        sampling_offsets = sampling_offsets.reshape(self.num_bev_queue, num_query, self.num_heads, self.num_levels, self.num_points, 2)
        logger.info(pt("sampling_offsets", sampling_offsets))    # torch.Size([2, 400, 8, 1, 4, 2])  torch.float32  

        #####################
        attention_weights = self.attention_weights(query)
        logger.info(pt("attention_weights", attention_weights))    # torch.Size([400, 64])  torch.float32  

        attention_weights = attention_weights.view(
            num_query* self.num_heads*self.num_bev_queue, self.num_levels * self.num_points)
        logger.info(pt("attention_weights", attention_weights))    # torch.Size([400*8*2, 4])  torch.float32  

        attention_weights = attention_weights.softmax(-1)
        logger.info(pt("attention_weights", attention_weights))    # torch.Size([400*8*2, 4])  torch.float32  

        attention_weights = attention_weights.view(num_query*self.num_heads, self.num_bev_queue, self.num_levels*self.num_points)
        logger.info(pt("attention_weights", attention_weights))    # torch.Size([400*8, 2, 1*4])  torch.float32  

        attention_weights = attention_weights.permute(1, 0, 2) 
        logger.info(pt("attention_weights", attention_weights))    # torch.Size([2, 400*8, 1*4])  torch.float32   false

        attention_weights = attention_weights.reshape(self.num_bev_queue, num_query, self.num_heads, self.num_levels, self.num_points)#.contiguous()
        logger.info(pt("attention_weights", attention_weights))    # torch.Size([2, 400, 8, 1, 4])  torch.float32  

        # logger.info(torch.equal(value_t, value))
        # logger.info(torch.equal(sampling_offsets_t, sampling_offsets))
        # logger.info(torch.equal(attention_weights_t, attention_weights))
        return value, sampling_offsets, attention_weights
             
        if reference_points.shape[-1] == 2:
            offset_normalizer = torch.stack([spatial_shapes[..., 1], spatial_shapes[..., 0]], -1)
            sampling_locations = reference_points[:, :, None, :, None, :] + sampling_offsets / offset_normalizer[None, None, None, :, None, :]

        elif reference_points.shape[-1] == 4:
            sampling_locations = reference_points[:, :, None, :, None, :2] \
                + sampling_offsets / self.num_points \
                * reference_points[:, :, None, :, None, 2:] \
                * 0.5
        else:
            raise ValueError(
                f'Last dim of reference_points must be'
                f' 2 or 4, but get {reference_points.shape[-1]} instead.')
        # if torch.cuda.is_available() and value.is_cuda and not torch.onnx.is_in_onnx_export():

        #     # using fp16 deformable attention is unstable because it performs many sum operations
        #     if value.dtype == torch.float16:
        #         MultiScaleDeformableAttnFunction = MultiScaleDeformableAttnFunction_fp32
        #     else:
        #         MultiScaleDeformableAttnFunction = MultiScaleDeformableAttnFunction_fp32
        #     output = MultiScaleDeformableAttnFunction.apply(
        #         value, spatial_shapes, level_start_index, sampling_locations,
        #         attention_weights, self.im2col_step)
        # else:
        #     output = multi_scale_deformable_attn_pytorch(
        #         value, spatial_shapes, sampling_locations, attention_weights)

        # output = multi_scale_deformable_attn_pytorch(
        #         value, spatial_shapes, sampling_locations, attention_weights)
        ##########################################################################################
        output = multi_scale_deformable_attn(
            value, 
            spatial_shapes, 
            level_start_index, 
            sampling_locations,
            attention_weights, ############ !!!
            torch.tensor(self.im2col_step, dtype=torch.int32)
            )
        ##########################################################################################

        # output shape (bs*num_bev_queue, num_query, embed_dims)
        # (bs*num_bev_queue, num_query, embed_dims)-> (num_query, embed_dims, bs*num_bev_queue)
        output = output.permute(1, 2, 0)

        # fuse history value and current value
        # (num_query, embed_dims, bs*num_bev_queue)-> (num_query, embed_dims, bs, num_bev_queue)
        output = output.view(num_query, embed_dims, bs, self.num_bev_queue)
        output = output.mean(-1)

        # (num_query, embed_dims, bs)-> (bs, num_query, embed_dims)
        output = output.permute(2, 0, 1)

        output = self.output_proj(output)

        if not self.batch_first:
            output = output.permute(1, 0, 2)

        return self.dropout(output) + identity
