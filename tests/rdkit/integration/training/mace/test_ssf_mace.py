import tempfile

import molflux.modelzoo as mz
import torch


def test_training_ssf_mace(featurised_gdb9_atomic_nums_and_feats_and_bond_feats):
    (
        dataset_feated,
        x_features,
        featurisation_metadata,
    ) = featurised_gdb9_atomic_nums_and_feats_and_bond_feats

    # specify the model config
    model_config = {
        "name": "ssf_mace_model",  # the model name
        "config": {
            "x_features": x_features,
            "y_features": ["u0"],
            "datamodule": {
                "y_graph_scalars": ["u0"],
                "num_elements": 4,
            },
            "num_node_feats": 27,
            "num_edge_feats": 12,
            "num_bessel": 8,
            "num_polynomial_cutoff": 6,
            "max_ell": 2,
            "num_interactions": 2,
            "hidden_irreps": "4x0e + 4x1o",
            "mlp_irreps": "5x0e",
            "avg_num_neighbours": 10.0,
            "correlation": 2,
            "y_graph_scalars_loss_config": {
                "name": "MSELoss",
                "weight": 1.0,
            },
        },
    }

    model = mz.load_from_dict(model_config)

    with tempfile.TemporaryDirectory() as tmpdir:
        model.train(
            train_data=dataset_feated,
            validation_data=dataset_feated,
            trainer_config={
                "accelerator": "gpu" if torch.cuda.is_available() else "cpu",
                "max_epochs": 10,
                "default_root_dir": tmpdir,
            },
            datamodule_config={
                "train": {"batch_size": 4},
                "num_workers": 0,
            },
        )

    preds = model.predict(
        dataset_feated,
        trainer_config={"accelerator": "gpu" if torch.cuda.is_available() else "cpu"},
    )

    assert "ssf_mace_model::u0" in preds.keys()
    assert len(preds) == 1
    assert len(preds["ssf_mace_model::u0"]) == 100

    assert (
        (torch.tensor(preds["ssf_mace_model::u0"]) - torch.tensor(dataset_feated["u0"]))
        ** 2
    ).mean().sqrt() < 1.0
