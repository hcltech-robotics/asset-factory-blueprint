#!/bin/bash
#SBATCH --job-name=asset-factory
#SBATCH --cpus-per-task=8
#SBATCH --mem=32G
#SBATCH --time=00:30:00
#SBATCH --output=asset-factory-%j.out

set -euo pipefail

RUN_REQUEST="${RUN_REQUEST:-examples/run-requests/warehouse_pick_cell.json}"
PROJECT_ROOT="${PROJECT_ROOT:-projects}"

mkdir -p artifacts/slurm "${PROJECT_ROOT}"
srun afb skill-audit --root . --output "artifacts/slurm/skill-audit-${SLURM_JOB_ID}.json"
srun afb workflow run --request "${RUN_REQUEST}" --project-root "${PROJECT_ROOT}" --dry-run
