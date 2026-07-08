from __future__ import annotations

import json
from pathlib import Path


REQUIRED = [
    "SKILL.md",
    "skill-card.md",
    "references/operating-playbook.md",
    "references/output-contract.md",
    "agents/openai.yaml",
]


def audit(root: str | Path = ".") -> dict:
    base = Path(root)
    registry = json.loads((base / "configs" / "skill-registry.json").read_text(encoding="utf-8"))
    results = []
    errors = []
    for item in registry["skills"]:
        skill_dir = base / item["package"]
        missing = [rel for rel in REQUIRED if not (skill_dir / rel).exists()]
        line_count = 0
        if (skill_dir / "SKILL.md").exists():
            line_count = len((skill_dir / "SKILL.md").read_text(encoding="utf-8").splitlines())
        if missing:
            errors.append(f"{item['name']} missing {', '.join(missing)}")
        if line_count < 120:
            errors.append(f"{item['name']} SKILL.md has {line_count} lines")
        results.append({"name": item["name"], "missing": missing, "skill_md_lines": line_count, "ready": not missing and line_count >= 120})
    return {"ok": not errors, "errors": errors, "skills": results}


def write_audit(root: str | Path, output: str | Path) -> dict:
    result = audit(root)
    target = Path(output)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result
