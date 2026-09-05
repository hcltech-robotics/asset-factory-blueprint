"""Publish README-linked repository documents from their canonical sources."""

import re
from pathlib import Path

from mkdocs.structure.files import File


REPOSITORY_PAGES = {
    "GOVERNANCE.md": "GOVERNANCE.md",
    "RELEASE.md": "RELEASE.md",
    "SUPPORT.md": "SUPPORT.md",
    "SECURITY.md": "SECURITY.md",
    "CODE_OF_CONDUCT.md": "CODE_OF_CONDUCT.md",
    "THIRD_PARTY_NOTICES.md": "THIRD_PARTY_NOTICES.md",
    "license.md": "LICENSE",
    "asset-programme-strategist.md": "skills/asset-programme-strategist/SKILL.md",
}


def on_files(files, config):
    root = Path(config.config_file_path).resolve().parent
    for destination, source in REPOSITORY_PAGES.items():
        content = (root / source).read_text(encoding="utf-8")
        if source == "LICENSE":
            content = "# " + content
        elif source.endswith("/SKILL.md"):
            content = re.sub(r"\A---\n.*?\n---\n", "", content, count=1, flags=re.S)
        # Root-level policy links remain siblings; docs/ links are now site-relative.
        content = content.replace("](docs/", "](")
        page = File.generated(config, destination, content=content)
        page.edit_uri = "../" + source
        files.append(page)
    return files
