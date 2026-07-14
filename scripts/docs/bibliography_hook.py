"""MkDocs hook that renders the complete ``references.bib`` onto the references page.

The ``mkdocs-bibtex`` plugin renders the citations used on each page as footnotes.
This hook complements it with a deterministic, alphabetical list of every entry in
``references.bib`` so that the site carries the full bibliography the citation page
promises, independent of which entries happen to be cited where. Rendering goes
through pandoc with the same CSL style as the in-text citations; when pandoc is not
available the hook falls back to pybtex so a local build never breaks on it.

The hook also tidies the per-page footnotes the plugin emits, because pandoc's
markdown writer leaves span markup, escaped underscores and ``--`` page ranges
that Python-Markdown does not understand.

Wire it in ``mkdocs.yml`` under ``hooks`` and place ``<!-- full-bibliography -->``
in the page that should carry the list.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

MARKER = "<!-- full-bibliography -->"
LOGGER = logging.getLogger("mkdocs.hooks.bibliography")


def _config_paths(config) -> tuple[Path, Path | None]:
    root = Path(config.config_file_path).resolve().parent
    bib_file = root / "references.bib"
    csl_file: Path | None = None
    for plugin_name, plugin in config.plugins.items():
        if plugin_name != "bibtex":
            continue
        plugin_config = getattr(plugin, "config", {})
        if plugin_config.get("bib_file"):
            bib_file = root / plugin_config["bib_file"]
        if plugin_config.get("csl_file"):
            csl_file = root / plugin_config["csl_file"]
    return bib_file, csl_file


def _render_with_pandoc(bib_file: Path, csl_file: Path | None) -> str | None:
    try:
        import pypandoc
    except ImportError:
        return None
    header = ["---", f'bibliography: "{bib_file.as_posix()}"', "nocite: '@*'", "link-citations: true"]
    if csl_file is not None:
        header.append(f'csl: "{csl_file.as_posix()}"')
    header.extend(["---", "", "::: {#refs}", ":::", ""])
    try:
        html = pypandoc.convert_text("\n".join(header), to="html", format="markdown", extra_args=["--citeproc"])
    except (OSError, RuntimeError) as exc:
        LOGGER.warning("pandoc rendering of the bibliography failed, falling back to pybtex: %s", exc)
        return None
    return html.strip()


def _render_with_pybtex(bib_file: Path) -> str:
    from pybtex.database import parse_file
    from pybtex.style.formatting.plain import Style

    database = parse_file(str(bib_file))
    style = Style()
    entries = []
    for key in sorted(database.entries, key=lambda item: item.lower()):
        formatted = style.format_entry(key, database.entries[key])
        entries.append(f"- {formatted.text.render_as('markdown')}")
    return "\n".join(entries)


FOOTNOTE_LINE = re.compile(r"^\[\^[^\]]+\]:.*$", re.M)
NOCASE_SPAN = re.compile(r"\[([^\]]+)\]\{\.nocase\}")
PAGE_RANGE = re.compile(r"(?<=\d)--(?=\d)")
ESCAPED_UNDERSCORE = re.compile(r"\\+_")


def _tidy_footnote(line: str) -> str:
    """Undo pandoc markdown-writer artefacts in a footnote emitted by mkdocs-bibtex.

    The plugin renders CSL output through pandoc's markdown writer, which wraps
    case-protected venue names in ``[...]{.nocase}`` spans, escapes underscores
    inside link text and writes en dashes back as ``--``. None of those survive
    Python-Markdown, so they are reversed here for the footnote lines only.
    """
    line = NOCASE_SPAN.sub(r"\1", line)
    line = ESCAPED_UNDERSCORE.sub("_", line)
    return PAGE_RANGE.sub("\u2013", line)


def on_page_markdown(markdown: str, page, config, files) -> str:
    markdown = FOOTNOTE_LINE.sub(lambda match: _tidy_footnote(match.group(0)), markdown)
    if MARKER not in markdown:
        return markdown
    bib_file, csl_file = _config_paths(config)
    if not bib_file.exists():
        raise FileNotFoundError(f"bibliography file not found: {bib_file}")
    rendered = _render_with_pandoc(bib_file, csl_file)
    if rendered is None:
        rendered = _render_with_pybtex(bib_file)
    from pybtex.database import parse_file

    count = len(parse_file(str(bib_file)).entries)
    preface = f"*{count} entries, rendered from `references.bib` at build time.*\n\n"
    return markdown.replace(MARKER, preface + rendered)
