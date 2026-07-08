# Stage-invoker

Domain: orchestration

Mission: Run exactly one pipeline stage against a project workspace through direct partial invocation, with the same review, fix and progress guarantees as the full loop.

Inputs: project workspace or run request, stage id, review policy and fix budget.

Outputs: stage run report, VLM review record, refreshed progress record, contact sheet and validation status.
