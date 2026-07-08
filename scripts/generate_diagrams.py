#!/usr/bin/env python3
"""Generate the documentation figures for the asset factory blueprint.

Figures are composed SVG schematics: titled panels with item chips, highlighted
borders for NVIDIA surfaces, accent connectors for the hot path. Check mode
regenerates every figure in memory and fails on drift against the committed
assets. PNG export through a headless Chromium is optional.
"""

from __future__ import annotations

import argparse
import os
import shutil
from html import escape
from pathlib import Path
from xml.etree import ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "docs" / "assets"
DEFAULT_PNG_DIR = ROOT / "artifacts" / "diagrams"

THEME = {
    "bg": "transparent",
    "panel": "#0a100c",
    "panel_hot": "#0b1608",
    "panel_accent": "#160929",
    "inner_accent": "#1b0c31",
    "stroke": "#7d8780",
    "stroke_dim": "#3d4a42",
    "green": "#76b900",
    "accent": "#5f1ebe",
    "text": "#edf3ef",
    "muted": "#c6cec8",
}

GREEN_BORDER_LABELS = {
    "isaac",
    "isaacsim",
    "isaaclab",
    "nim",
    "omniverse",
    "simready",
    "usdsearch",
    "vmaterials",
    "physx",
    "nucleus",
}


def label_key(label: str) -> str:
    return "".join(ch for ch in label.lower() if ch.isalnum())


def has_green_border(label: str) -> bool:
    key = label_key(label)
    return key in GREEN_BORDER_LABELS or key.startswith(("isaac", "omniverse", "simready", "usdsearch", "vmaterials"))


def svg_open(title: str, desc: str, width: int = 1360, height: int = 620) -> list[str]:
    return [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" aria-labelledby="title desc">',
        f'  <title id="title">{escape(title)}</title>',
        f'  <desc id="desc">{escape(desc)}</desc>',
        "  <defs>",
        "    <style>",
        f"      .bg {{ fill: {THEME['bg']}; }}",
        f"      .box {{ fill: {THEME['panel']}; stroke: {THEME['stroke_dim']}; stroke-width: 2; }}",
        f"      .boxHot {{ fill: {THEME['panel_hot']}; stroke: {THEME['green']}; stroke-width: 2.3; }}",
        f"      .boxAccent {{ fill: {THEME['panel_accent']}; stroke: {THEME['accent']}; stroke-width: 2.3; }}",
        f"      .inner {{ fill: #0d130f; stroke: {THEME['stroke']}; stroke-width: 1.9; }}",
        f"      .innerHot {{ fill: #0d1809; stroke: {THEME['green']}; stroke-width: 1.9; }}",
        f"      .innerAccent {{ fill: {THEME['inner_accent']}; stroke: {THEME['accent']}; stroke-width: 1.9; }}",
        f"      .label {{ fill: {THEME['text']}; font-family: Aptos, Helvetica, sans-serif; font-size: 20px; font-weight: 680; }}",
        f"      .small {{ fill: {THEME['muted']}; font-family: Aptos, Helvetica, sans-serif; font-size: 13px; font-weight: 620; }}",
        "      .line { stroke: #e6ece8; stroke-width: 2.2; fill: none; marker-end: url(#arrow); }",
        f"      .greenLine {{ stroke: {THEME['green']}; stroke-width: 2.4; fill: none; marker-end: url(#arrowGreen); }}",
        f"      .accentLine {{ stroke: {THEME['accent']}; stroke-width: 2.4; fill: none; marker-end: url(#arrowAccent); }}",
        "    </style>",
        '    <marker id="arrow" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="5" markerHeight="5" orient="auto-start-reverse">',
        '      <path d="M 0 1 L 8 4 L 0 7 z" fill="#e6ece8"/>',
        "    </marker>",
        '    <marker id="arrowGreen" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="5" markerHeight="5" orient="auto-start-reverse">',
        f'      <path d="M 0 1 L 8 4 L 0 7 z" fill="{THEME["green"]}"/>',
        "    </marker>",
        '    <marker id="arrowAccent" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="5" markerHeight="5" orient="auto-start-reverse">',
        f'      <path d="M 0 1 L 8 4 L 0 7 z" fill="{THEME["accent"]}"/>',
        "    </marker>",
        "  </defs>",
    ]


def text_size(label: str) -> int:
    if len(label) > 24:
        return 10
    if len(label) > 18:
        return 11
    if len(label) > 14:
        return 12
    return 13


def inner_box(x: float, y: float, w: float, h: float, label: str, hot: bool = False, accent: bool = False) -> list[str]:
    cls = "innerHot" if hot else "innerAccent" if accent else "inner"
    size = min(text_size(label), max(8, int(h) - 6))
    return [
        f'  <rect class="{cls}" x="{x:.0f}" y="{y:.0f}" width="{w:.0f}" height="{h:.0f}" rx="4"/>',
        f'  <text class="small" x="{x + w / 2:.1f}" y="{y + h / 2 + size / 2 - 2:.1f}" text-anchor="middle" style="font-size:{size}px">{escape(label)}</text>',
    ]


def outer_box(
    x: float,
    y: float,
    w: float,
    h: float,
    title: str,
    items: list[str],
    hot_items: set[str] | None = None,
    hot: bool = False,
    title_size: int | None = None,
) -> list[str]:
    hot_items = hot_items or set()
    title_is_nvidia = has_green_border(title)
    cls = "boxHot" if title_is_nvidia else "boxAccent" if hot else "box"
    out = [f'  <rect class="{cls}" x="{x:.0f}" y="{y:.0f}" width="{w:.0f}" height="{h:.0f}" rx="8"/>']
    title_y = y + (34 if h <= 110 else 42)
    style = f' style="font-size:{title_size}px"' if title_size else ""
    out.append(f'  <text class="label" x="{x + w / 2:.1f}" y="{title_y:.0f}" text-anchor="middle"{style}>{escape(title)}</text>')
    if not items:
        return out
    margin = 10 if w < 150 else 22
    ix = x + margin
    iw = w - 2 * margin
    content_top = y + (48 if h <= 110 else 64)
    content_bottom = y + h - 16
    available = content_bottom - content_top
    gap = 8 if h <= 170 else 12
    ih = min(34, max(16, int((available - gap * (len(items) - 1)) / len(items))))
    total_height = ih * len(items) + gap * (len(items) - 1)
    start_y = content_top + max(0, int((available - total_height) / 2))
    for i, item in enumerate(items):
        item_is_nvidia = has_green_border(item)
        out.extend(
            inner_box(
                ix,
                start_y + i * (ih + gap),
                iw,
                ih,
                item,
                hot=item_is_nvidia,
                accent=item in hot_items and not item_is_nvidia,
            )
        )
    return out


def arrow(x1: float, y1: float, x2: float, y2: float, hot: bool = False, accent: bool = False) -> str:
    cls = "greenLine" if hot else "accentLine" if accent else "line"
    return f'  <path class="{cls}" d="M{x1:.0f} {y1:.0f} L{x2:.0f} {y2:.0f}"/>'


def elbow(points: list[tuple[float, float]], hot: bool = False, accent: bool = False) -> str:
    cls = "greenLine" if hot else "accentLine" if accent else "line"
    path = [f"M{points[0][0]:.0f} {points[0][1]:.0f}"]
    path.extend(f"L{x:.0f} {y:.0f}" for x, y in points[1:])
    return f'  <path class="{cls}" d="{" ".join(path)}"/>'


def trunk(points: list[tuple[float, float]], hot: bool = False, accent: bool = False) -> str:
    colour = THEME["green"] if hot else THEME["accent"] if accent else "#e6ece8"
    path = [f"M{points[0][0]:.0f} {points[0][1]:.0f}"]
    path.extend(f"L{x:.0f} {y:.0f}" for x, y in points[1:])
    return f'  <path d="{" ".join(path)}" style="stroke:{colour};stroke-width:2.2;fill:none"/>'


def junction(x: float, y: float, accent: bool = False) -> str:
    colour = THEME["accent"] if accent else "#e6ece8"
    return f'  <circle cx="{x:.0f}" cy="{y:.0f}" r="4" fill="{colour}"/>'


def flow_row(
    lines: list[str],
    boxes: list[tuple[str, list[str], set[str], bool]],
    x0: float,
    y: float,
    w: float,
    h: float,
    gap: float,
    accent_arrows: set[int] | None = None,
) -> None:
    accent_arrows = accent_arrows or set()
    for i, (title, items, hot_items, hot) in enumerate(boxes):
        x = x0 + i * (w + gap)
        lines.extend(outer_box(x, y, w, h, title, items, hot_items, hot=hot))
        if i < len(boxes) - 1:
            lines.append(arrow(x + w, y + h / 2, x + w + gap - 4, y + h / 2, accent=i in accent_arrows))


def render(diagram: dict) -> str:
    lines = svg_open(diagram["title"], diagram["desc"], diagram.get("width", 1360), diagram.get("height", 620))
    diagram["draw"](lines)
    lines.append("</svg>")
    return "\n".join(lines) + "\n"


def draw_architecture(lines: list[str]) -> None:
    x0, y, w, h, gap = 62, 108, 222, 236, 22
    flow_row(
        lines,
        [
            ("Sources", ["CAD / OpenUSD", "photos + video", "robot descriptions"], {"photos + video"}, False),
            ("Workspace", ["immutable copies", "checksums", "manifests"], {"manifests"}, False),
            ("Stages", ["proposals", "services + tools", "layer authority"], {"proposals"}, False),
            ("Validation", ["formal gates", "VLM sign-off", "reports"], {"VLM sign-off"}, True),
            ("Release", ["governance", "promotion state", "SimReady package"], set(), False),
        ],
        x0,
        y,
        w,
        h,
        gap,
        accent_arrows={2},
    )
    agent_x, agent_y, agent_w, agent_h = 372, 424, 616, 150
    lines.extend(
        outer_box(
            agent_x,
            agent_y,
            agent_w,
            agent_h,
            "Agent control",
            ["agent loop", "fix library", "library grounding"],
            {"agent loop", "fix library", "library grounding"},
            hot=True,
        )
    )
    stages_cx = x0 + 2 * (w + gap) + w / 2
    validation_cx = x0 + 3 * (w + gap) + w / 2
    lines.append(arrow(agent_x + agent_w * 0.3, agent_y, stages_cx, y + h, accent=True))
    lines.append(elbow([(validation_cx, y + h), (validation_cx, agent_y - 22), (agent_x + agent_w * 0.7, agent_y - 22), (agent_x + agent_w * 0.7, agent_y)], accent=True))


def draw_pipeline(lines: list[str]) -> None:
    stage_x0, stage_y, stage_w, stage_h, stage_gap = 222, 74, 131, 212, 8
    lines.extend(outer_box(30, stage_y, 180, stage_h, "Pre-pipeline", ["intake", "source ingestion"], {"source ingestion"}, hot=False, title_size=17))
    stages = [
        ("1 Reconstruct", ["optional", "mesh from views"], set()),
        ("2 Segment", ["masks", "semantics"], set()),
        ("3 Materials", ["classes", "physical props"], set()),
        ("4 Texture", ["PBR maps", "decals"], set()),
        ("5 Physics", ["joints", "grasps"], set()),
        ("6 Nonvisual", ["optional", "thermal, acoustic"], set()),
        ("7 Verify", ["package", "USD gates"], set()),
    ]
    for i, (title, items, hot_items) in enumerate(stages):
        x = stage_x0 + i * (stage_w + stage_gap)
        hot = title in {"1 Reconstruct", "6 Nonvisual"}
        lines.extend(outer_box(x, stage_y, stage_w, stage_h, title, items, hot_items, hot=hot, title_size=16))
        if i < len(stages) - 1:
            lines.append(arrow(x + stage_w, stage_y + stage_h / 2, x + stage_w + stage_gap - 4, stage_y + stage_h / 2))
    lines.append(arrow(210, 180, stage_x0 - 4, 180))
    lines.extend(outer_box(1200, stage_y, 140, stage_h, "Downstream", ["RL contracts", "layout sweeps"], set(), hot=False, title_size=17))
    last_x = stage_x0 + 6 * (stage_w + stage_gap)
    lines.append(arrow(last_x + stage_w, 180, 1196, 180))
    stage4_cx = stage_x0 + 3 * (stage_w + stage_gap) + stage_w / 2
    loop_w, loop_h = 600, 150
    loop_x, loop_y = stage4_cx - loop_w / 2, 400
    lines.extend(
        outer_box(loop_x, loop_y, loop_w, loop_h, "Every stage", ["formal gates", "VLM sign-off", "fix or escalate"], {"VLM sign-off", "fix or escalate"}, hot=True)
    )
    lines.append(elbow([(stage4_cx, stage_y + stage_h), (stage4_cx, loop_y)], accent=True))


def draw_storyboard(lines: list[str]) -> None:
    x0, y, w, h, gap = 52, 96, 200, 250, 16
    flow_row(
        lines,
        [
            ("Capture", ["photos", "video", "objective text"], {"photos"}, False),
            ("Priors", ["segment masks", "part count", "conditioning image"], set(), False),
            ("Backend", ["TRELLIS.2", "Hunyuan3D", "PartCrafter", "DUSt3R"], set(), True),
            ("Layer stack", ["geo / mtl / phy", "art / sem", "variants"], set(), False),
            ("Review", ["VLM verdicts", "gates", "fix attempts"], {"VLM verdicts"}, False),
            ("Package", ["SimReady", "checksums", "provenance"], set(), False),
        ],
        x0,
        y,
        w,
        h,
        gap,
        accent_arrows={1},
    )
    lines.extend(outer_box(452, 430, 460, 130, "Evidence never mutates", ["source copies stay immutable"], set(), hot=False))
    lines.append(elbow([(x0 + w / 2, y + h), (x0 + w / 2, 495), (452, 495)]))


def draw_segmentation_lane(lines: list[str]) -> None:
    x0, y, w, h, gap = 60, 120, 232, 230, 20
    flow_row(
        lines,
        [
            ("Inputs", ["source photos", "reconstructed mesh"], set(), False),
            ("Prior", ["SAM masks", "appearance CV", "part hints"], {"SAM masks"}, True),
            ("Segments", ["stable ids", "labels", "material regions"], {"stable ids"}, False),
            ("Conditioning", ["heal", "smooth by selector", "prune fragments"], set(), False),
            ("Manifest", ["segment masks", "semantics", "consumers"], set(), False),
        ],
        x0,
        y,
        w,
        h,
        gap,
        accent_arrows={1},
    )
    lines.extend(outer_box(420, 420, 520, 150, "Downstream consumers", ["materials", "texturing", "verification"], set(), hot=False))
    seg_cx = x0 + 2 * (w + gap) + w / 2
    lines.append(elbow([(seg_cx, y + h), (seg_cx, 388), (680, 388), (680, 420)], accent=True))


def draw_material_lane(lines: list[str]) -> None:
    x0, y, w, h, gap = 60, 120, 232, 230, 20
    flow_row(
        lines,
        [
            ("Evidence", ["photos", "metadata", "segments"], set(), False),
            ("Grounding", ["exemplar index", "property dictionary", "estate search"], {"exemplar index", "property dictionary"}, True),
            ("Candidates", ["constrained classes", "bindings", "confidence"], set(), False),
            ("Physical proposals", ["mass, density", "friction ranges", "library priors"], {"library priors"}, False),
            ("Review", ["uncertainty", "gates", "human decision"], set(), False),
        ],
        x0,
        y,
        w,
        h,
        gap,
        accent_arrows={1},
    )
    lines.extend(outer_box(420, 420, 520, 130, "Never promoted from pixels", ["numeric values stay review gated"], set(), hot=False))
    prop_cx = x0 + 3 * (w + gap) + w / 2
    lines.append(elbow([(prop_cx, y + h), (prop_cx, 388), (680, 388), (680, 420)]))


def draw_texturing_lane(lines: list[str]) -> None:
    x0, y, w, h, gap = 60, 120, 232, 230, 20
    flow_row(
        lines,
        [
            ("Material manifest", ["classes", "bindings", "map policy"], set(), False),
            ("Prompting", ["prompt", "negative prompt", "seeds"], set(), False),
            ("Generation", ["PBR map sets", "variants", "deformations"], {"PBR map sets"}, True),
            ("Decals", ["segment targets", "placement", "evidence"], set(), False),
            ("Consistency", ["policy checks", "render evidence", "VLM review"], {"VLM review"}, False),
        ],
        x0,
        y,
        w,
        h,
        gap,
        accent_arrows={1},
    )
    lines.extend(outer_box(420, 420, 520, 150, "Sources", ["indexed backings", "cached packs", "live providers"], set(), hot=False))
    lines.append(elbow([(680, 420), (680, 380), (x0 + 2 * (w + gap) + w / 2, 380), (x0 + 2 * (w + gap) + w / 2, y + h)], accent=True))


def draw_consistency_lane(lines: list[str]) -> None:
    x0, y, w, h, gap = 60, 150, 232, 220, 20
    flow_row(
        lines,
        [
            ("Visible cue", ["rust", "gloss", "wear"], set(), False),
            ("Material class", ["library match", "exemplar ranges"], {"library match"}, False),
            ("Property claim", ["friction", "mass", "stiffness"], set(), False),
            ("Contradiction check", ["policy rules", "range tests"], {"policy rules"}, True),
            ("Review state", ["approve", "block with reasons"], set(), False),
        ],
        x0,
        y,
        w,
        h,
        gap,
        accent_arrows={2},
    )


def draw_physics_lane(lines: list[str]) -> None:
    x0, y, w, h, gap = 46, 120, 200, 230, 16
    flow_row(
        lines,
        [
            ("Proposals", ["validated props", "review approved"], set(), False),
            ("Bodies", ["rigid bodies", "colliders", "mass"], set(), False),
            ("Joints", ["axes", "limits", "drives"], set(), False),
            ("Grasps", ["points + frames", "approach vectors", "gripper width"], {"points + frames"}, True),
            ("Scenarios", ["drop tests", "motion checks", "solver stability"], set(), False),
            ("Layers", ["phy.usda", "art.usda", "sem semantics"], set(), False),
        ],
        x0,
        y,
        w,
        h,
        gap,
        accent_arrows={0},
    )
    lines.extend(outer_box(420, 420, 520, 130, "PhysX consumes the opinions", ["UsdPhysics schemas only"], set(), hot=True))
    lines.append(elbow([(x0 + 5 * (w + gap) + w / 2, y + h), (x0 + 5 * (w + gap) + w / 2, 485), (940, 485)]))


def draw_nonvisual_lane(lines: list[str]) -> None:
    x0, y, w, h, gap = 60, 150, 232, 220, 20
    flow_row(
        lines,
        [
            ("Evidence rank", ["measured", "spec sheet", "material prior"], {"measured"}, False),
            ("Values", ["thermal", "acoustic", "electrical"], set(), False),
            ("Units + bounds", ["SI units", "ranges", "uncertainty"], set(), True),
            ("Review", ["task critical", "operator decision"], set(), False),
            ("Manifest", ["citations", "review states"], set(), False),
        ],
        x0,
        y,
        w,
        h,
        gap,
        accent_arrows={1},
    )


def draw_simready_gates(lines: list[str]) -> None:
    x0, y, w, h, gap = 60, 120, 232, 230, 20
    flow_row(
        lines,
        [
            ("Layer stack", ["composition", "references", "defaultPrim"], set(), False),
            ("Package checks", ["bindings", "units + axis", "self contained"], set(), False),
            ("Runtime load", ["Isaac Sim", "prim counts", "load report"], set(), False),
            ("Performance", ["budget", "measurements"], set(), False),
            ("Decision", ["promote", "review", "block with reasons"], {"promote"}, True),
        ],
        x0,
        y,
        w,
        h,
        gap,
        accent_arrows={3},
    )
    lines.extend(outer_box(420, 420, 520, 130, "Final visual review", ["packaged render against source photos"], {"packaged render against source photos"}, hot=True))
    lines.append(elbow([(680, 420), (680, 380), (x0 + 4 * (w + gap) + w / 2, 380), (x0 + 4 * (w + gap) + w / 2, y + h)], accent=True))


def draw_rl_loop(lines: list[str]) -> None:
    x0, y, w, h, gap = 60, 120, 232, 230, 20
    flow_row(
        lines,
        [
            ("SimReady asset", ["validated package", "grasp affordances"], set(), False),
            ("Task contract", ["observations", "actions", "resets"], set(), False),
            ("Reward + curriculum", ["reward terms", "randomisation"], set(), False),
            ("Training", ["Isaac Lab", "smoke rollout"], set(), False),
            ("Policy evidence", ["metrics", "evaluation runs"], {"metrics"}, True),
        ],
        x0,
        y,
        w,
        h,
        gap,
    )
    lines.append(elbow([(x0 + 4 * (w + gap) + w / 2, y + h), (x0 + 4 * (w + gap) + w / 2, 470), (x0 + w / 2, 470), (x0 + w / 2, y + h)], accent=True))
    lines.append('  <text class="small" x="680" y="492" text-anchor="middle">better assets feed better policies; policy gaps feed the next asset run</text>')


def draw_agent_workflow(lines: list[str]) -> None:
    x0, y, w, h, gap = 70, 90, 260, 180, 60
    flow_row(
        lines,
        [
            ("Run request", ["objective", "sources", "outputs"], set(), False),
            ("Orchestrator", ["stage routing", "provider roles", "gates"], {"stage routing"}, False),
            ("Skill router", ["SKILL.md", "tool surface", "services"], {"tool surface"}, True),
            ("Artefacts", ["manifests", "reports", "evidence"], set(), False),
        ],
        x0,
        y,
        w,
        h,
        gap,
        accent_arrows={1},
    )
    step_y, step_w, step_h, step_gap = 380, 124, 74, 8
    steps = ["Intake", "Sources", "Reconstruct", "Segment", "Materials", "Texture", "Physics", "Nonvisual", "Verify", "Govern"]
    step_x = (1360 - (len(steps) * step_w + (len(steps) - 1) * step_gap)) / 2
    for i, step in enumerate(steps):
        x = step_x + i * (step_w + step_gap)
        lines.extend(outer_box(x, step_y, step_w, step_h, step, [], hot=step in {"Reconstruct", "Verify"}, title_size=16))
        if i < len(steps) - 1:
            lines.append(arrow(x + step_w, step_y + step_h / 2, x + step_w + step_gap - 4, step_y + step_h / 2))
    router_cx = x0 + 2 * (w + gap) + w / 2
    texture_cx = step_x + 5 * (step_w + step_gap) + step_w / 2
    lines.append(elbow([(router_cx, y + h), (router_cx, 334), (texture_cx, 334), (texture_cx, step_y)], accent=True))


def draw_agentic_loop(lines: list[str]) -> None:
    x0, y, w, h, gap = 52, 100, 236, 210, 24
    flow_row(
        lines,
        [
            ("Stage artefacts", ["manifests", "renders", "evidence"], set(), False),
            ("Formal gates", ["schemas", "lineage", "domain checks"], set(), False),
            ("VLM review", ["stage rubric", "defect tags", "verdict"], {"stage rubric", "defect tags"}, True),
            ("Sign-off", ["approved", "recorded verdict"], set(), False),
            ("Progress", ["progress.json", "contact sheet"], {"contact sheet"}, False),
        ],
        x0,
        y,
        w,
        h,
        gap,
        accent_arrows={1, 2},
    )
    fix_x, fix_y, fix_w, fix_h = 260, 400, 400, 150
    lines.extend(outer_box(fix_x, fix_y, fix_w, fix_h, "Fix library", ["symptom to recipe", "bounded attempts", "re-verify"], {"symptom to recipe"}, hot=True))
    esc_x, esc_y, esc_w, esc_h = 760, 400, 340, 150
    lines.extend(outer_box(esc_x, esc_y, esc_w, esc_h, "Escalate", ["operator review", "findings attached"], set(), hot=False))
    review_cx = x0 + 2 * (w + gap) + w / 2
    lines.append(elbow([(review_cx - 40, y + h), (review_cx - 40, fix_y - 25), (fix_x + fix_w / 2, fix_y - 25), (fix_x + fix_w / 2, fix_y)], accent=True))
    lines.append(elbow([(fix_x, fix_y + fix_h / 2), (x0 + w / 2, fix_y + fix_h / 2), (x0 + w / 2, y + h)], accent=True))
    lines.append(elbow([(review_cx + 40, y + h), (review_cx + 40, esc_y - 25), (esc_x + esc_w / 2, esc_y - 25), (esc_x + esc_w / 2, esc_y)]))


def draw_execution_lanes(lines: list[str]) -> None:
    selector_x, selector_y, selector_w, selector_h = 76, 214, 250, 180
    lines.extend(outer_box(selector_x, selector_y, selector_w, selector_h, "Lane selector", ["workload class", "GPU needs", "cost envelope"], {"workload class"}, hot=True))
    lanes = [
        (468, 40, 158, "Local CLI", ["dry runs", "packaging"]),
        (468, 226, 158, "GPU runners", ["reconstruction", "texture generation"]),
        (468, 412, 158, "WSL Docker", ["containerised paths"]),
        (904, 132, 158, "Isaac runtime", ["load checks", "physics gates"]),
        (904, 330, 190, "Storage roots", ["projects", "caches", "downloads"]),
    ]
    lane_trunk_x = 394
    for x, y, lane_h, title, items in lanes:
        lines.extend(outer_box(x, y, 310, lane_h, title, items, {items[0]}, hot=title == "GPU runners"))
        if x == 468:
            lane_y = y + lane_h / 2
            lines.append(elbow([(selector_x + selector_w, selector_y + selector_h / 2), (lane_trunk_x, selector_y + selector_h / 2), (lane_trunk_x, lane_y), (x, lane_y)], accent=title == "GPU runners"))
    lines.append(elbow([(778, 305), (842, 305), (842, 211), (904, 211)], accent=True))
    lines.append(elbow([(778, 491), (842, 491), (842, 409), (904, 409)]))


def draw_library_grounding(lines: list[str]) -> None:
    lines.extend(outer_box(56, 96, 250, 200, "Operator backings", ["material folders", "USD assets", "Omniverse estate"], set(), hot=False))
    lines.extend(outer_box(56, 336, 250, 180, "Remote sources", ["ambientCG", "Poly Haven", "vMaterials"], set(), hot=False))
    lines.extend(outer_box(392, 210, 252, 200, "Indexes", ["local scans", "curated seeds", "download cache"], {"curated seeds"}, hot=True))
    lines.append(elbow([(306, 196), (350, 196), (350, 280), (392, 280)]))
    lines.append(elbow([(306, 426), (350, 426), (350, 340), (392, 340)]))
    routes = [
        (740, 60, 150, "Knowledge", ["USD, PhysX", "PBR and MDL", "validation"]),
        (740, 244, 136, "Exemplars", ["materials", "asset packs"]),
        (740, 414, 150, "Properties", ["density, friction", "thermal ranges"]),
    ]
    for x, y, rh, title, items in routes:
        lines.extend(outer_box(x, y, 274, rh, title, items, {items[0]}, hot=title == "Exemplars"))
        route_y = y + rh / 2
        lines.append(elbow([(644, 310), (696, 310), (696, route_y), (x, route_y)], accent=title == "Exemplars"))
    lines.extend(outer_box(1090, 216, 240, 190, "Grounded proposals", ["citations", "review states", "no invented prims"], {"no invented prims"}, hot=True, title_size=18))
    for _, y, rh, title, _ in routes:
        route_y = y + rh / 2
        lines.append(elbow([(1014, route_y), (1056, route_y), (1056, 311), (1090, 311)], accent=title == "Exemplars"))
    lines.append(elbow([(392, 260), (368, 260), (368, 32), (1210, 32), (1210, 216)]))
    lines.append('  <text class="small" x="788" y="24" text-anchor="middle">USD Search over the estate joins search results when configured</text>')


def draw_source_ingestion_lineage(lines: list[str]) -> None:
    x0, y, w, h, gap = 40, 100, 240, 240, 20
    flow_row(
        lines,
        [
            ("Programme intake", ["task + simulator", "evidence needs"], set(), False),
            ("Original source", ["path metadata", "original checksum"], set(), False),
            ("Immutable copy", ["source-assets/", "copy checksum"], {"source-assets/"}, True),
            ("Source record", ["rights + retention", "units + axis"], set(), False),
            ("Route decision", ["downstream stages", "block if incomplete"], set(), False),
        ],
        x0,
        y,
        w,
        h,
        gap,
        accent_arrows={1, 3},
    )
    lines.extend(
        outer_box(
            430,
            420,
            500,
            130,
            "Lineage carried forward",
            ["checksums + ids", "rights + units"],
            {"checksums + ids"},
            hot=True,
        )
    )
    source_record_cx = x0 + 3 * (w + gap) + w / 2
    lines.append(elbow([(source_record_cx, y + h), (source_record_cx, 385), (680, 385), (680, 420)], accent=True))


def draw_orchestrator_routing(lines: list[str]) -> None:
    lines.extend(outer_box(40, 205, 240, 190, "Run request", ["source types", "requested outputs", "constraints"], set(), hot=False))
    lines.extend(
        outer_box(
            360,
            180,
            280,
            230,
            "Dependency closure",
            ["stage contracts", "inputs + outputs", "policy + capacity"],
            {"stage contracts"},
            hot=True,
        )
    )
    branches = [
        (730, 30, "Asset sources", ["CAD / USD / visual", "reconstruction", "segment + material"]),
        (730, 220, "Robot sources", ["URDF / MJCF / XML", "reconstruction", "physics + closure"]),
        (730, 410, "Requested outputs", ["texture / nonvisual", "SimReady / RL / eval", "target + closure"]),
    ]
    for x, y, title, items in branches:
        lines.extend(outer_box(x, y, 310, 160, title, items, set(), hot=False))
    lines.extend(
        outer_box(
            1120,
            170,
            200,
            260,
            "Run plan",
            ["stages + skips", "providers + bounds", "gates + blockers", "attempt paths"],
            {"stages + skips"},
            hot=True,
            title_size=18,
        )
    )
    lines.append(arrow(280, 300, 356, 300, accent=True))
    lines.append(trunk([(640, 295), (685, 295), (685, 110), (685, 490)]))
    lines.append(trunk([(1080, 110), (1080, 490)]))
    for _, branch_y, title, _ in branches:
        branch_cy = branch_y + 80
        is_output = title == "Requested outputs"
        lines.append(arrow(685, branch_cy, 726, branch_cy, accent=is_output))
        lines.append(trunk([(1040, branch_cy), (1080, branch_cy)], accent=is_output))
        lines.append(junction(685, branch_cy, accent=is_output))
        lines.append(junction(1080, branch_cy, accent=is_output))
    lines.append(arrow(1080, 300, 1116, 300, accent=True))
    lines.extend(
        outer_box(
            360,
            450,
            280,
            130,
            "Always selected",
            ["orchestrate + intake", "evaluate + govern", "infrastructure"],
            set(),
            hot=False,
            title_size=18,
        )
    )
    lines.append(arrow(500, 410, 500, 451))


def draw_record_graph(lines: list[str]) -> None:
    lines.extend(outer_box(50, 70, 230, 160, "Run request + plan", ["request digest", "selected stages"], set(), hot=False, title_size=18))
    lines.extend(outer_box(50, 370, 230, 160, "Stage attempts", ["immutable history", "I/O digests"], set(), hot=False, title_size=18))
    lines.extend(outer_box(380, 70, 250, 180, "Stage manifests", ["stable ids", "upstream records", "promotion states"], set(), hot=False))
    lines.extend(outer_box(380, 360, 250, 180, "Evidence + reports", ["checksums", "validator findings", "runtime + task"], set(), hot=False, title_size=18))
    lines.extend(
        outer_box(
            720,
            190,
            260,
            220,
            "Record graph",
            ["identity links", "digest binding", "selected run", "path closure"],
            {"identity links", "digest binding"},
            hot=True,
        )
    )
    lines.extend(outer_box(1060, 90, 250, 180, "Governance", ["rights + decisions", "current bindings", "no blockers"], set(), hot=False))
    lines.extend(outer_box(1060, 360, 250, 180, "Release + capsule", ["promotion record", "portable evidence", "reproduction"], set(), hot=False, title_size=18))
    lines.append(arrow(280, 150, 376, 150))
    lines.append(arrow(165, 230, 165, 366))
    lines.append(elbow([(280, 450), (330, 450), (330, 210), (380, 210)]))
    lines.append(arrow(280, 450, 376, 450))
    lines.append(elbow([(630, 160), (675, 160), (675, 260), (716, 260)], accent=True))
    lines.append(elbow([(630, 450), (675, 450), (675, 340), (716, 340)], accent=True))
    lines.append(elbow([(980, 260), (1020, 260), (1020, 180), (1056, 180)], accent=True))
    lines.append(arrow(1185, 270, 1185, 356, accent=True))


def draw_usd_layer_ownership(lines: list[str]) -> None:
    lines.extend(outer_box(500, 35, 360, 130, "<asset-id>.usda", ["composed root", "owned layer refs"], {"composed root"}, hot=True))
    layers = [
        ("geo.usda", ["geometry", "reconstruction"]),
        ("mtl.usda", ["materials + maps", "material stages"]),
        ("phy.usda", ["bodies + colliders", "physics"]),
        ("art.usda", ["joints + drives", "physics"]),
        ("sem.usda", ["labels + grasps", "segmentation"]),
        ("deform.usda", ["optional layer", "deformation"]),
        ("variants.usda", ["variant opinions", "permutation"]),
        ("contents.usda", ["geo assembly", "source policy"]),
    ]
    x0, y, w, h, gap = 30, 235, 150, 230, 15
    root_cx = 680
    first_cx = x0 + w / 2
    last_cx = x0 + (len(layers) - 1) * (w + gap) + w / 2
    lines.append(trunk([(root_cx, 165), (root_cx, 205), (first_cx, 205), (last_cx, 205)], accent=True))
    for i, (title, items) in enumerate(layers):
        x = x0 + i * (w + gap)
        is_variant = title == "variants.usda"
        lines.extend(outer_box(x, y, w, h, title, items, set(), hot=is_variant, title_size=16))
        lines.append(arrow(x + w / 2, 205, x + w / 2, 231, accent=is_variant))
        lines.append(junction(x + w / 2, 205, accent=is_variant))


def draw_governance_release_decision(lines: list[str]) -> None:
    inputs = [
        ("Source rights", ["allowed use", "retention current"]),
        ("Technical gates", ["graph + package", "runtime gate"]),
        ("Task evidence", ["approved protocol", "measured fitness"]),
        ("Operator approval", ["scope bound", "not expired"]),
    ]
    x0, y, w, h, gap = 40, 70, 200, 180, 30
    for i, (title, items) in enumerate(inputs):
        x = x0 + i * (w + gap)
        lines.extend(outer_box(x, y, w, h, title, items, set(), hot=False, title_size=18))
        lines.append(trunk([(x + w / 2, y + h), (x + w / 2, 300)], accent=title == "Technical gates"))
        lines.append(junction(x + w / 2, 300, accent=title == "Technical gates"))
    lines.append(trunk([(x0 + w / 2, 300), (960, 300)]))
    lines.extend(outer_box(970, 190, 80, 80, "AND", [], hot=True, title_size=16))
    lines.append(elbow([(960, 300), (960, 230), (966, 230)], accent=True))
    lines.extend(
        outer_box(
            1100,
            80,
            220,
            230,
            "Release decision",
            ["evidence current", "bindings match", "no blockers"],
            {"no blockers"},
            hot=True,
            title_size=18,
        )
    )
    lines.append(arrow(1050, 230, 1096, 230, accent=True))
    lines.extend(outer_box(50, 430, 280, 120, "Review-required claim", ["remains a proposal", "operator decision"], set(), hot=False, title_size=18))
    lines.append(elbow([(190, 430), (190, 380), (830, 380), (830, 254)], accent=True))
    lines.extend(outer_box(430, 430, 250, 120, "Approved", ["released: true"], set(), hot=True, title_size=18))
    lines.extend(outer_box(730, 430, 250, 120, "Blocked", ["named conditions"], set(), hot=False, title_size=18))
    lines.extend(outer_box(1110, 340, 200, 70, "One outcome", [], hot=False, title_size=16))
    lines.append(arrow(1210, 310, 1210, 336, accent=True))
    lines.append(trunk([(1210, 410), (1210, 415), (555, 415)]))
    lines.append(arrow(555, 415, 555, 426, accent=True))
    lines.append(arrow(855, 415, 855, 426))
    lines.append(junction(555, 415, accent=True))
    lines.append(junction(855, 415))
    lines.extend(outer_box(1050, 445, 280, 120, "Re-evaluate on change", ["run / package", "Profile / scope", "expiry"], set(), hot=False, title_size=17))
    lines.append(elbow([(1190, 445), (1340, 445), (1340, 45), (1010, 45), (1010, 186)], accent=True))


def draw_tool_service_authorisation(lines: list[str]) -> None:
    lines.extend(outer_box(40, 170, 210, 210, "Client", ["bearer token", "tool + params"], set(), hot=False))
    lines.extend(outer_box(310, 170, 210, 210, "Authentication", ["caller identity", "network policy"], set(), hot=False, title_size=18))
    lines.extend(outer_box(590, 145, 250, 260, "Tool service", ["allowlist + schema", "request bounds", "job state"], {"allowlist + schema"}, hot=True))
    lines.extend(outer_box(910, 170, 180, 210, "Worker", ["bounded attempt", "no silent resume"], set(), hot=False))
    lines.extend(outer_box(1160, 170, 160, 210, "Artefacts", ["manifests", "reports", "evidence"], set(), hot=False, title_size=17))
    lines.append(arrow(250, 275, 306, 275))
    lines.append(arrow(520, 275, 586, 275))
    lines.append(arrow(840, 275, 906, 275, accent=True))
    lines.append(arrow(1090, 275, 1156, 275))
    lines.extend(outer_box(250, 455, 300, 120, "Approval issuer", ["independent secret", "one-use approval"], {"one-use approval"}, hot=True, title_size=18))
    lines.extend(outer_box(660, 455, 300, 120, "Durable ledger", ["state + retries", "used approvals"], set(), hot=False, title_size=18))
    lines.extend(outer_box(1060, 455, 260, 120, "Audit output", ["append-only events", "redacted metadata"], set(), hot=False, title_size=18))
    lines.append(elbow([(550, 515), (570, 515), (570, 345), (590, 345)], accent=True))
    lines.append(elbow([(715, 405), (715, 430), (810, 430), (810, 455)]))
    lines.append(elbow([(1000, 380), (1000, 430), (900, 430), (900, 455)]))
    lines.append(arrow(960, 515, 1056, 515))


def draw_capsule_trust_chain(lines: list[str]) -> None:
    x0, y, w, h, gap = 40, 80, 230, 240, 20
    flow_row(
        lines,
        [
            ("Selected run", ["attempts + package", "release decision"], set(), False),
            ("Publication gates", ["rights + licences", "clean source + BOM"], set(), False),
            ("Sanitise + materialise", ["relative paths", "portable records"], set(), False),
            ("Capsule closure", ["checksum inventory", "schemas + digests"], {"checksum inventory"}, True),
            ("Reproduce", ["tagged archive", "recomputed checks"], set(), False),
        ],
        x0,
        y,
        w,
        h,
        gap,
        accent_arrows={2, 3},
    )
    lines.extend(
        outer_box(
            450,
            425,
            460,
            130,
            "External release attestation",
            ["binds capsule.json", "signed externally"],
            {"binds capsule.json"},
            hot=True,
        )
    )
    reproduce_cx = x0 + 4 * (w + gap) + w / 2
    lines.append(elbow([(reproduce_cx, y + h), (reproduce_cx, 385), (680, 385), (680, 425)], accent=True))


def draw_partial_invocation_convergence(lines: list[str]) -> None:
    lines.extend(outer_box(45, 70, 230, 150, "CLI", ["afb stage run", "project or request"], set(), hot=False))
    lines.extend(outer_box(45, 350, 230, 150, "Agent tool", ["asset_stage_run", "stage-invoker"], set(), hot=False))
    lines.extend(
        outer_box(
            360,
            185,
            270,
            220,
            "Same stage service",
            ["one contract", "same rubric", "same gates"],
            {"one contract"},
            hot=True,
        )
    )
    lines.append(elbow([(275, 145), (315, 145), (315, 250), (356, 250)], accent=True))
    lines.append(elbow([(275, 425), (315, 425), (315, 340), (356, 340)], accent=True))
    stages = [
        (700, "Refresh", ["stage artefacts", "manifest inputs"]),
        (925, "VLM review", ["verdict", "bounded fixes"]),
        (1150, "Durable state", ["stage-run report", "progress + hashes"]),
    ]
    for x, title, items in stages:
        lines.extend(outer_box(x, 175, 185, 240, title, items, set(), hot=title == "VLM review", title_size=17))
    lines.append(arrow(630, 295, 696, 295))
    lines.append(arrow(885, 295, 921, 295, accent=True))
    lines.append(arrow(1110, 295, 1146, 295, accent=True))
    lines.extend(inner_box(830, 465, 170, 30, "bounded fix", accent=True))
    lines.append(elbow([(1018, 415), (1018, 525), (792, 525), (792, 415)], accent=True))


def draw_runtime_layer_contract(lines: list[str]) -> None:
    x0, y, w, h, gap = 120, 170, 250, 230, 35
    flow_row(
        lines,
        [
            ("CLI + public tools", ["typed inputs", "preconditions"], set(), False),
            ("Schema validation", ["Pydantic runtime", "JSON Schema files"], set(), False),
            ("Services", ["state + mutation", "one service / tool"], {"one service / tool"}, True),
            ("Project + runtimes", ["durable artefacts", "USD / Isaac / CUDA"], set(), False),
        ],
        x0,
        y,
        w,
        h,
        gap,
        accent_arrows={1, 2},
    )
    lines.extend(outer_box(250, 465, 340, 115, "Prompts + skills", ["editable guidance", "stage playbooks"], set(), hot=False, title_size=18))
    lines.extend(outer_box(770, 465, 340, 115, "Utilities", ["pure helpers", "explicit I/O"], set(), hot=False, title_size=18))
    lines.append(elbow([(420, 465), (420, 435), (245, 435), (245, 400)]))
    services_cx = x0 + 2 * (w + gap) + w / 2
    lines.append(elbow([(940, 465), (940, 435), (services_cx, 435), (services_cx, 400)], accent=True))


DIAGRAMS = [
    {"name": "architecture", "title": "Asset factory architecture", "desc": "Five blocks from immutable sources through staged proposals, validation and release, with agent control looping under the stages.", "draw": draw_architecture},
    {"name": "asset-factory-pipeline", "title": "The seven-stage pipeline", "desc": "Pre-pipeline intake feeds seven content stages into downstream extensions; every stage passes formal gates and a VLM sign-off.", "draw": draw_pipeline},
    {"name": "source-ingestion-lineage", "title": "Source ingestion lineage", "desc": "Programme intent and original source metadata become an immutable project copy, source record and explicit downstream route while checksums, rights and unit policy carry forward.", "draw": draw_source_ingestion_lineage},
    {"name": "image-to-usd-storyboard", "title": "Image to USD storyboard", "desc": "Photos and words move through priors, generative backends, the USD layer stack and review into a governed package.", "draw": draw_storyboard},
    {"name": "segmentation-lane", "title": "Segmentation lane", "desc": "Inputs pass through segmentation priors into stable segments, semantic conditioning and the segmentation manifest.", "draw": draw_segmentation_lane},
    {"name": "material-inference-lane", "title": "Material and physical inference lane", "desc": "Evidence is grounded in library indexes before material candidates and review-gated physical proposals are recorded.", "draw": draw_material_lane},
    {"name": "texturing-lane", "title": "Texturing lane", "desc": "Material manifests drive prompting, PBR generation, decal placement and consistency checks, fed by indexed sources.", "draw": draw_texturing_lane},
    {"name": "texture-physics-consistency-lane", "title": "Texture and physics consistency lane", "desc": "Visible cues, material classes and property claims pass a contradiction check before a review state is recorded.", "draw": draw_consistency_lane, "height": 480},
    {"name": "physics-articulation-lane", "title": "Physics and articulation lane", "desc": "Validated proposals become bodies, joints, grasp affordances and validation scenarios in the physics and articulation layers.", "draw": draw_physics_lane},
    {"name": "nonvisual-materials-lane", "title": "Nonvisual materials lane", "desc": "Evidence-ranked thermal, acoustic and electrical values carry units, bounds and review states into the manifest.", "draw": draw_nonvisual_lane, "height": 480},
    {"name": "simready-verification-gates", "title": "SimReady verification gates", "desc": "Layer stack, package, runtime load and performance checks feed the promotion decision, with a final visual review.", "draw": draw_simready_gates},
    {"name": "rl-environment-loop", "title": "RL environment loop", "desc": "Validated assets become task contracts, rewards and training runs whose policy evidence loops back into asset work.", "draw": draw_rl_loop},
    {"name": "library-grounding", "title": "Library grounding", "desc": "Operator backings and remote sources become indexes of knowledge, exemplars and properties that ground every proposal.", "draw": draw_library_grounding},
    {"name": "agentic-loop", "title": "Agentic loop", "desc": "Stage artefacts pass formal gates and VLM review; revise verdicts route through the fix library or escalate to operators.", "draw": draw_agentic_loop},
    {"name": "agent-workflow", "title": "Agent workflow", "desc": "Run requests route through the orchestrator and skill router into artefacts, across the ordered stage strip.", "draw": draw_agent_workflow},
    {"name": "execution-lanes", "title": "Execution lanes", "desc": "A lane selector maps workload classes to local CLI, GPU runners, containers, the Isaac runtime and storage roots.", "draw": draw_execution_lanes},
    {"name": "orchestrator-routing", "title": "Orchestrator routing", "desc": "A run request is closed over stage contracts and policies, split into source and output routes, then recorded as a dependency-complete run plan with gates and stop reasons.", "draw": draw_orchestrator_routing},
    {"name": "record-graph", "title": "Record graph", "desc": "Run identity, immutable attempts, stage manifests and durable evidence converge on cross-record validation before governance can promote a release or capsule.", "draw": draw_record_graph},
    {"name": "usd-layer-ownership", "title": "USD layer ownership", "desc": "The composed asset root references eight separately owned layer families so geometry, appearance, physics, articulation, semantics, optional deformation and controlled variants remain attributable.", "draw": draw_usd_layer_ownership, "height": 570},
    {"name": "governance-release-decision", "title": "Governance release decision", "desc": "Current rights, technical gates, task-fitness evidence and a bound operator decision form an AND gate whose result is approved or blocked and is invalidated by any run, package, Profile, scope or expiry change.", "draw": draw_governance_release_decision},
    {"name": "tool-service-authorisation", "title": "Tool service authorisation", "desc": "Caller authentication and an independently issued single-use mutation capability meet at the bounded tool service, with durable jobs, approval consumption and audit output.", "draw": draw_tool_service_authorisation},
    {"name": "capsule-trust-chain", "title": "Reference capsule trust chain", "desc": "A selected run passes publication gates, sanitisation and checksum closure before reproduction, while an external signed attestation binds the immutable capsule digest.", "draw": draw_capsule_trust_chain},
    {"name": "partial-invocation-convergence", "title": "Direct partial invocation", "desc": "CLI and agent entry paths converge on the same stage service, artefact refresh, VLM review loop and durable state updates.", "draw": draw_partial_invocation_convergence},
    {"name": "runtime-layer-contract", "title": "Runtime layer contract", "desc": "Public tools pass typed inputs through schema validation into state-owning services and bounded runtime integrations, supported by prompts, skills and pure utilities.", "draw": draw_runtime_layer_contract},
]


def find_chromium() -> str:
    configured = os.environ.get("AFB_CHROMIUM")
    if configured:
        return configured
    for candidate in ("chromium-browser", "chromium", "google-chrome", "google-chrome-stable", "chrome", "msedge"):
        path = shutil.which(candidate)
        if path:
            return path
    raise SystemExit("No Chromium executable found. Set AFB_CHROMIUM=/path/to/chromium and retry.")


def export_pngs(output_dir: Path) -> None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise SystemExit("PNG export requires the optional Python package 'playwright'.") from exc
    chromium = find_chromium()
    output_dir.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, executable_path=chromium)
        for svg_path in sorted(ASSETS.glob("*.svg")):
            svg = svg_path.read_text(encoding="utf-8")
            root = ET.fromstring(svg)
            width = int(float(root.attrib["width"]))
            height = int(float(root.attrib["height"]))
            page = browser.new_page(viewport={"width": width, "height": height}, device_scale_factor=1)
            page.set_content(
                "<!doctype html><html><head><style>"
                f"html,body{{margin:0;padding:0;background:transparent;width:{width}px;height:{height}px;overflow:hidden;}}"
                "svg{display:block;}"
                "</style></head><body>" + svg + "</body></html>"
            )
            page.screenshot(path=str(output_dir / f"{svg_path.stem}.png"), omit_background=True)
            page.close()
        browser.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate asset factory blueprint diagrams.")
    parser.add_argument("--check", action="store_true", help="fail when committed figures drift from the generator")
    parser.add_argument("--png", action="store_true", help="also export transparent PNGs from the generated SVGs")
    parser.add_argument("--png-dir", type=Path, default=DEFAULT_PNG_DIR, help="directory for PNG exports")
    args = parser.parse_args(argv)

    ASSETS.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []
    for diagram in DIAGRAMS:
        expected = render(diagram)
        target = ASSETS / f"{diagram['name']}.svg"
        if args.check:
            if not target.exists() or target.read_text(encoding="utf-8") != expected:
                errors.append(str(target.relative_to(ROOT)))
        else:
            target.write_text(expected, encoding="utf-8")
    if errors:
        print("diagram drift: " + ", ".join(errors))
        return 1
    if args.check:
        print("diagram check passed")
    if args.png:
        export_pngs(args.png_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
