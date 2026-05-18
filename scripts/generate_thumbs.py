#!/usr/bin/env python3
"""Generate template-aligned thumb.svg (16:9) or primary.svg (1:1) from
existing logo.svg files for studios where ``template`` is ``false`` (or
absent) in ``studio.json``.

For each matching entry the script:
  1. Reads logo.svg's viewBox to size the embedded logo.
  2. Computes the logo's dominant ink colour(s) — area-weighted fills
     and strokes via ``svgelements``.
  3. Picks a background colour that hits WCAG AA contrast (>= 4.5:1)
     against those significant ink colours. Brand colours declared
     under ``colors[*].hex`` in studio.json are tried first, then
     white, then black. Highest minimum-contrast wins.
  4. Wraps the logo body inside a ``<g translate scale>`` that centres
     it within the template safe area on the chosen background.
  5. Updates studio.json — adds the new artwork slot (``svg`` + ``webp``
     so the build pipeline picks it up) and flips ``template`` to
     ``true`` so the entry is not re-processed on the next run.

Specs match ``templates/README.md`` and ``templates/{thumb,primary}.svg``.

Requires: ``pip install svgelements``.
"""
from __future__ import annotations

import argparse
import io
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

try:
    from svgelements import SVG, Shape
except ImportError:
    sys.exit("svgelements not installed. Install with: pip install svgelements")

try:
    from PIL import Image
except ImportError:
    Image = None  # type: ignore  # source-bg detection will degrade gracefully

ROOT = Path(__file__).resolve().parent.parent
STUDIOS = ROOT / "studios"

SPECS = {
    "16x9": {"w": 1024, "h": 576,  "inset": 80, "out": "thumb.svg",   "slot": "thumb"},
    "1x1":  {"w": 1024, "h": 1024, "inset": 96, "out": "primary.svg", "slot": "primary"},
}

# WCAG 2.1 thresholds
MIN_RATIO = 4.5   # AA — normal text. Hard floor for accepting a background.
GOOD_RATIO = 7.0  # AAA — preferred when achievable.


# ─── colour utilities ──────────────────────────────────────────────────

def srgb_to_linear(c: int) -> float:
    v = c / 255.0
    return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4


def relative_luminance(rgb: tuple[int, int, int]) -> float:
    r, g, b = rgb
    return (0.2126 * srgb_to_linear(r)
            + 0.7152 * srgb_to_linear(g)
            + 0.0722 * srgb_to_linear(b))


def contrast_ratio(a: tuple[int, int, int], b: tuple[int, int, int]) -> float:
    la, lb = relative_luminance(a), relative_luminance(b)
    lo, hi = (la, lb) if la <= lb else (lb, la)
    return (hi + 0.05) / (lo + 0.05)


HEX_RE = re.compile(r"^#?([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


def parse_hex(s: str) -> tuple[int, int, int] | None:
    m = HEX_RE.match(s.strip())
    if not m:
        return None
    h = m.group(1)
    if len(h) == 3:
        h = h[0] * 2 + h[1] * 2 + h[2] * 2
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def to_hex(rgb: tuple[int, int, int]) -> str:
    return "#" + "".join(f"{c:02X}" for c in rgb)


def paint_to_rgb(paint) -> tuple[int, int, int] | None:
    """Pull (r,g,b) from an svgelements paint. None for ``none``/``url(...)``."""
    if paint is None:
        return None
    if getattr(paint, "value", paint) is None:
        return None
    try:
        return int(paint.red), int(paint.green), int(paint.blue)
    except Exception:
        return None


# ─── logo introspection ────────────────────────────────────────────────

SVG_OPEN_RE = re.compile(r"<svg\b[^>]*>", re.DOTALL)
SVG_CLOSE_RE = re.compile(r"</svg\s*>", re.IGNORECASE)
VB_RE = re.compile(r'\bviewBox\s*=\s*["\']([^"\']+)["\']')

# Illustrator-exported SVGs frequently use a DOCTYPE ENTITY block to alias
# style strings, e.g. `<!ENTITY st0 "fill:#000;">` referenced as
# `style="&st0;"`. Stripping the DOCTYPE when we lift the <svg> body into
# our wrapper breaks those references, so we inline them at copy time.
ENTITY_DEF_RE = re.compile(
    r'<!ENTITY\s+([A-Za-z_][\w.-]*)\s+(?:"([^"]*)"|\'([^\']*)\')\s*>'
)


def collect_entities(svg_text: str) -> dict[str, str]:
    """Return name→value for every <!ENTITY> in the DOCTYPE prolog."""
    out: dict[str, str] = {}
    for m in ENTITY_DEF_RE.finditer(svg_text):
        out[m.group(1)] = m.group(2) if m.group(2) is not None else m.group(3)
    return out


def expand_entities(body: str, entities: dict[str, str]) -> str:
    """Replace `&name;` with the entity's literal value. XML built-ins
    (`amp`, `lt`, `gt`, `quot`, `apos`) and numeric refs are left alone."""
    if not entities:
        return body
    pat = re.compile(r"&([A-Za-z_][\w.-]*);")
    return pat.sub(lambda m: entities.get(m.group(1), m.group(0)), body)


def parse_viewbox(svg_text: str) -> tuple[float, float, float, float] | None:
    m = SVG_OPEN_RE.search(svg_text)
    if not m:
        return None
    vbm = VB_RE.search(m.group(0))
    if not vbm:
        return None
    parts = vbm.group(1).replace(",", " ").split()
    if len(parts) != 4:
        return None
    try:
        return tuple(float(p) for p in parts)
    except ValueError:
        return None


def svg_inner_body(svg_text: str) -> str | None:
    om = SVG_OPEN_RE.search(svg_text)
    cm = SVG_CLOSE_RE.search(svg_text)
    if not om or not cm or cm.start() <= om.end():
        return None
    return svg_text[om.end():cm.start()].strip("\n")


def is_white(rgb: tuple[int, int, int]) -> bool:
    return rgb == (255, 255, 255)


# Per-channel tolerance when comparing perimeter samples to their mean.
# Generous enough to accept gradient bgs (e.g. BBC Cymru Wales red fades
# across the canvas) but tight enough to reject noise/anti-aliasing.
BG_CORNER_TOL = 48

# Minimum fraction of perimeter samples that must be opaque AND within
# tolerance of the running mean for the design to be classified as
# having a built-in background. A few stray transparent pixels (e.g. a
# logo whose top edge nearly-but-not-quite touches the canvas) are OK;
# a wordmark whose ink fills 60% of a narrow canvas is not.
BG_AGREEMENT = 0.92

# How far back from the canvas edge we sample. 2px avoids the worst of
# the rsvg anti-aliasing fringe while still being firmly "on the edge".
BG_INSET = 2


def detect_source_background(path: Path) -> tuple[int, int, int] | None:
    """Render the source SVG and sample many points around its
    perimeter. Returns a consensus RGB if ≥ BG_AGREEMENT of opaque
    samples agree to within ``BG_CORNER_TOL`` per channel — the source
    paints its own canvas. Returns None otherwise (transparent canvas,
    multi-coloured edges, etc.).

    Corner-only sampling was too sparse: a 64-px-tall narrow wordmark
    fills its corner pixels with ink even on an otherwise transparent
    canvas; conversely BBC America's three BBC-box ink shapes happen to
    pin every corner to black despite gaps between the boxes."""
    if Image is None:
        return None
    rsvg = shutil.which("rsvg-convert")
    if rsvg is None:
        return None
    try:
        r = subprocess.run(
            [rsvg, "-w", "128", str(path)],
            capture_output=True, timeout=15,
        )
        if r.returncode != 0 or not r.stdout:
            return None
        img = Image.open(io.BytesIO(r.stdout))
    except Exception:
        return None
    if img.mode != "RGBA":
        img = img.convert("RGBA")
    w, h = img.size
    if w < 8 or h < 8:
        return None

    # Walk the perimeter with a stride proportional to canvas size.
    step = max(2, min(w, h) // 12)
    inset = BG_INSET
    points: list[tuple[int, int]] = []
    for x in range(inset, w - inset, step):
        points.append((x, inset))
        points.append((x, h - 1 - inset))
    for y in range(inset, h - inset, step):
        points.append((inset, y))
        points.append((w - 1 - inset, y))

    samples = [img.getpixel(p) for p in points]
    opaque = [(r_, g_, b_) for (r_, g_, b_, a_) in samples if a_ >= 128]
    if not opaque or len(opaque) / len(samples) < BG_AGREEMENT:
        return None

    mean = tuple(sum(c[i] for c in opaque) // len(opaque) for i in range(3))
    in_tol = [c for c in opaque
              if all(abs(c[i] - mean[i]) <= BG_CORNER_TOL for i in range(3))]
    if len(in_tol) / len(samples) < BG_AGREEMENT:
        return None

    # Perimeter consensus alone can't tell a "tight-cropped silhouette"
    # apart from a true background — both pin every edge pixel to the
    # ink colour. Sample the interior on a grid: a true bg paints the
    # whole canvas, so almost every interior point is opaque too. A
    # silhouette has visible gaps (negative space) between strokes.
    grid_step = max(4, min(w, h) // 8)
    grid: list[tuple[int, ...]] = []
    for x in range(grid_step, w - grid_step + 1, grid_step):
        for y in range(grid_step, h - grid_step + 1, grid_step):
            grid.append(img.getpixel((x, y)))
    if grid:
        grid_opaque = sum(1 for p in grid if p[3] >= 128)
        if grid_opaque / len(grid) < BG_AGREEMENT:
            return None

    # Recompute mean over the in-tolerance subset for a tighter estimate.
    final = tuple(sum(c[i] for c in in_tol) // len(in_tol) for i in range(3))
    return final  # type: ignore[return-value]


def analyse_logo(
    path: Path,
    viewbox: tuple[float, float, float, float] | None,
) -> tuple[list[tuple[tuple[int, int, int], float]],
           tuple[int, int, int] | None]:
    """Returns (inks, source_background):
      * inks — area-weighted (rgb, area) ink colours, sorted descending.
      * source_background — corner-sampled bg colour from a small render
        of the source, or None if the design has a transparent canvas.
    """
    text = path.read_text(encoding="utf-8", errors="ignore")
    text = expand_entities(text, collect_entities(text))
    svg = SVG.parse(io.StringIO(text), reify=True)

    visible: dict[tuple[int, int, int], float] = {}
    white_only: dict[tuple[int, int, int], float] = {}

    for el in svg.elements():
        if not isinstance(el, Shape):
            continue
        try:
            bb = el.bbox()
        except Exception:
            continue
        if bb is None:
            continue
        x0, y0, x1, y1 = bb
        bw, bh = x1 - x0, y1 - y0
        if bw <= 1e-6 or bh <= 1e-6:
            continue
        area = bw * bh

        fill = paint_to_rgb(getattr(el, "fill", None))
        if fill is not None:
            bucket = white_only if is_white(fill) else visible
            bucket[fill] = bucket.get(fill, 0.0) + area

        stroke = paint_to_rgb(getattr(el, "stroke", None))
        if stroke is not None:
            sw = float(getattr(el, "stroke_width", 0) or 0)
            if sw > 0:
                perim = 2.0 * (bw + bh)
                bucket = white_only if is_white(stroke) else visible
                bucket[stroke] = bucket.get(stroke, 0.0) + perim * sw

    if not visible:
        visible = white_only

    source_bg = detect_source_background(path)
    if source_bg is not None:
        # Filter the bg colour out of the ink list — anything within
        # corner tolerance of the bg renders effectively against itself
        # and shouldn't drag the contrast computation down.
        visible = {
            rgb: area for rgb, area in visible.items()
            if any(abs(rgb[i] - source_bg[i]) > BG_CORNER_TOL for i in range(3))
        }

    inks = sorted(visible.items(), key=lambda kv: -kv[1])
    return inks, source_bg


# ─── background selection ─────────────────────────────────────────────

def significant_inks(
    inks: list[tuple[tuple[int, int, int], float]],
    coverage: float = 0.9,
) -> list[tuple[int, int, int]]:
    """Top inks covering at least ``coverage`` of total area. Always returns
    at least one colour when ``inks`` is non-empty."""
    if not inks:
        return [(0, 0, 0)]
    total = sum(a for _, a in inks) or 1.0
    out: list[tuple[int, int, int]] = []
    cum = 0.0
    for rgb, area in inks:
        out.append(rgb)
        cum += area
        if cum >= coverage * total:
            break
    return out or [inks[0][0]]


def pick_background(
    inks: list[tuple[tuple[int, int, int], float]],
    brand_colors: list[tuple[str, tuple[int, int, int]]],
) -> tuple[tuple[int, int, int], str, float]:
    """Returns (rgb, name, min_ratio). Highest worst-case contrast wins."""
    targets = significant_inks(inks)
    candidates: list[tuple[tuple[int, int, int], str]] = []
    seen: set[tuple[int, int, int]] = set()
    for label, rgb in brand_colors:
        if rgb in seen:
            continue
        seen.add(rgb)
        candidates.append((rgb, f"brand {label}"))
    for rgb, name in (((255, 255, 255), "white"), ((0, 0, 0), "black")):
        if rgb in seen:
            continue
        candidates.append((rgb, name))

    best: tuple[tuple[int, int, int], str, float] | None = None
    for bg, name in candidates:
        worst = min(contrast_ratio(bg, ink) for ink in targets)
        if best is None or worst > best[2]:
            best = (bg, name, worst)
    assert best is not None  # white/black always present
    return best


# ─── SVG assembly ─────────────────────────────────────────────────────

def fmt(n: float) -> str:
    if abs(n - round(n)) < 1e-9:
        return str(int(round(n)))
    return f"{n:.4f}".rstrip("0").rstrip(".")


# The default SVG namespace. Always declared on our wrapper. Any extra
# prefixed namespaces (xmlns:foo="...") declared on the source logo are
# lifted onto the wrapper at build time so embedded attributes like
# `serif:id="..."` or `sodipodi:namedview` keep resolving once the
# original <svg> open tag is dropped.
DEFAULT_NS = 'xmlns="http://www.w3.org/2000/svg"'

XMLNS_PREFIXED_RE = re.compile(r'\bxmlns:[A-Za-z_][\w.-]*\s*=\s*"[^"]*"')


def lift_source_namespaces(svg_text: str) -> str:
    """Return space-prefixed extra `xmlns:foo="..."` declarations from
    the source's root <svg> tag, or the empty string. Entity references
    inside URI values (an Adobe Illustrator quirk, e.g.
    `xmlns:i="&ns_ai;"`) are expanded against the source's DOCTYPE so
    the wrapper's namespace URIs remain valid XML."""
    m = SVG_OPEN_RE.search(svg_text)
    if not m:
        return ""
    entities = collect_entities(svg_text)
    decls: list[str] = []
    seen: set[str] = set()
    for nm in XMLNS_PREFIXED_RE.finditer(m.group(0)):
        text = expand_entities(nm.group(0), entities)
        if text in seen:
            continue
        seen.add(text)
        decls.append(text)
    return (" " + " ".join(decls)) if decls else ""


def build_svg(
    spec: dict,
    logo_text: str,
    bg_rgb: tuple[int, int, int],
    name_label: str,
    source_has_bg: bool,
) -> str:
    """Wrap ``logo_text`` inside a thumb-canvas SVG of the configured
    dimensions: the source is scaled (preserving aspect ratio), centred
    on the canvas, and the outer background ``rect`` fills the margins.

    Two scaling modes:

      source_has_bg == False  → FIT to the template safe area. The
        outer bg shows around all four sides of the logo; the inset
        gives wordmarks/icons breathing room from the canvas edge.

      source_has_bg == True   → FIT to the FULL canvas (ignore the
        safe-area inset). The source already paints its own canvas
        design, so we want it as close to edge-to-edge as we can get
        while preserving aspect ratio. Margins exist only along the
        one dimension where source aspect ≠ canvas aspect; the outer
        bg colour (sampled from the source) blends into the source's
        edge along that dimension, so any seam is restricted to two
        edges instead of all four."""
    vb = parse_viewbox(logo_text)
    if vb is None:
        raise RuntimeError("logo has no viewBox")
    lx, ly, lw, lh = vb
    if lw <= 0 or lh <= 0:
        raise RuntimeError(f"logo viewBox is empty: {vb}")
    inner = svg_inner_body(logo_text)
    if inner is None:
        raise RuntimeError("logo has no <svg> body")
    inner = expand_entities(inner, collect_entities(logo_text))

    cw, ch, inset = spec["w"], spec["h"], spec["inset"]
    if source_has_bg:
        scale = min(cw / lw, ch / lh)
    else:
        safe_w, safe_h = cw - 2 * inset, ch - 2 * inset
        scale = min(safe_w / lw, safe_h / lh)
    tx = cw / 2.0 - (lx + lw / 2.0) * scale
    ty = ch / 2.0 - (ly + lh / 2.0) * scale

    ns = DEFAULT_NS + lift_source_namespaces(logo_text)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<svg {ns}'
        f' viewBox="0 0 {cw} {ch}" width="{cw}" height="{ch}"'
        f' role="img" aria-label="{name_label}">\n'
        f'  <rect width="{cw}" height="{ch}" fill="{to_hex(bg_rgb)}"/>\n'
        f'  <g transform="translate({fmt(tx)} {fmt(ty)}) scale({fmt(scale)})">\n'
        f'{inner}\n'
        '  </g>\n'
        '</svg>\n'
    )


# ─── manifest helpers ─────────────────────────────────────────────────

def collect_brand_colors(entries: list[dict]) -> list[tuple[str, tuple[int, int, int]]]:
    out: list[tuple[str, tuple[int, int, int]]] = []
    seen: set[tuple[int, int, int]] = set()
    for e in entries:
        for c in (e.get("colors") or []):
            if not isinstance(c, dict):
                continue
            hex_s = (c.get("hex") or "").strip()
            rgb = parse_hex(hex_s)
            if rgb is None or rgb in seen:
                continue
            seen.add(rgb)
            label = c.get("name") or hex_s
            out.append((f"{label} ({hex_s})", rgb))
    return out


def merge_formats(existing, want=("svg", "webp")) -> list[str]:
    if existing is None:
        return list(want)
    cur = list(existing) if isinstance(existing, list) else [existing]
    for f in want:
        if f not in cur:
            cur.append(f)
    return cur


def update_entry(entry: dict, slot: str) -> None:
    artwork = entry.setdefault("artwork", {})
    artwork[slot] = merge_formats(artwork.get(slot))
    entry["template"] = True


# Signature we always emit on the wrapper <svg>. Lets us tell our own
# generated thumbs apart from hand-authored ones so --force doesn't
# wipe out hand-crafted artwork. The studio name appears verbatim
# inside the aria-label so a stray match in unrelated XML is unlikely.
GENERATOR_SIGNATURE_RE = re.compile(
    r'\brole\s*=\s*["\']img["\'][^>]*\baria-label\s*=\s*["\'][^"\']*\((?:thumb|primary)\)["\']',
    re.IGNORECASE,
)


def is_generated_output(path: Path) -> bool:
    """True if the existing file at ``path`` was produced by this script
    (matches our wrapper signature). False for hand-authored thumbs."""
    if not path.exists():
        return False
    try:
        head = path.read_text(encoding="utf-8", errors="ignore")[:4096]
    except OSError:
        return False
    return GENERATOR_SIGNATURE_RE.search(head) is not None


def needs_processing(
    entry: dict,
    force: bool,
    output_path: Path,
    overwrite_handcrafted: bool,
) -> bool:
    """Decide whether an entry should have its thumb regenerated.

      template == True            → already template-aligned, skip.
      template == False           → explicit request, process.
      template missing & file exists → existing artwork is hand-crafted;
                                       leave it alone (the original
                                       motivation for this script was to
                                       fill in *missing* thumbs, not to
                                       clobber hand-authored ones).
      template missing & no file  → bootstrap case, process.
      --force                     → process regardless, BUT still refuse
                                    to overwrite hand-authored thumbs
                                    (i.e. files our generator did not
                                    emit) unless --overwrite-handcrafted
                                    is also passed. Plain --force is
                                    meant for re-rendering our own past
                                    outputs after the algorithm changes,
                                    not for clobbering bespoke artwork.
    """
    if not isinstance(entry, dict):
        return False
    if entry.get("placeholder"):
        return False
    if force:
        if (output_path.exists()
                and not overwrite_handcrafted
                and not is_generated_output(output_path)):
            return False
        return True
    tpl = entry.get("template")
    if tpl is True:
        # template=true is the manifest saying "we already templated
        # this entry on a prior run." If the output file is actually
        # missing (deleted by hand, lost in a rebase, etc.) we still
        # need to regenerate it — a stale flag shouldn't hide the gap.
        return not output_path.exists()
    if tpl is False:
        return True
    # template field absent
    return not output_path.exists()


# ─── main loop ────────────────────────────────────────────────────────

def process_studio(
    studio_dir: Path,
    spec: dict,
    write: bool,
    force: bool,
    allow_low_contrast: bool,
    overwrite_handcrafted: bool,
) -> tuple[str, str]:
    sj_path = studio_dir / "studio.json"
    try:
        data = json.loads(sj_path.read_text())
    except Exception as e:
        return "err", f"{studio_dir.name}: studio.json: {e}"
    if not isinstance(data, list):
        return "skip", f"{studio_dir.name}: studio.json is not a list"

    out_path = studio_dir / spec["out"]
    candidates = [e for e in data if isinstance(e, dict)]
    targets = [e for e in candidates
               if needs_processing(e, force, out_path, overwrite_handcrafted)]
    if not targets:
        if out_path.exists() and not is_generated_output(out_path):
            kind = "hand-crafted preserved"
        elif out_path.exists():
            kind = "already templated"
        else:
            kind = "no work needed"
        return "skip", f"{studio_dir.name}: {kind}"

    logo_path = studio_dir / "logo.svg"
    if not logo_path.exists():
        return "skip", f"{studio_dir.name}: no logo.svg"
    logo_text = logo_path.read_text(encoding="utf-8")

    try:
        inks, source_bg = analyse_logo(logo_path, parse_viewbox(logo_text))
    except Exception as e:
        return "err", f"{studio_dir.name}: colour extraction failed: {e}"
    if not inks and source_bg is None:
        return "warn", f"{studio_dir.name}: no ink colours detected"

    brand = collect_brand_colors(targets)

    if source_bg is not None:
        # Source already paints its own canvas — inherit it so the design
        # bleeds seamlessly to the edge of the thumb. Contrast ratio is
        # reported against significant inks for telemetry only.
        bg_rgb, bg_label = source_bg, "source built-in bg"
        if inks:
            ratio = min(contrast_ratio(bg_rgb, ink)
                        for ink in significant_inks(inks))
        else:
            ratio = float("inf")
    else:
        bg_rgb, bg_label, ratio = pick_background(inks, brand)
        if ratio < MIN_RATIO and not allow_low_contrast:
            return "warn", (
                f"{studio_dir.name}: best contrast {ratio:.2f}:1 "
                f"(< {MIN_RATIO}) — skipped (rerun with --allow-low-contrast)"
            )

    tier = ("source-bg" if source_bg is not None
            else "AAA" if ratio >= GOOD_RATIO
            else "AA" if ratio >= MIN_RATIO
            else "below-AA")
    ratio_txt = "n/a" if ratio == float("inf") else f"{ratio:.2f}:1"
    detail = (f"bg {to_hex(bg_rgb)} ({bg_label}), "
              f"contrast {ratio_txt} [{tier}]")

    if write:
        try:
            svg = build_svg(
                spec, logo_text, bg_rgb,
                name_label=f"{studio_dir.name} ({spec['slot']})",
                source_has_bg=(source_bg is not None),
            )
        except Exception as e:
            return "err", f"{studio_dir.name}: assembly failed: {e}"
        (studio_dir / spec["out"]).write_text(svg, encoding="utf-8")
        for entry in targets:
            update_entry(entry, spec["slot"])
        sj_path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
        return "wrote", f"{studio_dir.name}: {detail}"
    return "plan", f"{studio_dir.name}: {detail}"


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--aspect", choices=sorted(SPECS), default="16x9",
                    help="Output aspect (default: 16x9 / thumb)")
    ap.add_argument("--write", action="store_true",
                    help="Write files. Without --write the script reports "
                         "what it *would* do (dry-run).")
    ap.add_argument("--force", action="store_true",
                    help="Re-process entries even if template=true. Still "
                         "refuses to overwrite hand-authored thumbs (files "
                         "without our generator signature) unless "
                         "--overwrite-handcrafted is also passed.")
    ap.add_argument("--overwrite-handcrafted", action="store_true",
                    help="Allow --force to overwrite hand-authored thumbs. "
                         "Use with care — this clobbers bespoke artwork.")
    ap.add_argument("--allow-low-contrast", action="store_true",
                    help="Emit thumbs whose best background still falls "
                         "below WCAG AA (4.5:1). Default: skip with warn.")
    ap.add_argument("--only", metavar="SLUG", action="append", default=[],
                    help="Process only this studio slug (repeatable).")
    ap.add_argument("--limit", type=int, default=0,
                    help="Stop after this many processed studios (0 = all).")
    args = ap.parse_args()

    spec = SPECS[args.aspect]
    if not STUDIOS.is_dir():
        sys.exit(f"missing directory: {STUDIOS}")

    studios = sorted(STUDIOS.glob("*/studio.json"))
    only = set(args.only)
    if only:
        studios = [s for s in studios if s.parent.name in only]
        missing = only - {s.parent.name for s in studios}
        for m in sorted(missing):
            print(f"  ! unknown studio: {m}", flush=True)

    counts: dict[str, int] = {}
    seen_targets = 0
    for sj in studios:
        if args.limit and seen_targets >= args.limit:
            break
        status, msg = process_studio(
            sj.parent, spec,
            write=args.write,
            force=args.force,
            allow_low_contrast=args.allow_low_contrast,
            overwrite_handcrafted=args.overwrite_handcrafted,
        )
        counts[status] = counts.get(status, 0) + 1
        if status in ("wrote", "plan", "warn", "err"):
            seen_targets += 1
            tag = {"wrote": "wrote", "plan": "plan ", "warn": "warn ",
                   "err": "ERROR"}[status]
            print(f"  {tag} {msg}", flush=True)

    print()
    print(f"aspect:          {args.aspect}  ({spec['w']}x{spec['h']}, "
          f"safe {spec['w']-2*spec['inset']}x{spec['h']-2*spec['inset']})")
    print(f"mode:            {'WRITE' if args.write else 'dry-run'}")
    print(f"studios scanned: {len(studios)}")
    for k in ("wrote", "plan", "skip", "warn", "err"):
        if counts.get(k):
            print(f"  {k}: {counts[k]}")
    return 1 if counts.get("err") else 0


if __name__ == "__main__":
    sys.exit(main())
