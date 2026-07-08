# Operating playbook

Use stage-invoker when one stage of an existing workspace needs to run or re-run on its own.

Name the stage, point at the project, choose dry or live, bound the fixes, run `asset_stage_run` and report the final state with the record paths. Stop on blocked stages and escalate with the findings attached.
