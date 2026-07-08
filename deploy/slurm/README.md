# Slurm

The Slurm template runs a finite dry-run workflow and stores scheduler output under `artifacts/slurm/`. Submit it from an extracted repository checkout after installing the package into that environment.

Override `RUN_REQUEST` and `PROJECT_ROOT` through exported variables. Add the site's account, partition, GPU and container directives explicitly for live reconstruction; the checked-in dry run does not request a GPU.
