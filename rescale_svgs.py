#!/usr/bin/env python3
"""Crop SVGs to their content bbox and normalize the longer dimension to 1024px.

Per file:
  1. Parse the SVG with svgelements and compute the bounding box of all
     drawn content (paths, shapes, text glyphs — with transforms applied).
  2. Rewrite the root <svg> tag's viewBox to that bbox (in the file's
     original user-space coordinates) and set width/height so
     max(width, height) == 1024 with aspect ratio preserved.

Only the root <svg> opening tag changes — path data, defs, groups, and
the file's existing pretty-print layout are left untouched.

Accuracy notes:
  * Strokes: svgelements computes geometric path bboxes; very thick
    strokes may extend a fraction of a unit past the bbox. Negligible
    for fill-based logos.
  * Text: live <text> elements use font metrics from the embedded
    font-family attribute, not the system font. For logos that ship text
    rather than path data, the bbox may be approximate.

Requires: `pip install svgelements`.
"""
import argparse
import io
import os
import re
import sys

try:
    from svgelements import SVG, Image, Point, Shape
except ImportError:
    sys.exit("svgelements not installed. Install with: pip install svgelements")

MAX_DIM = 1024

SVG_OPEN_RE = re.compile(r"<svg\b[^>]*>", re.DOTALL)


def attr_re(name):
    return re.compile(
        r'(\b' + re.escape(name) + r'\s*=\s*)(["\'])(.*?)\2',
        re.DOTALL,
    )


W_RE = attr_re("width")
H_RE = attr_re("height")
VB_RE = attr_re("viewBox")


def fmt(n):
    if abs(n - round(n)) < 1e-9:
        return str(int(round(n)))
    return f"{n:.6f}".rstrip("0").rstrip(".")


def set_or_insert(open_tag, regex, name, value):
    if regex.search(open_tag):
        return regex.sub(
            lambda m: f"{m.group(1)}{m.group(2)}{value}{m.group(2)}",
            open_tag, count=1,
        )
    return f'<svg\n   {name}="{value}"' + open_tag[4:]


def rewrite_open_tag(open_tag, viewbox, new_w, new_h):
    vb_str = " ".join(fmt(v) for v in viewbox)
    open_tag = set_or_insert(open_tag, VB_RE, "viewBox", vb_str)
    open_tag = set_or_insert(open_tag, W_RE, "width", fmt(new_w))
    open_tag = set_or_insert(open_tag, H_RE, "height", fmt(new_h))
    return open_tag


def is_invisible(el):
    """Shape with no fill and no stroke renders nothing — usually an
    invisible 'background frame' rect that pollutes the bbox."""
    fill = getattr(el, "fill", None)
    stroke = getattr(el, "stroke", None)
    fill_drawn = fill is not None and getattr(fill, "value", fill) is not None
    stroke_drawn = stroke is not None and getattr(stroke, "value", stroke) is not None
    return not fill_drawn and not stroke_drawn


def is_invisible_on_light_bg(el):
    """A shape with pure-white fill and no stroke renders invisibly on a
    light/transparent background — common in dual-light/dark SVGs that
    ship both versions stacked together. We detect these so a typical
    crop-to-visible-content excludes them. If the *whole* SVG turns out
    to be white-only (a dark-theme logo), we fall back to including them."""
    fill = getattr(el, "fill", None)
    if fill is None:
        return False
    s = str(fill).lower().replace(" ", "")
    if s not in ("#fff", "#ffffff", "white", "rgb(255,255,255)"):
        return False
    stroke = getattr(el, "stroke", None)
    if stroke is not None and getattr(stroke, "value", stroke) is not None:
        return False  # has a stroke → still visible via the outline
    return True


def shape_bbox(el):
    """Bbox of a Shape (Path/Rect/Circle/...) inflated by stroke radius if
    the element is actually stroked. svgelements gives geometric path
    bboxes; ignoring strokes means thick outlines get clipped."""
    try:
        bb = el.bbox()
    except Exception:
        return None
    if bb is None:
        return None
    stroke = getattr(el, "stroke", None)
    if stroke is None or getattr(stroke, "value", stroke) is None:
        return bb
    sw = getattr(el, "stroke_width", 0) or 0
    if sw <= 0:
        return bb
    r = sw / 2.0
    x0, y0, x1, y1 = bb
    return (x0 - r, y0 - r, x1 + r, y1 + r)


def image_bbox(el):
    """Manual bbox for an <image> element: svgelements' Image.bbox() returns
    a degenerate ~zero-size box even when x/y/width/height are populated, so
    we compute corners ourselves and apply any residual transform."""
    x = float(el.x or 0)
    y = float(el.y or 0)
    w = float(el.width or 0)
    h = float(el.height or 0)
    if w <= 0 or h <= 0:
        return None
    corners = [Point(x, y), Point(x + w, y), Point(x, y + h), Point(x + w, y + h)]
    if el.transform is not None:
        corners = [el.transform.point_in_matrix_space(p) for p in corners]
    xs = [p.x for p in corners]
    ys = [p.y for p in corners]
    return (min(xs), min(ys), max(xs), max(ys))


VIEWPORT_ATTR_RE = re.compile(
    r'\s*\b(viewBox|width|height)\s*=\s*("[^"]*"|\'[^\']*\')'
)

# Used to decide whether to canvas-clamp: only when the SVG actually clips
# something. Files without clip-paths render everything inside their path
# bboxes, so clamping to the canvas would hide visible content.
HAS_CLIP_RE = re.compile(
    r'<clipPath\b|clip-path\s*=\s*["\']\s*url\(', re.IGNORECASE
)


def canvas_bounds_from_text(text):
    """Read the file's *original* canvas extent from its raw text (not
    via svgelements, since we'll be parsing a stripped copy). Returns
    (x0, y0, x1, y1) in user-space, or None."""
    m = SVG_OPEN_RE.search(text)
    if not m:
        return None
    open_tag = m.group(0)
    vbm = VB_RE.search(open_tag)
    if vbm:
        parts = vbm.group(3).replace(",", " ").split()
        if len(parts) == 4:
            try:
                x, y, w, h = (float(p) for p in parts)
                if w > 0 and h > 0:
                    return (x, y, x + w, y + h)
            except ValueError:
                pass
    # No viewBox — fall back to (0,0,width,height) if present and numeric.
    wm = W_RE.search(open_tag)
    hm = H_RE.search(open_tag)
    if wm and hm:
        try:
            w = float(re.sub(r"[a-zA-Z%]+$", "", wm.group(3)).strip())
            h = float(re.sub(r"[a-zA-Z%]+$", "", hm.group(3)).strip())
            if w > 0 and h > 0:
                return (0.0, 0.0, w, h)
        except ValueError:
            pass
    return None


def compute_content_bbox(path):
    """Returns (x, y, w, h) of the SVG's visible content bbox in *user space*."""
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()

    # svgelements applies the viewBox->viewport transform during parse, so
    # bboxes from a normal SVG.parse() come back in viewport coords. We
    # want user-space (the coord system viewBox itself uses), so we strip
    # viewBox/width/height from the root before parsing. With no viewport
    # mapping, user-space == viewport-space and the bboxes are correct.
    m = SVG_OPEN_RE.search(text)
    if not m:
        raise RuntimeError("no <svg> tag")
    stripped_open = VIEWPORT_ATTR_RE.sub("", m.group(0))
    stripped_text = text[: m.start()] + stripped_open + text[m.end():]

    svg = SVG.parse(io.StringIO(stripped_text), reify=True)

    boxes = []
    white_boxes = []
    for el in svg.elements():
        # Only count leaf primitives. SVG/Group containers also have a
        # .bbox() that's the union of their children — including children
        # whose own bbox is degenerate/invisible (e.g. an invisible <line>
        # with zero width). Including a container would re-introduce those
        # children's coords even after the degenerate filter below.
        if isinstance(el, Image):
            bb = image_bbox(el)
        elif isinstance(el, Shape):
            bb = shape_bbox(el)
        else:
            continue
        if bb is None:
            continue
        x0, y0, x1, y1 = bb
        if x1 - x0 <= 1e-6 or y1 - y0 <= 1e-6:
            continue
        if isinstance(el, Shape) and is_invisible(el):
            continue
        if isinstance(el, Shape) and is_invisible_on_light_bg(el):
            white_boxes.append(bb)
        else:
            boxes.append(bb)

    # If the file has non-white content, ignore the white duplicates (they
    # render invisible on light backgrounds anyway). If everything is
    # white-only, it's a dark-theme logo — keep it.
    if not boxes:
        boxes = white_boxes
    if not boxes:
        raise RuntimeError("no drawable content")
    xmin = min(b[0] for b in boxes)
    ymin = min(b[1] for b in boxes)
    xmax = max(b[2] for b in boxes)
    ymax = max(b[3] for b in boxes)

    # Canvas-clamp only when the file actually uses clip-paths. Without
    # clipping, everything inside the geometric bbox is visible, so the
    # canvas isn't authoritative about content extent (and in fact may be
    # wrong: many sources ship SVGs with intentionally-loose canvases).
    # If the clamp would produce an empty box, the file's viewBox is bogus
    # (e.g. from a prior buggy run) — fall back to the unclamped bbox.
    if HAS_CLIP_RE.search(text):
        canvas = canvas_bounds_from_text(text)
        if canvas is not None:
            cx0, cy0, cx1, cy1 = canvas
            c_xmin = max(xmin, cx0)
            c_ymin = max(ymin, cy0)
            c_xmax = min(xmax, cx1)
            c_ymax = min(ymax, cy1)
            if c_xmax - c_xmin > 0 and c_ymax - c_ymin > 0:
                xmin, ymin, xmax, ymax = c_xmin, c_ymin, c_xmax, c_ymax

    w, h = xmax - xmin, ymax - ymin
    if w <= 0 or h <= 0:
        raise RuntimeError(f"empty bbox ({w}x{h})")
    return (xmin, ymin, w, h)


def process_file(path, dry_run):
    bbox = compute_content_bbox(path)
    _, _, w, h = bbox
    longer = max(w, h)
    scale = MAX_DIM / longer
    if w >= h:
        new_w, new_h = float(MAX_DIM), h * scale
    else:
        new_w, new_h = w * scale, float(MAX_DIM)

    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    m = SVG_OPEN_RE.search(text)
    if not m:
        raise RuntimeError("no <svg> tag")

    open_tag = m.group(0)
    wm = W_RE.search(open_tag)
    hm = H_RE.search(open_tag)
    old_dims = (
        f"{wm.group(3)}x{hm.group(3)}"
        if wm and hm
        else f"bbox {fmt(w)}x{fmt(h)}"
    )

    new_open = rewrite_open_tag(open_tag, bbox, new_w, new_h)
    new_text = text[: m.start()] + new_open + text[m.end():]

    if not dry_run:
        with open(path, "w", encoding="utf-8") as f:
            f.write(new_text)

    return old_dims, f"{fmt(new_w)}x{fmt(new_h)}"


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("directory", help="root directory to scan recursively")
    ap.add_argument("--dry-run", action="store_true", help="report changes without writing")
    args = ap.parse_args()

    root = os.path.abspath(args.directory)
    if not os.path.isdir(root):
        sys.exit(f"not a directory: {root}")

    targets = []
    for dp, _, files in os.walk(root):
        for name in files:
            if name.lower().endswith(".svg"):
                targets.append(os.path.join(dp, name))
    targets.sort()

    if not targets:
        print("no SVG files found")
        return 0

    total = len(targets)
    print(f"processing {total} SVGs")

    scaled = errored = 0
    for i, path in enumerate(targets, 1):
        rel = os.path.relpath(path, root)
        try:
            old_dims, new_dims = process_file(path, args.dry_run)
            scaled += 1
            print(f"  [{i}/{total}] {old_dims} -> {new_dims}  {rel}", flush=True)
        except Exception as e:
            errored += 1
            print(f"  [{i}/{total}] err {rel}: {e}", flush=True)

    verb = "would crop+rescale" if args.dry_run else "cropped+rescaled"
    print(f"done: {verb} {scaled}, errors {errored}, total {total}")
    return 1 if errored else 0


if __name__ == "__main__":
    sys.exit(main())
