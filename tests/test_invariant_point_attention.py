import math
import torch
from nanofold.invariant_point_attention import InvariantPointAttention
from nanofold.frame import Frame


class TestInvariantPointAttention:
    def setup_method(self):
        self.len_seq = 10
        self.pair_embedding_size = 7
        self.single_embedding_size = 3
        self.embedding_size = 5
        self.num_query_points = 4
        self.num_value_points = 8
        self.num_heads = 2
        self.model = InvariantPointAttention(
            single_embedding_size=self.single_embedding_size,
            pair_embedding_size=self.pair_embedding_size,
            embedding_size=self.embedding_size,
            num_query_points=self.num_query_points,
            num_value_points=self.num_value_points,
            num_heads=self.num_heads,
        )
        self.single_representation = (
            torch.arange(self.single_embedding_size * self.len_seq)
            .float()
            .reshape(self.len_seq, self.single_embedding_size)
        )
        self.pair_representation = (
            torch.arange(self.len_seq**2 * self.pair_embedding_size)
            .float()
            .reshape(self.len_seq, self.len_seq, self.pair_embedding_size)
        )
        rotations = torch.tensor(
            [
                [
                    [math.cos(i), -math.sin(i), 0],
                    [math.sin(i), math.cos(i), 0],
                    [0, 0, 1],
                ]
                for i in range(self.len_seq)
            ]
        )
        translations = torch.arange(self.len_seq * 3).float().reshape(self.len_seq, 3)
        self.frames = Frame(
            rotations=rotations,
            translations=translations,
        )

    @torch.no_grad
    def test_shape(self):
        result = self.model(
            self.single_representation, self.pair_representation, self.frames
        )
        assert result.shape == (self.len_seq, self.single_embedding_size)

    @torch.no_grad
    def test_invariant_to_transformations(self):
        rotation = torch.tensor(
            [[math.cos(1), -math.sin(1), 0], [math.sin(1), math.cos(1), 0], [0, 0, 1]]
        )
        transform = Frame(rotations=rotation, translations=torch.ones(3))
        attention = self.model(
            self.single_representation, self.pair_representation, self.frames
        )
        transformed_attention = self.model(
            self.single_representation,
            self.pair_representation,
            Frame.compose(transform, self.frames),
        )
        assert torch.allclose(attention, transformed_attention, atol=1e-5)

    @torch.no_grad
    def test_single_rep_weight(self):
        weight = self.model.single_rep_weight(self.single_representation)
        assert weight.shape == (self.num_heads, self.len_seq, self.len_seq)

        q = self.model.query(self.single_representation).view(
            self.len_seq, self.num_heads, -1
        )
        k = self.model.key(self.single_representation).view(
            self.len_seq, self.num_heads, -1
        )
        for h in range(weight.shape[0]):
            for i in range(weight.shape[1]):
                for j in range(weight.shape[2]):
                    assert torch.allclose(
                        weight[h, i, j],
                        self.model.scale_single_rep * torch.dot(q[i, h], k[j, h]),
                        atol=1e-5,
                    )

    @torch.no_grad
    def test_pair_rep_weight(self):
        weight = self.model.pair_rep_weight(self.pair_representation)
        assert weight.shape == (self.num_heads, self.len_seq, self.len_seq)

        for i in range(self.len_seq):
            for j in range(self.len_seq):
                for h in range(self.num_heads):
                    assert torch.allclose(
                        weight[h, i, j],
                        self.model.bias(self.pair_representation[i, j])[h],
                        atol=1e-5,
                    )

    @torch.no_grad
    def test_frame_weight(self):
        for i in range(self.num_heads):
            self.model.scale_head[i] = i + 1
        weight = self.model.frame_weight(self.frames, self.single_representation)
        assert weight.shape == (self.num_heads, self.len_seq, self.len_seq)

        qp = self.model.query_points(self.single_representation).view(
            self.len_seq, self.num_heads, -1, 3
        )
        kp = self.model.key_points(self.single_representation).view(
            self.len_seq, self.num_heads, -1, 3
        )
        scale_factor = (
            self.model.softplus(self.model.scale_head) * self.model.scale_frame
        )

        for h in range(weight.shape[0]):
            for i in range(weight.shape[1]):
                for j in range(weight.shape[2]):
                    sum_distance = 0
                    for p in range(self.num_query_points):
                        diff = Frame.apply(
                            self.frames[i], qp[i][h][p].unsqueeze(0)
                        ) - Frame.apply(self.frames[j], kp[j][h][p].unsqueeze(0))
                        sum_distance += torch.linalg.vector_norm(diff) ** 2
                    assert torch.allclose(
                        scale_factor[h] * sum_distance,
                        weight[h, i, j],
                        atol=1e-5,
                    )

    @torch.no_grad
    def test_single_rep_attention(self):
        weight = self.model.single_rep_weight(self.single_representation)
        attention = self.model.single_rep_attention(weight, self.single_representation)
        assert attention.shape == (self.num_heads, self.len_seq, self.embedding_size)

        v = self.model.value(self.single_representation).view(
            self.len_seq, self.num_heads, -1
        )
        for h in range(self.num_heads):
            for i in range(self.len_seq):
                for x in range(self.embedding_size):
                    assert torch.allclose(
                        attention[h, i, x],
                        weight[h][i] @ v[:, h, x],
                        atol=1e-5,
                    )

    @torch.no_grad
    def test_pair_rep_attention(self):
        weight = self.model.pair_rep_weight(self.pair_representation)
        attention = self.model.pair_rep_attention(weight, self.pair_representation)
        assert attention.shape == (self.num_heads, self.len_seq, self.pair_embedding_size)

        for h in range(self.num_heads):
            for i in range(self.len_seq):
                for x in range(self.pair_embedding_size):
                    assert torch.allclose(
                        attention[h, i, x],
                        weight[h][i] @ self.pair_representation[i, :, x],
                        atol=1e-5,
                    )

    @torch.no_grad
    def test_frame_attention(self):
        weight = self.model.frame_weight(self.frames, self.single_representation)
        attention = self.model.frame_attention(
            weight, self.frames, self.single_representation
        )
        assert attention.shape == (self.num_heads, self.num_value_points, self.len_seq, 3)

        vp = self.model.value_points(self.single_representation).view(
            self.len_seq, self.num_heads, -1, 3
        )
        for h in range(self.num_heads):
            for i in range(self.len_seq):
                for p in range(self.num_value_points):
                    sum = torch.zeros(3)
                    for j in range(self.len_seq):
                        sum += weight[h][i][j] * Frame.apply(self.frames[j], vp[j][h][p])
                    assert torch.allclose(
                        attention[h][p][i],
                        Frame.apply(Frame.inverse(self.frames[i]), sum),
                        atol=1e-5,
                    )