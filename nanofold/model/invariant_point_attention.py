import math
import torch
from torch import nn
from nanofold.frame import Frame
from nanofold.util import LinearWithView


class InvariantPointAttention(nn.Module):
    def __init__(
        self,
        single_embedding_size,
        pair_embedding_size,
        ipa_embedding_size,
        num_query_points,
        num_value_points,
        num_heads,
    ):
        super().__init__()
        self.num_heads = num_heads
        self.num_query_points = num_query_points
        self.num_value_points = num_value_points
        self.query = LinearWithView(
            single_embedding_size, (num_heads, ipa_embedding_size), bias=False
        )
        self.key = LinearWithView(
            single_embedding_size, (num_heads, ipa_embedding_size), bias=False
        )
        self.value = LinearWithView(
            single_embedding_size, (num_heads, ipa_embedding_size), bias=False
        )
        self.query_points = LinearWithView(
            single_embedding_size, (num_heads, num_query_points, 3), bias=False
        )
        self.key_points = LinearWithView(
            single_embedding_size, (num_heads, num_query_points, 3), bias=False
        )
        self.value_points = LinearWithView(
            single_embedding_size, (num_heads, num_value_points, 3), bias=False
        )
        self.bias = nn.Linear(pair_embedding_size, num_heads, bias=False)
        self.out = nn.Linear(
            self.num_heads
            * (pair_embedding_size + ipa_embedding_size + self.num_value_points * (3 + 1)),
            single_embedding_size,
        )
        self.softplus = nn.Softplus()
        self.scale_head = nn.Parameter(torch.ones(self.num_heads))
        self.scale_single_rep = 1 / math.sqrt(ipa_embedding_size)
        self.scale_frame = -1 / math.sqrt(18 * self.num_query_points)

    @staticmethod
    def get_args(config):
        return {
            "single_embedding_size": config.getint("Other", "single_embedding_size"),
            "pair_embedding_size": config.getint("InputEmbedding", "pair_embedding_size"),
            "ipa_embedding_size": config.getint("InvariantPointAttention", "embedding_size"),
            "num_query_points": config.getint("InvariantPointAttention", "num_query_points"),
            "num_value_points": config.getint("InvariantPointAttention", "num_value_points"),
            "num_heads": config.getint("InvariantPointAttention", "num_heads"),
        }

    @classmethod
    def from_config(cls, config):
        return cls(**cls.get_args(config))

    def single_rep_weight(self, single_rep):
        q = self.query(single_rep)
        k = self.key(single_rep)
        weight = self.scale_single_rep * q.unsqueeze(-2) @ k.permute(1, 2, 0)
        return weight.squeeze(-2)

    def pair_rep_weight(self, pair_rep):
        weight = self.bias(pair_rep).transpose(-2, -1)
        return weight

    def frame_weight(self, frames, single_rep):
        qp = self.query_points(single_rep)
        kp = self.key_points(single_rep)
        local_qp = Frame.apply(frames, qp.transpose(0, -2))
        local_kp = Frame.apply(frames, kp.transpose(0, -2))
        difference = local_qp.unsqueeze(-2) - local_kp.unsqueeze(-3)
        squared_distance = difference.unsqueeze(-2) @ difference.unsqueeze(-1)
        squared_distance = squared_distance.squeeze()
        weight = torch.sum(squared_distance, dim=0).transpose(0, 1)
        weight = self.scale_frame * self.softplus(self.scale_head).unsqueeze(-1) * weight
        return weight

    def single_rep_attention(self, weight, single_rep):
        v = self.value(single_rep)
        attention = weight.unsqueeze(-2) @ v.transpose(0, 1)
        return attention.squeeze(-2)

    def pair_rep_attention(self, weight, pair_rep):
        attention = weight.unsqueeze(-2) @ pair_rep.unsqueeze(1)
        return attention.squeeze(-2)

    def frame_attention(self, weight, frames, single_rep):
        vp = self.value_points(single_rep)
        local_vp = Frame.apply(frames, vp.permute(1, 2, 0, 3))
        local_attention = weight.unsqueeze(-2).unsqueeze(-2) @ local_vp
        frames_inverse = Frame.inverse(frames)[:, None, None, ...]
        return Frame.apply(frames_inverse, local_attention.squeeze(-2))

    def forward(self, single_rep, pair_rep, frames):
        weight = (
            self.single_rep_weight(single_rep)
            + self.pair_rep_weight(pair_rep)
            + self.frame_weight(frames, single_rep)
        )
        weight = nn.functional.softmax(weight, dim=-1)

        len_seq = single_rep.shape[0]
        single_rep_attention = self.single_rep_attention(weight, single_rep)
        pair_rep_attention = self.pair_rep_attention(weight, pair_rep)
        frame_attention = self.frame_attention(weight, frames, single_rep)
        frame_norm_attention = torch.linalg.vector_norm(frame_attention, dim=-1)
        attention = torch.cat(
            [
                single_rep_attention,
                pair_rep_attention,
                frame_attention.view(len_seq, self.num_heads, -1),
                frame_norm_attention,
            ],
            dim=-1,
        )
        attention = self.out(attention.reshape(len_seq, -1))
        return attention