from dataclasses import field

from molflux.modelzoo.models.lightning.config import OptimizerConfig, SchedulerConfig
from pydantic.v1 import dataclasses

from physicsml.lightning.config import ConfigDict, PhysicsMLModelConfig


@dataclasses.dataclass(config=ConfigDict)
class MACEModelConfig(PhysicsMLModelConfig):
    num_elements: int = 0
    use_cueq: bool = False
    use_oeq: bool = False
    num_node_feats: int = 0
    num_edge_feats: int = 0
    num_bessel: int = 8
    num_polynomial_cutoff: int = 5
    max_ell: int = 3
    num_interactions: int = 2
    hidden_irreps: str = "128x0e + 128x1o"
    mlp_irreps: str = "16x0e"
    avg_num_neighbours: float | None = None
    correlation: int = 3
    scaling_mean: float = 0.0
    scaling_std: float = 1.0
    y_node_scalars_loss_config: dict | None = None
    y_graph_scalars_loss_config: dict | None = None
    y_node_vector_loss_config: dict | None = None
    y_graph_vector_loss_config: dict | None = None
    optimizer: OptimizerConfig = field(
        default_factory=lambda: OptimizerConfig(
            name="Adam",
            config={
                "lr": 1e-2,
                "amsgrad": True,
                "weight_decay": 5e-7,
            },
        ),
    )
    scheduler: SchedulerConfig | None = field(
        default_factory=lambda: SchedulerConfig(
            name="ReduceLROnPlateau",
            config={
                "factor": 0.8,
                "patience": 50,
            },
            monitor="val/total/loss",
            interval="epoch",
        ),
    )
