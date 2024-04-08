import numpy as np
import torch
from torch import nn

from nanofold.training.frame import Frame
from nanofold.training.loss import DistogramLoss
from nanofold.training.model.evoformer import Evoformer
from nanofold.training.model.input import InputEmbedding
from nanofold.training.model.recycle import RecyclingEmbedder
from nanofold.training.model.structure import StructureModule


class Nanofold(nn.Module):
    def __init__(
        self,
        num_recycle,
        num_structure_layers,
        single_embedding_size,
        pair_embedding_size,
        msa_embedding_size,
        num_triangular_update_channels,
        num_triangular_attention_channels,
        product_embedding_size,
        position_bins,
        num_evoformer_blocks,
        num_evoformer_msa_heads,
        num_evoformer_pair_heads,
        num_evoformer_channels,
        evoformer_transition_multiplier,
        dropout,
        ipa_embedding_size,
        num_query_points,
        num_value_points,
        num_heads,
        num_distogram_bins,
        num_distogram_channels,
        num_lddt_bins,
        num_lddt_channels,
        use_checkpoint,
        device,
    ):
        super().__init__()
        self.device = device
        self.num_recycle = num_recycle
        self.msa_embedding_size = msa_embedding_size
        self.pair_embedding_size = pair_embedding_size
        self.input_embedder = InputEmbedding(pair_embedding_size, msa_embedding_size, position_bins)
        self.recycling_embedder = RecyclingEmbedder(pair_embedding_size, msa_embedding_size, device)
        self.evoformer = Evoformer(
            single_embedding_size,
            pair_embedding_size,
            msa_embedding_size,
            num_triangular_update_channels,
            num_triangular_attention_channels,
            product_embedding_size,
            num_evoformer_blocks,
            num_evoformer_msa_heads,
            num_evoformer_pair_heads,
            num_evoformer_channels,
            evoformer_transition_multiplier,
            device,
        )
        self.structure_module = StructureModule(
            num_structure_layers,
            single_embedding_size,
            pair_embedding_size,
            dropout,
            ipa_embedding_size,
            num_query_points,
            num_value_points,
            num_heads,
            num_lddt_bins,
            num_lddt_channels,
            device,
        )
        self.distogram_loss = DistogramLoss(
            pair_embedding_size, num_distogram_bins, num_distogram_channels, device
        )
        self.use_checkpoint = use_checkpoint

    @staticmethod
    def get_args(config):
        return {
            "num_recycle": config.getint("Nanofold", "num_recycle"),
            "num_structure_layers": config.getint("StructureModule", "num_layers"),
            "single_embedding_size": config.getint("Nanofold", "single_embedding_size"),
            "pair_embedding_size": config.getint("Nanofold", "pair_embedding_size"),
            "msa_embedding_size": config.getint("Nanofold", "msa_embedding_size"),
            "position_bins": config.getint("Nanofold", "position_bins"),
            "num_triangular_update_channels": config.getint(
                "Evoformer", "num_triangular_update_channels"
            ),
            "num_triangular_attention_channels": config.getint(
                "Evoformer", "num_triangular_attention_channels"
            ),
            "product_embedding_size": config.getint("Evoformer", "product_embedding_size"),
            "num_evoformer_blocks": config.getint("Evoformer", "num_blocks"),
            "num_evoformer_msa_heads": config.getint("Evoformer", "num_msa_heads"),
            "num_evoformer_pair_heads": config.getint("Evoformer", "num_pair_heads"),
            "num_evoformer_channels": config.getint("Evoformer", "num_channels"),
            "evoformer_transition_multiplier": config.getint("Evoformer", "transition_multiplier"),
            "dropout": config.getfloat("StructureModule", "dropout"),
            "ipa_embedding_size": config.getint("InvariantPointAttention", "embedding_size"),
            "num_query_points": config.getint("InvariantPointAttention", "num_query_points"),
            "num_value_points": config.getint("InvariantPointAttention", "num_value_points"),
            "num_heads": config.getint("InvariantPointAttention", "num_heads"),
            "num_distogram_bins": config.getint("Loss", "num_distogram_bins"),
            "num_distogram_channels": config.getint("Loss", "num_distogram_channels"),
            "num_lddt_bins": config.getint("Loss", "num_lddt_bins"),
            "num_lddt_channels": config.getint("Loss", "num_lddt_channels"),
            "use_checkpoint": config.get("General", "use_checkpoint"),
            "device": config.get("General", "device"),
        }

    @classmethod
    def from_config(cls, config):
        return cls(**cls.get_args(config))

    def run_evoformer(self, *args):
        if self.use_checkpoint or not self.training:
            return torch.utils.checkpoint.checkpoint(
                lambda *inputs: self.evoformer(*inputs), *args, use_reentrant=False
            )
        return self.evoformer(*args)

    def forward(self, batch):
        num_recycle = (
            torch.randint(self.num_recycle, (1,)) + 1 if self.training else self.num_recycle
        )
        fape_clamp = 10.0 if torch.rand(1) < 0.9 and self.training else None

        s = batch["positions"].shape
        prev_msa_row = torch.zeros((*s, self.msa_embedding_size), device=self.device)
        prev_ca_coords = torch.zeros((*s, 3), device=self.device)
        prev_pair_rep = torch.zeros((*s, s[-1], self.pair_embedding_size), device=self.device)

        for i in range(num_recycle):
            prev_msa_row = prev_msa_row.detach()
            prev_ca_coords = prev_ca_coords.detach()
            prev_pair_rep = prev_pair_rep.detach()

            msa_rep, pair_rep = self.input_embedder(
                batch["target_feat"], batch["positions"], batch["msa_feat"]
            )
            msa_row_update, pair_rep_update = self.recycling_embedder(
                prev_msa_row, prev_pair_rep, prev_ca_coords
            )
            msa_rep[..., 0, :, :] = msa_rep[..., 0, :, :] + msa_row_update
            pair_rep = pair_rep + pair_rep_update

            msa_rep, pair_rep, single_rep = self.run_evoformer(msa_rep, pair_rep)

            coords, chain_plddt, chain_lddt, fape_loss, conf_loss, aux_loss = self.structure_module(
                single_rep,
                pair_rep,
                batch["local_coords"],
                (
                    Frame(
                        rotations=batch["rotations"],
                        translations=batch["translations"],
                    )
                    if i == num_recycle - 1 and "translations" in batch
                    else None
                ),
                fape_clamp,
            )
            prev_msa_row = msa_rep[..., 0, :, :]
            prev_pair_rep = pair_rep
            prev_ca_coords = coords[..., 1, :]

        dist_loss = (
            self.distogram_loss(pair_rep, batch["translations"])
            if "translations" in batch
            else None
        )

        return coords, chain_plddt, chain_lddt, fape_loss, conf_loss, aux_loss, dist_loss
