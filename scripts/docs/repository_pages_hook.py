"""Publish reference documentation from canonical repository sources."""

import re
from pathlib import Path

from mkdocs.structure.files import File


REPOSITORY_PAGES = {
    "asset-programme-strategist.md": "skills/asset-programme-strategist/SKILL.md",
}


def on_files(files, config):
    root = Path(config.config_file_path).resolve().parent
    for destination, source in REPOSITORY_PAGES.items():
        content = (root / source).read_text(encoding="utf-8")
        if source.endswith("/SKILL.md"):
            content = re.sub(r"\A---\n.*?\n---\n", "", content, count=1, flags=re.S)
        # Repository docs/ links become site-relative.
        content = content.replace("](docs/", "](")
        page = File.generated(config, destination, content=content)
        page.edit_uri = "../" + source
        files.append(page)
    return files
