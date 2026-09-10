---
description: "Benchmark a reconstruction pipeline with ARROW: select cases, fix the evidence, generate and submit meshes, evaluate them against measured references and compare configurations."
---

# How to benchmark with ARROW

An [ARROW](../arrow.md) benchmark compares a pipeline's reconstruction with an independently collected reference for the same physical object. Choose the cases and experimental conditions, run your pipeline on the selected observations, then evaluate its outputs with ARROW. These steps work with AFB and other reconstruction pipelines.

## 1. Define the comparison

Start with a question precise enough to test, such as whether changing the reconstruction model improves surface accuracy at the same compute budget. Decide which variable will change and which conditions will remain fixed before running the cases.

Record the dataset release, case IDs and selected description tier, along with the pipeline's source revision, agent driver, model revisions, prompts and preprocessing. Specify the generation settings, repeat count and seeds, resource budget and stopping rule. Use the same evaluator version, sampling settings and tolerances for every configuration.

Choose measures that answer the question. A geometry comparison might use surface error and completeness. When comparing agent drivers, also account for failed runs, retries, elapsed time and resource consumption. Record these as each attempt runs, including unsuccessful reconstructions.

## 2. Install the ARROW tools

Use Python 3.11 or 3.12 and a CUDA-capable evaluation machine. Install the public repository with its CUDA evaluation extra:

```bash
git clone https://github.com/hcltech-robotics/ARROW.git
cd ARROW
python -m pip install ".[evaluation-cuda]"
git rev-parse HEAD
arrow-bench --help
```

Save the commit printed by `git rev-parse HEAD` with the experiment and use that checkout for all evaluations in the comparison. The installation includes the Hugging Face data client. CUDA handles surface-distance queries and NumPy/SciPy handle registration.

The [public repository](https://github.com/hcltech-robotics/ARROW#install) contains the installation instructions and evaluator source.

## 3. Select the cases and prepare their inputs

Pin the dataset to a release tag or commit. This example uses a published release and selects one case from its `development` split, which contains cases with verified reference geometry. For a larger comparison, freeze an explicit list of case IDs and repeat the process for each one.

Save the following as `prepare_case.py` and run it with `python prepare_case.py`:

```python
import json
from pathlib import Path

from datasets import load_dataset
from arrow_bench.client import load_case

revision = "rel_01M23Z8GJMWPSZMBKKWGZ8H8MP"
objects = load_dataset(
    "chrisvoncsefalvay/ARROW",
    "overview",
    split="development",
    revision=revision,
)
case_id = objects[0]["case_id"]
evidence = load_case(
    case_id,
    revision=revision,
    description_tier="structured_observer",
)

work = Path("benchmark-work")
work.mkdir(exist_ok=True)
evidence_record = {
    **evidence,
    "photo_paths": [str(path) for path in evidence["photo_paths"]],
}
(work / "case-inputs.json").write_text(
    json.dumps(evidence_record, indent=2), encoding="utf-8"
)
print(evidence["description"])
print(evidence["photo_paths"])
```

This example uses the medium-length observer description, `structured_observer`. The helper returns the chosen description, its ID and the photograph identities and checksums. Keep `case-inputs.json` with the run records and give every configuration those same inputs. If the experiment calls for the original collector description, select the description specified by `case_inputs` in the dataset. The [data dictionary](https://github.com/hcltech-robotics/ARROW/blob/main/docs/data-dictionary.md#case_inputs) describes that relation.

Give the reconstruction process only the selected photographs and text. Keep reference scans, their rendered views and measured physical values in the evaluation workspace so they cannot influence the reconstruction being measured.

## 4. Run the reconstruction pipeline

Pass `evidence["photo_paths"]` and `evidence["description"]` to the pipeline being tested. With AFB, these are the source observations and description used for the reconstruction request. Run the chosen pipeline through its own interface and save the resulting mesh as `benchmark-work/generated.glb`.

Keep a separate output directory for each case, configuration and repeat. Record the exact settings, resources consumed, completion status and any error. When comparing models, preserve the surrounding stages and record any adapter or preprocessing changes needed to substitute one for another.

Retain the generated mesh in its declared units. Any repairs performed by the pipeline belong in the recipe and its run record. Submit the resulting bytes to ARROW for evaluation.

## 5. Register the generated mesh

After the pipeline has written its mesh, save this as `register_submission.py` and run it with `python register_submission.py`:

```python
import json
from pathlib import Path

from arrow_bench.client import write_submission

work = Path("benchmark-work")
evidence = json.loads((work / "case-inputs.json").read_text(encoding="utf-8"))
write_submission(
    work / "generated.glb",
    case_id=evidence["case_id"],
    output=work / "submission",
    declared_length_unit="m",
)
```

The helper creates a new submission directory containing `submission.yaml` and a copy of the mesh named `candidate.glb`, with its case ID and file checksum recorded in the submission. Use a fresh directory for each attempt and declare the length unit used by the generated geometry. ARROW accepts self-contained PLY, GLB and STL meshes.

Keep the pipeline revision and configuration records alongside the submission. If the pipeline predicts mass, pass its value and unit through the helper's `predicted_mass` argument. The [submission format](https://github.com/hcltech-robotics/ARROW/blob/main/docs/validation.md) defines the provenance and physical-prediction fields.

## 6. Evaluate against the matching reference

Download the same dataset release into the evaluation workspace, then score the submission:

```bash
arrow-bench download --revision rel_01M23Z8GJMWPSZMBKKWGZ8H8MP \
  --output ./benchmark-work/reference
arrow-bench evaluate ./benchmark-work/submission \
  --dataset-release ./benchmark-work/reference \
  --suite geometry-v0 --output ./benchmark-work/evaluation \
  --compute cuda --sample-count 20000 --seed 20260908
```

The evaluator checks the reference release's files and finds the canonical mesh using the submitted case ID. Reuse this frozen reference release across the comparison and choose a fresh evaluation output directory for each attempt. The command samples 20,000 points per surface with seed `20260908`. This controls evaluation sampling and is separate from the seeds used to generate the candidate. W&B records evaluations offline by default.

### Check the evaluator with the identity recipe

The identity recipe copies a case's reference mesh into a submission. Use it to check the data path and evaluator's exact-match behaviour. Replace `CASE_ID` with the case ID saved in `case-inputs.json`:

```bash
arrow-bench oracle --dataset-release ./benchmark-work/reference \
  --case-id CASE_ID --output ./benchmark-work/identity-submission
arrow-bench evaluate ./benchmark-work/identity-submission \
  --dataset-release ./benchmark-work/reference \
  --suite geometry-v0 --output ./benchmark-work/identity-evaluation \
  --compute cuda --sample-count 20000 --seed 20260908
```

Keep this evaluator check separate from pipeline results. Its recipe ID, [`arrow.identity-oracle`](https://github.com/hcltech-robotics/ARROW/blob/main/recipes/identity.yaml), identifies a reference self-comparison.

## 7. Read and compare the results

Open `benchmark-work/evaluation/report.html` to inspect the result. `report.json` contains the detailed geometry and physical records, while `evaluation.json` records the evaluation identity, attempts and transforms. Use `metrics.parquet` for analysis across runs and retain `evaluation-manifest.json` for the output inventory and checksums.

Read the evaluation status before comparing numerical scores. Keep both completed and failed attempts in the experiment's case inventory and record why any quantity is unavailable.

| Result | What it measures |
|---|---|
| Surface accuracy | Distance from the candidate surface to the reference. Lower error means the generated surface lies closer to the measured object. |
| Surface completeness | Distance from the reference surface to the candidate. This exposes parts of the measured object that the reconstruction missed. |
| Coverage and F-score | Agreement within the tolerances declared by the evaluator. Compare values at the same tolerance. |
| Normal agreement | Agreement between surface orientations at the sampled correspondences. |
| Topology | Mesh structure, including connected components, boundary edges, degeneracies and watertightness. |
| Metric size and physical values | Dimensions with the candidate's scale preserved, plus compatible mass, volume and density comparisons. |

Read shape and metric results separately. Shape evaluation uses similarity alignment, which can adjust scale, and reports distances relative to the reference bounding-box diagonal. Metric evaluation uses rigid alignment and metres, preserving scale error. Both transforms are retained. The [versioned metric profile](https://github.com/hcltech-robotics/ARROW/blob/main/evaluators/geometry-v0.yaml) specifies the sampling policy and thresholds.

Compare configurations on matching cases and repeats. Report per-case results alongside aggregate statistics, with the number attempted, number completed and coverage of each metric. For repeated runs, show the spread of results and state how any uncertainty interval was calculated. Report accuracy and completeness alongside time or cost so readers can judge a faster configuration against the quality of its output.

Store the frozen case list, selected evidence, resolved configuration, generated meshes and evaluation records together. Their revisions and checksums tie each reported improvement to the experiment that produced it.
