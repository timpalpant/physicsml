import torch
from e3nn import nn, o3
from e3nn.util.jit import compile_mode, simplify_if_compile
from torch_geometric.utils import scatter

from ._activation import Activation
from .irreps_tools import reshape_irreps, tp_out_irreps_with_instructions
from .radial import BesselBasis, PolynomialCutoff
from .wrapper_ops import Linear, TensorProduct, FullyConnectedTensorProduct, SymmetricContractionWrapper, default_cueq_config, default_oeq_config


@simplify_if_compile
@compile_mode("script")
class NonLinearReadoutBlock(torch.nn.Module):
    def __init__(
        self,
        irreps_in: o3.Irreps,
        MLP_irreps: o3.Irreps,
        irreps_out: o3.Irreps,
        use_cueq: bool,
    ) -> None:
        super().__init__()

        self.linear_1 = Linear(irreps_in=irreps_in, irreps_out=MLP_irreps, use_cueq=use_cueq)
        self.non_linearity = Activation(irreps_in=MLP_irreps, acts=[torch.nn.SiLU()])
        self.linear_2 = Linear(irreps_in=MLP_irreps, irreps_out=irreps_out, use_cueq=use_cueq)

    def forward(self, x: torch.Tensor) -> torch.Tensor:  # [n_nodes, irreps]  # [..., ]
        x = self.linear_1(x)
        x = self.non_linearity(x)
        x = self.linear_2(x)
        return x


@compile_mode("script")
class RadialEmbeddingBlock(torch.nn.Module):
    def __init__(
        self,
        r_max: float,
        num_bessel: int,
        num_polynomial_cutoff: int,
    ) -> None:
        super().__init__()
        self.bessel_fn = BesselBasis(r_max=r_max, num_basis=num_bessel)
        self.cutoff_fn = PolynomialCutoff(r_max=r_max, p=num_polynomial_cutoff)
        self.out_dim = num_bessel

    def forward(
        self,
        edge_lengths: torch.Tensor,  # [n_edges, 1]
    ) -> torch.Tensor:
        bessel = self.bessel_fn(edge_lengths)  # [n_edges, n_basis]
        cutoff = self.cutoff_fn(edge_lengths)  # [n_edges, 1]

        output: torch.Tensor = bessel * cutoff  # [n_edges, n_basis]
        return output


@compile_mode("script")
class NodeUpdateBlock(torch.nn.Module):
    def __init__(
        self,
        node_attrs_irreps: o3.Irreps,
        node_feats_irreps: o3.Irreps,
        hidden_irreps: o3.Irreps,
        residual_connection: bool,
        use_cueq: bool,
    ) -> None:
        super().__init__()

        # net to compute W m_i
        self.linear = Linear(
            hidden_irreps,
            hidden_irreps,
            internal_weights=True,
            shared_weights=True,
            use_cueq=use_cueq,
        )

        if residual_connection:
            # residual connection from original node attrs and node features
            self.residual_connection_layer = FullyConnectedTensorProduct(
                node_feats_irreps,
                node_attrs_irreps,
                hidden_irreps,
                use_cueq=use_cueq,
            )
        else:
            self.residual_connection_layer = None

    def forward(
        self,
        m_i: torch.Tensor,
        node_feats: torch.Tensor,
        node_attrs: torch.Tensor,
    ) -> torch.Tensor:
        if self.residual_connection_layer is not None:
            node_feats = self.linear(m_i) + self.residual_connection_layer(
                node_feats,
                node_attrs,
            )
        else:
            node_feats = self.linear(m_i)

        return node_feats


class MessageBlock(torch.nn.Module):
    def __init__(
        self,
        interaction_irreps: o3.Irreps,
        node_attrs_irreps: o3.Irreps,
        hidden_irreps: o3.Irreps,
        num_elements: int,
        correlation: int,
        use_cueq: bool,
    ) -> None:
        super().__init__()

        # symmetric contraction to make A_i into messages m_i = W B_i
        self.symmetric_contractions = SymmetricContractionWrapper(
            irreps_in=interaction_irreps,
            irreps_out=hidden_irreps,
            correlation=correlation,
            num_elements=num_elements,
            use_cueq=use_cueq,
        )

    def forward(self, a_i: torch.Tensor, node_attrs: torch.Tensor) -> torch.Tensor:
        # contract the A_i's with element dependent weights and generalised CG coefs to get m_i = W B_i
        m_i: torch.Tensor = self.symmetric_contractions(a_i, node_attrs)

        return m_i


@compile_mode("script")
class InteractionBlock(torch.nn.Module):
    def __init__(
        self,
        node_feats_irreps: o3.Irreps,
        node_attrs_irreps: o3.Irreps,
        edge_attrs_irreps: o3.Irreps,
        edge_feats_irreps: o3.Irreps,
        interaction_irreps: o3.Irreps,
        avg_num_neighbours: float,
        mix_with_node_attrs: bool = False,
        use_cueq: bool = False,
        use_oeq: bool = False,
    ) -> None:
        super().__init__()

        self.avg_num_neighbours = avg_num_neighbours

        self.linear_node_feats = Linear(
            node_feats_irreps,
            node_feats_irreps,
            internal_weights=True,
            shared_weights=True,
            use_cueq=use_cueq,
        )

        # TensorProduct
        # find the only possible results from the tensor prod of node feats with edge attrs into targets
        # only do the tensor prod for these possibilities

        tp_out_irreps, instructions = tp_out_irreps_with_instructions(
            node_feats_irreps,
            edge_attrs_irreps,
            interaction_irreps,
        )

        # net to compute R_n -> R_k_l1_l2_l3
        self.net_R_channels = nn.FullyConnectedNet(
            [edge_feats_irreps.dim, 64, 64, 64, tp_out_irreps.num_irreps],
            torch.nn.SiLU(),
        )

        # product to do
        # \sum_{l1_l2_m1_m2} (CGs) (R_ij_k_l1_l2_l3) (Y_ij_l1_m1) (W h)_j_k_l2_m2 -> A_ij_k_l3_m3
        self.conv_tp = TensorProduct(
            node_feats_irreps,
            edge_attrs_irreps,
            tp_out_irreps,
            instructions=instructions,
            shared_weights=False,
            internal_weights=False,
            use_cueq=use_cueq,
            use_oeq=use_oeq,
        )

        # linear to take the tp_out_irreps (which are a subset of interaction_irreps,
        # since prod node_feats_irreps and edge_attrs_irreps doesn't necessarily
        # give all interaction_irreps
        self.linear = Linear(
            tp_out_irreps.simplify(),
            interaction_irreps,
            internal_weights=True,
            shared_weights=True,
            use_cueq=use_cueq,
        )

        if mix_with_node_attrs:
            self.mix_layer = FullyConnectedTensorProduct(
                interaction_irreps,
                node_attrs_irreps,
                interaction_irreps,
                use_cueq=use_cueq,
            )
        else:
            self.mix_layer = None

        # reshape the [n_nodes, num_feats * (\sum_l 2l+1)] -> [n_nodes, num_feats, (\sum_l 2l+1)]
        cueq_config = default_cueq_config if use_cueq else None
        self.reshape = reshape_irreps(interaction_irreps, cueq_config=cueq_config)
        self.conv_fusion = default_oeq_config.conv_fusion if use_oeq else ""

    def forward(
        self,
        node_feats: torch.Tensor,
        node_attrs: torch.Tensor,
        edge_attrs: torch.Tensor,
        edge_feats: torch.Tensor,
        edge_index: torch.Tensor,
    ) -> torch.Tensor:
        sender = edge_index[0]
        receiver = edge_index[1]
        num_nodes = node_feats.shape[0]

        # compute (W h)_j_k_l2_m2
        w_h_j_k_l2_m2 = self.linear_node_feats(node_feats)

        # compute R_ij_k_l1_l2_l3
        r_ij_k_l1_l2_l3 = self.net_R_channels(edge_feats)

        if self.conv_fusion == "deterministic":
            tp_a_i_k_l3_m3 = self.conv_tp.forward(w_h_j_k_l2_m2, edge_attrs, r_ij_k_l1_l2_l3, receiver, sender, edge_index[2])
        elif self.conv_fusion == "atomic":
            tp_a_i_k_l3_m3 = self.conv_tp.forward(w_h_j_k_l2_m2, edge_attrs, r_ij_k_l1_l2_l3, receiver, sender)
        else:
            # compute A_ij_k_l3_m3. Remember that edge_attrs = Y_ij_l1_m1. shape = [n_edges, irreps]
            tp_a_ij_k_l3_m3 = self.conv_tp(
                w_h_j_k_l2_m2[sender],
                edge_attrs,
                r_ij_k_l1_l2_l3,
            )

            # sum over neighbours, shape = [n_nodes, irreps]
            tp_a_i_k_l3_m3 = scatter(
                src=tp_a_ij_k_l3_m3,
                index=receiver,
                dim=0,
                dim_size=num_nodes,
            )

        # embed into correct interaction_irreps and divide by average num neighbours
        tp_a_i_k_l3_m3 = self.linear(tp_a_i_k_l3_m3) / self.avg_num_neighbours

        if self.mix_layer is not None:
            tp_a_i_k_l3_m3 = self.mix_layer(tp_a_i_k_l3_m3, node_attrs)

        # reshape from [n_nodes, channels * (lmax + 1)**2] -> [n_nodes, channels, (lmax + 1)**2]
        a_i_k_l3_m3: torch.Tensor = self.reshape(tp_a_i_k_l3_m3)

        return a_i_k_l3_m3


@compile_mode("script")
class ScaleShiftBlock(torch.nn.Module):
    def __init__(self, scale: float | None, shift: float | None) -> None:
        super().__init__()

        if scale is not None:
            self.register_buffer("scale", torch.tensor(scale))
        else:
            self.register_buffer("scale", torch.tensor(1.0))

        if shift is not None:
            self.register_buffer("shift", torch.tensor(shift))
        else:
            self.register_buffer("shift", torch.tensor(0.0))

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        scaled_shifted_x: torch.Tensor = self.scale * x + self.shift
        return scaled_shifted_x
