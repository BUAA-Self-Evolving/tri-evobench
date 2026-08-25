"""Repair the known-good browser oracle/verifier compatibility issue in l2_demo.

The materialized L2 package intentionally lives outside this repository. The
two browser streams were written against an older Chromium/Playwright behavior:
``inner_text()`` can be empty for otherwise rendered table/legend content, and
hovering an SVG label with ``pointer-events: none`` does not dispatch the
bubble's tooltip handler. This tool updates only the private known-good oracle
and its private verifier, preserving the deliberately-broken control.
"""

from __future__ import annotations

import argparse
from pathlib import Path


STREAMS = (
    "skilllearn_stock_data_visualization_001",
    "skillsbench_data_to_d3_001",
)

ORACLE_REPLACEMENTS = (
    (
        ".force('collision', d3.forceCollide().radius(d => d.radius + 2))",
        ".force('collision', d3.forceCollide().radius(d => d.radius + 6).iterations(4))",
    ),
)

VERIFIER_REPLACEMENTS = (
    (
        "first_row_text = all_rows.first.inner_text().lower()",
        "first_row_text = (all_rows.first.text_content() or '').lower()",
    ),
    (
        "table_text = table.inner_text().lower()",
        "table_text = (table.text_content() or '').lower()",
    ),
    (
        "svg_texts = page.locator('svg text')",
        "svg_texts = page.locator('svg text')\n    bubbles = page.locator('svg circle')",
    ),
    (
        """if text_content == text_to_hover:
                box = text_elem.bounding_box()
                if box:
                    page.mouse.move(box['x'] + box['width'] / 2, box['y'] + box['height'] / 2)
                    page.wait_for_timeout(400)
                    return True""",
        """if text_content == text_to_hover and i < bubbles.count():
                bubbles.nth(i).hover()
                page.wait_for_timeout(400)
                return True""",
    ),
    (
        "tooltip_text = tooltip.first.inner_text()",
        "tooltip_text = tooltip.first.text_content() or ''",
    ),
    (
        "header_texts = [h.inner_text().lower() for h in headers.all()]",
        "header_texts = [(h.text_content() or '').lower() for h in headers.all()]",
    ),
    (
        "market_cap_text = cells.nth(market_cap_col_index).inner_text().strip()",
        "market_cap_text = (cells.nth(market_cap_col_index).text_content() or '').strip()",
    ),
    (
        "legend_text = legend.first.inner_text().lower()",
        "legend_text = (legend.first.text_content() or '').lower()",
    ),
)


def _repair(path: Path, replacements: tuple[tuple[str, str], ...], *, check: bool) -> bool:
    text = path.read_text(encoding="utf-8")
    changed = False
    for old, new in replacements:
        if new in text:
            continue
        if old not in text:
            raise ValueError(f"{path}: expected compatibility marker is missing: {old!r}")
        text = text.replace(old, new, 1)
        changed = True
    if changed and not check:
        path.write_text(text, encoding="utf-8")
    return changed


def repair(package: Path, *, check: bool) -> list[Path]:
    changed: list[Path] = []
    for stream_id in STREAMS:
        episodes = package / "streams" / stream_id / "episodes"
        if not episodes.is_dir():
            raise ValueError(f"missing browser stream: {episodes}")
        for episode in sorted(path for path in episodes.iterdir() if path.is_dir()):
            assets = episode / "evaluation_assets"
            oracle = assets / "oracle_solution.sh"
            verifier = assets / "native_test_outputs.py"
            if not oracle.is_file() or not verifier.is_file():
                raise ValueError(f"{episode}: browser evaluation assets are incomplete")
            if _repair(oracle, ORACLE_REPLACEMENTS, check=check):
                changed.append(oracle)
            if _repair(verifier, VERIFIER_REPLACEMENTS, check=check):
                changed.append(verifier)
    return changed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package", type=Path, help="path to an extracted l2_demo package")
    parser.add_argument("--check", action="store_true", help="validate that the repair is already present")
    args = parser.parse_args()
    changed = repair(args.package.resolve(), check=args.check)
    print("browser oracle repair: present" if args.check else f"browser oracle repair: updated {len(changed)} files")


if __name__ == "__main__":
    main()
