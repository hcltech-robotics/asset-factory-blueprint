# How validation works here

The factory's doctrine: generation accelerates work, validation promotes work. Everything an agent produces is a proposal until gates pass.

## Gate families

- Schema gates: every stage manifest validates against its JSON schema before downstream stages may consume it.
- Source lineage gates: artefacts trace to checksummed source evidence.
- Domain gates: segmentation-segments (masks, labels and material targets exist), material-evidence, texture-uv, nonvisual-evidence.
- VLM sign-off: every content stage carries the vlm-signoff gate; a vision reviewer judges the artefacts against a stage rubric with a controlled defect vocabulary and its verdict is recorded as a vlm-review-record.
- Runtime gates: the isaac-load gate records whether the packaged asset loads in the configured runtime.
- Governance gates: rights, retention, reviewer decisions and release status.

## Promotion states

`proposal` means generated and recorded; `review_required` means a human must accept a weak or task-critical claim; `validated` means the required gates passed; `blocked` means a named condition prevents progress. Blocked is a healthy state: it says exactly what is missing.

## What agents must never do

- Never mark numeric physical values as validated from visual evidence.
- Never approve your own output; sign-off comes from the reviewer role and the recorded gates.
- Never delete or weaken a blocked reason; resolve it or escalate it.
- Never author around a failing gate; the fix library and escalation paths exist for that.

## Where results live

Per project: `manifests/` holds stage manifests, `reports/` holds stage reports, VLM reviews and fix attempts, `evidence/checksums.json` holds file hashes, `progress.json` and `reports/contact-sheet.md` hold the rolled-up state.
