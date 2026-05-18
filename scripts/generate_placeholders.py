#!/usr/bin/env python3
"""Generate placeholder ``thumb.svg`` (16:9) and ``primary.svg`` (1:1)
artwork for every studio whose ``studio.json`` has ``placeholder: true``.

For each such studio the script:

  1. Writes ``thumb.svg`` (1024x576) and ``primary.svg`` (1024x1024). By
     default existing files are kept; pass ``--override`` to rewrite
     every placeholder regardless of whether the files already exist.
  2. Deletes every other artwork file in the studio directory
     (``logo.svg``, ``backdrop.svg``, any ``*.webp``, …).
     ``logo.svg`` is reserved for a real studio logo; a placeholder
     studio has no real logo, so the file would be misleading.
  3. Normalises the entry's ``artwork`` block to
     ``{"thumb": ["svg", "webp"], "primary": ["svg", "webp"]}``
     so the manifest matches the files on disk.

Promotion pass: studios that aren't flagged ``placeholder: true`` but
whose ``logo.svg`` is actually one of our placeholder renders (matched
by the ``PLACEHOLDER`` marker we always emit) get flipped to
``placeholder: true`` before the steps above run. Their fake ``logo.svg``
is removed and they're processed like any other placeholder studio.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STUDIOS = ROOT / "studios"
MAX_DIM = 1024

# Slot specs. Each placeholder studio gets one file per slot. ``slot`` is
# the manifest key; ``file`` is the on-disk basename; (w, h) is the
# canonical canvas. We always emit both — the build pipeline expects
# both a 16:9 thumb and a 1:1 primary, and the consumer plugin picks
# whichever the UI surface needs.
SLOTS = (
    {"slot": "thumb",   "file": "thumb.svg",   "w": 1024, "h": 576},
    {"slot": "primary", "file": "primary.svg", "w": 1024, "h": 1024},
)
KEEP = {s["file"] for s in SLOTS}


def font_size_for(name: str, canvas_w: int = 1024) -> int:
    """Clamp the font-size so long names fit inside ~85% of the canvas
    width. canvas_w defaults to the standard 1024-wide canvas."""
    chars = max(1, len(name))
    base = int(900 * canvas_w / 1024 / chars)
    return max(26, min(72, base))


def caption_font_size() -> int:
    return 22


def fmt(n: float) -> str:
    if abs(n - round(n)) < 1e-9:
        return str(int(round(n)))
    return f"{n:.3f}".rstrip("0").rstrip(".")


def escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def svg_for(name: str, vb_w: int, vb_h: int) -> str:
    inset = 16
    stroke = 4
    dash = 12
    name_fs = font_size_for(name, vb_w)
    cap_fs = caption_font_size()
    cx = vb_w / 2
    name_y = vb_h * 0.5
    cap_y = vb_h - 60
    family = ("-apple-system,Segoe UI,Roboto,Helvetica,"
              "Arial,sans-serif")

    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'viewBox="0 0 {vb_w} {vb_h}" width="{vb_w}" height="{vb_h}" '
        f'role="img" aria-label="{escape(name)} (placeholder)">\n'
        f'  <rect width="{vb_w}" height="{vb_h}" fill="#1f1f1f"/>\n'
        f'  <rect x="{inset}" y="{inset}" '
        f'width="{vb_w - 2 * inset}" height="{vb_h - 2 * inset}" '
        f'fill="none" stroke="#3a3a3a" stroke-width="{stroke}" '
        f'stroke-dasharray="{dash} {dash}"/>\n'
        f'  <text x="{fmt(cx)}" y="{fmt(name_y)}" text-anchor="middle" '
        f'dominant-baseline="middle" font-family="{family}" '
        f'font-size="{name_fs}" font-weight="600" fill="#e0e0e0">'
        f'{escape(name)}</text>\n'
        f'  <text x="{fmt(cx)}" y="{fmt(cap_y)}" text-anchor="middle" '
        f'font-family="{family}" font-size="{cap_fs}" fill="#888" '
        f'letter-spacing="4">PLACEHOLDER</text>\n'
        f'</svg>\n'
    )


ARTWORK_EXTS = {".svg", ".webp", ".png", ".jpg", ".jpeg"}


def remove_stale_artwork(studio_dir: Path, removed: list[str]) -> None:
    for child in studio_dir.iterdir():
        if not child.is_file():
            continue
        if child.name == "studio.json" or child.name in KEEP:
            continue
        if child.suffix.lower() in ARTWORK_EXTS:
            removed.append(str(child.relative_to(ROOT)))
            child.unlink()


def normalize_artwork(entry: dict) -> bool:
    """Force a placeholder entry's artwork block to advertise one
    ``["svg", "webp"]`` pair per slot we emit.

    The build pipeline renders a .webp sibling for every .svg it ships,
    so the manifest must advertise both formats. Setting svg-only here
    used to fight ``build_release.lint_manifests``, which re-adds webp
    on every build and dirties thousands of studio.json files per run.

    Returns True if anything changed."""
    desired = {s["slot"]: ["svg", "webp"] for s in SLOTS}
    if entry.get("artwork") == desired:
        return False
    entry["artwork"] = desired
    return True


# Markers we always include in the placeholder SVGs we emit. A logo.svg
# matching any of these is one of our own placeholder renders, not a
# real studio logo. Used to detect studios that should be flipped to
# placeholder=true.
PLACEHOLDER_MARKERS = (
    ">PLACEHOLDER<",
    "(placeholder logo)",
    "(placeholder)",
)


def is_placeholder_svg(path: Path) -> bool:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    return any(m in text for m in PLACEHOLDER_MARKERS)


def promote_if_placeholder_logo(slug: str, data: list, studio_dir: Path) -> bool:
    """If the studio's logo.svg is one of our placeholder renders, set
    ``placeholder: true`` on every entry that isn't already flagged.
    Returns True if anything was changed."""
    logo = studio_dir / "logo.svg"
    if not logo.exists() or not is_placeholder_svg(logo):
        return False
    changed = False
    for entry in data:
        if isinstance(entry, dict) and not entry.get("placeholder"):
            entry["placeholder"] = True
            changed = True
    return changed


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "--override",
        action="store_true",
        help=("Rewrite every placeholder slot (thumb.svg + primary.svg) "
              "for every placeholder studio, even if files already exist. "
              "Without this flag, existing files are left untouched."),
    )
    args = ap.parse_args()

    if not STUDIOS.is_dir():
        sys.exit(f"missing directory: {STUDIOS}")

    studios_done = 0
    skipped = 0
    written: list[str] = []
    kept = 0
    removed: list[str] = []
    json_updated = 0
    promoted = 0
    errors = 0

    for studio_file in sorted(STUDIOS.glob("*/studio.json")):
        slug = studio_file.parent.name
        try:
            data = json.loads(studio_file.read_text())
        except Exception as e:
            errors += 1
            print(f"  err {slug}/studio.json: {e}", flush=True)
            continue
        if not isinstance(data, list):
            continue

        # Promotion pass: studios with a placeholder-shaped logo.svg get
        # flipped to placeholder=true so the rest of the pipeline
        # processes them.
        if promote_if_placeholder_logo(slug, data, studio_file.parent):
            promoted += 1
            print(f"  promote {slug}: logo.svg is a placeholder", flush=True)

        placeholder_entries = [
            e for e in data if isinstance(e, dict) and e.get("placeholder")
        ]
        if not placeholder_entries:
            skipped += 1
            continue
        # Only auto-clean directories whose every entry is a placeholder
        # — otherwise we'd risk deleting a real entry's artwork.
        if len(placeholder_entries) != len(
            [e for e in data if isinstance(e, dict)]
        ):
            print(
                f"  skip {slug}: mixed placeholder/non-placeholder entries",
                flush=True,
            )
            skipped += 1
            continue

        studio_dir = studio_file.parent
        name = placeholder_entries[0].get("name") or slug

        # 1) Remove stale artwork (logo.svg, backdrop.svg, *.webp, …)
        remove_stale_artwork(studio_dir, removed)

        # 2) Write a fresh placeholder for each slot. Without --override,
        # skip slots whose file already exists so prior renders aren't
        # disturbed.
        for spec in SLOTS:
            out = studio_dir / spec["file"]
            if args.override or not out.exists():
                out.write_text(
                    svg_for(name, spec["w"], spec["h"]), encoding="utf-8",
                )
                written.append(str(out.relative_to(ROOT)))
            else:
                kept += 1

        # 3) Normalise the manifest so artwork matches the files on disk
        changed = False
        for entry in placeholder_entries:
            if normalize_artwork(entry):
                changed = True
        if changed:
            new_text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
            studio_file.write_text(new_text)
            json_updated += 1

        studios_done += 1

    print(f"placeholder studios processed:   {studios_done}")
    print(f"non-placeholder studios skipped: {skipped}")
    print(f"promoted from placeholder logo:  {promoted}")
    print(f"placeholder files written:       {len(written)}")
    print(f"placeholder files kept as-is:    {kept}")
    print(f"stale artwork files removed:     {len(removed)}")
    print(f"studio.json files updated:       {json_updated}")
    if errors:
        print(f"errors: {errors}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
