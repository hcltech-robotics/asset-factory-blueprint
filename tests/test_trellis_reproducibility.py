from __future__ import annotations

import pytest

from asset_factory_blueprint.reconstruction_backends import trellis_reproducibility_arguments


def test_trellis_reproducibility_arguments_are_explicit_and_ordered() -> None:
    arguments = trellis_reproducibility_arguments(
        {
            "reproducibility": {
                "seed": 17,
                "pipeline_type": "512",
                "dtype": "float32",
                "max_num_tokens": 49152,
                "decimation_target": 1_000_000,
                "texture_size": 4096,
                "deterministic": True,
                "prune_unused_models": True,
                "cpu_image_cond": True,
                "skip_preprocess": False,
            }
        },
        {"id": "trellisv2"},
    )

    assert arguments == [
        "--seed",
        "17",
        "--pipeline-type",
        "512",
        "--dtype",
        "float32",
        "--max-num-tokens",
        "49152",
        "--decimation-target",
        "1000000",
        "--texture-size",
        "4096",
        "--deterministic",
        "--prune-unused-models",
        "--cpu-image-cond",
    ]


def test_trellis_reproducibility_rejects_unapproved_values() -> None:
    with pytest.raises(ValueError, match="unsupported TRELLIS reproducibility dtype"):
        trellis_reproducibility_arguments(
            {"reproducibility": {"dtype": "unsafe"}},
            {"id": "trellisv2"},
        )


@pytest.mark.parametrize("field", ["seed", "max_num_tokens", "decimation_target", "texture_size"])
def test_trellis_reproducibility_rejects_booleans_as_integers(field: str) -> None:
    with pytest.raises(ValueError, match="must be"):
        trellis_reproducibility_arguments(
            {"reproducibility": {field: True}},
            {"id": "trellisv2"},
        )
