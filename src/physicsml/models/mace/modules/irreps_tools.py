###########################################################################################
# elementary tools for handling irreducible representations
# Authors: Ilyes Batatia, Gregor Simm
# This program is distributed under the MIT License (see MIT.md)
###########################################################################################


import torch
from e3nn import o3
from e3nn.util.jit import compile_mode

from .wrapper_ops import CuEquivarianceConfig


# Based on mir-group/nequip
def tp_out_irreps_with_instructions(
    irreps1: o3.Irreps,
    irreps2: o3.Irreps,
    target_irreps: o3.Irreps,
) -> tuple[o3.Irreps, list]:
    trainable = True

    # Collect possible irreps and their instructions
    irreps_out_list: list[tuple[int, o3.Irreps]] = []
    instructions = []
    for i, (mul, ir_in) in enumerate(irreps1):
        for j, (_, ir_edge) in enumerate(irreps2):
            for ir_out in ir_in * ir_edge:  # | l1 - l2 | <= l <= l1 + l2
                if ir_out in target_irreps:
                    k = len(irreps_out_list)  # instruction index
                    irreps_out_list.append((mul, ir_out))
                    instructions.append((i, j, k, "uvu", trainable))

    # We sort the output irreps of the tensor product so that we can simplify them
    # when they are provided to the second o3.Linear
    irreps_out = o3.Irreps(irreps_out_list)
    irreps_out, permut, _ = irreps_out.sort()

    # Permute the output indexes of the instructions to match the sorted irreps:
    instructions = [
        (i_in1, i_in2, permut[i_out], mode, train)
        for i_in1, i_in2, i_out, mode, train in instructions
    ]

    return irreps_out, instructions


@compile_mode("script")
class reshape_irreps(torch.nn.Module):
    def __init__(
        self, irreps: o3.Irreps, cueq_config: CuEquivarianceConfig | None = None
    ) -> None:
        super().__init__()
        self.irreps = o3.Irreps(irreps)
        self.cueq_config = cueq_config
        self.dims = []
        self.muls = []
        for mul, ir in self.irreps:
            d = ir.dim
            self.dims.append(d)
            self.muls.append(mul)

    def forward(self, tensor: torch.Tensor) -> torch.Tensor:
        ix = 0
        out = []
        batch, _ = tensor.shape
        for mul, d in zip(self.muls, self.dims):
            field = tensor[:, ix : ix + mul * d]  # [batch, sample, mul * repr]
            ix += mul * d
            if self.cueq_config is not None:
                if self.cueq_config.layout_str == "mul_ir":
                    field = field.reshape(batch, mul, d)
                else:
                    field = field.reshape(batch, d, mul)
            else:
                field = field.reshape(batch, mul, d)
            out.append(field)

        if self.cueq_config is not None:
            if self.cueq_config.layout_str == "mul_ir":
                return torch.cat(out, dim=-1)
            return torch.cat(out, dim=-2)
        return torch.cat(out, dim=-1)