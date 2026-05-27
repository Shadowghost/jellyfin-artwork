"""Shared root-<svg> coordinate-box resolution for the artwork scripts.

Both ``generate_thumbs.py`` (which sizes/centres an embedded logo) and
``rescale_svgs.py`` (which clamps clipped content to the source canvas)
need the same primitive: given an SVG's raw text, what user-space box
does its root element define?

The rule, in priority order:
  1. ``viewBox="x y w h"`` - used verbatim (four finite numbers, w/h > 0).
  2. else ``width``/``height`` on the root - synthesised as
     ``(0, 0, width, height)``. Per the SVG spec, a document with intrinsic
     dimensions but no viewBox has an implicit ``0 0 width height`` user
     coordinate system.
  3. else ``None`` - extent unknown.

Unit policy for the width/height fallback: only *unitless* or ``px``/``pt``
lengths have an intrinsic pixel size we can trust. Percentages (and ``em``,
``mm``, ``cm`` … which depend on a reference box or font we don't resolve
here) carry no usable intrinsic size, so they yield ``None`` rather than a
fabricated box. Centralising this keeps the two scripts from drifting into
subtly different parsers - the same regression ``_studio_safety.py`` was
created to prevent on the studio.json side.
"""
from __future__ import annotations

import re

# The root <svg> open tag. ``[^>]*`` is fine because '>' cannot appear
# unescaped inside an attribute value in well-formed XML.
SVG_OPEN_RE = re.compile(r"<svg\b[^>]*>", re.DOTALL)

VB_RE = re.compile(r'\bviewBox\s*=\s*["\']([^"\']+)["\']')

# width/height attributes on the root tag (matched only within the open tag,
# so descendant elements with their own width/height are never picked up).
DIM_RE = {
    "width": re.compile(r'\bwidth\s*=\s*["\']([^"\']+)["\']'),
    "height": re.compile(r'\bheight\s*=\s*["\']([^"\']+)["\']'),
}

# A length we can treat as user units: a number, optionally suffixed with
# px or pt. Anything else (%, em, rem, mm, cm, in, ex, pc, …) fails the
# match and is rejected as having no intrinsic size.
LEN_RE = re.compile(r"^\s*([0-9]*\.?[0-9]+)\s*(?:px|pt)?\s*$")

Box = "tuple[float, float, float, float]"  # (x, y, w, h)


def parse_viewbox(svg_text: str) -> tuple[float, float, float, float] | None:
    """The root viewBox as ``(x, y, w, h)``, or None if absent/malformed
    (not four finite numbers, or non-positive w/h)."""
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
        x, y, w, h = (float(p) for p in parts)
    except ValueError:
        return None
    if w <= 0 or h <= 0:
        return None
    return x, y, w, h


def parse_root_dimensions(svg_text: str) -> tuple[float, float] | None:
    """``(width, height)`` from the root tag when *both* are positive
    unitless/px/pt lengths. None if either is missing, non-positive, or
    carries a unit with no intrinsic pixel size (e.g. %, em)."""
    m = SVG_OPEN_RE.search(svg_text)
    if not m:
        return None
    open_tag = m.group(0)
    dims: list[float] = []
    for attr in ("width", "height"):
        am = DIM_RE[attr].search(open_tag)
        if not am:
            return None
        lm = LEN_RE.match(am.group(1))
        if not lm:
            return None
        val = float(lm.group(1))
        if val <= 0:
            return None
        dims.append(val)
    return dims[0], dims[1]


def resolve_root_box(
    svg_text: str,
) -> tuple[float, float, float, float] | None:
    """The root coordinate box as ``(x, y, w, h)``: viewBox if present,
    else ``(0, 0, width, height)`` from the root dimensions, else None."""
    vb = parse_viewbox(svg_text)
    if vb is not None:
        return vb
    dims = parse_root_dimensions(svg_text)
    if dims is None:
        return None
    return 0.0, 0.0, dims[0], dims[1]
