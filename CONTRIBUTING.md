# Contributing

Contributions are welcome. The blueprint is contract-first: every change must keep the repository's own gates green.

## Ground rules

- Every stage-coupled change lands atomically across `configs/agent-workflow.json`, `configs/skill-registry.json`, `configs/repository-policy.json`, `configs/tool-surface.json`, `workflow.py`, `orchestrator.py`, the schema, the skill package, the prompt file and `scripts/generate_diagrams.py`.
- Public tools call one same-named service function and are mentioned in their prompt file.
- Generated results are proposals; never author code paths that promote numeric physical values from visual evidence.
- No secrets, customer data, machine-specific paths or benchmark claims in the repository.

## Workflow

1. Fork and branch.
2. Make the change; regenerate figures with `python scripts/generate_diagrams.py` when diagrams change.
3. Build the docs strictly: `make site`.
4. Run the verification suite from a checkout of the asset-factory-verification repository:

    ```bash
    set AFB_REPO_ROOT=<this checkout>
    pytest
    python repo_checks/validate_repository.py --repo-root %AFB_REPO_ROOT%
    python benchmarks/run_benchmarks.py --spec benchmarks/benchmark-spec.json --repo-root %AFB_REPO_ROOT%
    ```

5. Open a pull request describing the contract surfaces the change touches.
