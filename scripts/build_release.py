#!/usr/bin/env python3
"""Build the release blob locally, mirroring .github/workflows/build_and_release.yml.

Steps (identical to CI):
  1. Wipe and recreate <out>/studios/.
  2. Copy studios/ → <out>/studios/.
  3. Run svgo --multipass on every SVG inside <out>/studios/.
  4. Render every SVG to a sibling .webp using ImageMagick (mogrify or
     magick - whichever is available). Skipped with a warning if
     ImageMagick is not installed.
  5. Build <out>/studios.json by flattening every source
     studios/<bucket>/<slug>/studio.json (reads from the original
     studios/, NOT the dist tree, so original files stay untouched).
     Each entry is stamped with its slug so consumers can map the
     manifest back to its on-disk folder.
  6. Remove the per-studio studio.json copies from <out>/studios/.
  7. Zip <out>/studios/ + <out>/studios.json into <out>/release.zip,
     with the zip rooted at <out> so it unpacks as ./studios/* and
     ./studios.json (matching CI's `cd dist; zip -r ../release.zip *`).

Nothing in studios/ is ever modified.

Usage:
  python3 build_release.py                # default: out=dist
  python3 build_release.py --out /tmp/r   # custom output dir
  python3 build_release.py --skip-svgo    # fast iteration
  python3 build_release.py --skip-webp
  python3 build_release.py --skip-zip
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE_STUDIOS = ROOT / "studios"
TEMPLATES = ROOT / "templates"

# Generic artwork placeholders shipped at the root of the release bundle.
# Consumers fall back to these when a studio.json entry is flagged
# placeholder=true (or has no artwork files of its own), avoiding the
# need to materialise a per-studio placeholder for every unknown studio.
PLACEHOLDER_FILES = ("placeholder-thumb.svg", "placeholder-primary.svg")

# Canonical aspect ratio for each well-known artwork slot. logo.svg is
# intentionally absent - logos are arbitrary aspect.
SLOT_ASPECT = {
    "thumb.svg": 16.0 / 9.0,
    "backdrop.svg": 16.0 / 9.0,
    "primary.svg": 1.0,
}
# Slots whose artwork manifest entry should always list ["svg", "webp"]
# when a .svg file is present - the build always renders a webp sibling
# from any source .svg, so the manifest must reflect that.
MANIFEST_SLOTS = {"thumb", "primary", "logo", "backdrop"}
# Relative tolerance for aspect-match. Tighter than 1% triggers false
# positives from float-rounded viewBoxes; looser misses real bugs (TF1's
# primary was off by ~50%).
ASPECT_TOL = 0.01


def which(*names: str) -> str | None:
    for n in names:
        if shutil.which(n):
            return shutil.which(n)
    return None


# ─── lint helpers ─────────────────────────────────────────────────────

_SVG_OPEN_RE = re.compile(r"<svg\b[^>]*>", re.DOTALL)
_VB_ATTR_RE = re.compile(r'(viewBox\s*=\s*)(["\'])([^"\']+)\2')
_W_ATTR_RE = re.compile(r'(\bwidth\s*=\s*)(["\'])([^"\']+)\2')
_H_ATTR_RE = re.compile(r'(\bheight\s*=\s*)(["\'])([^"\']+)\2')


def _fmt_num(n: float) -> str:
    if abs(n - round(n)) < 1e-9:
        return str(int(round(n)))
    return f"{n:.4f}".rstrip("0").rstrip(".")


def _lint_one_aspect(svg: Path, source_root: Path) -> tuple[str, str]:
    """Process one slot SVG. Returns (status, message). Statuses:
    'ok' (in spec), 'fixed' (rewrote), 'failed' (could not parse)."""
    target = SLOT_ASPECT[svg.name]
    try:
        text = svg.read_text(encoding="utf-8")
    except OSError as e:
        return ("failed", f"read {svg.relative_to(source_root)}: {e}")

    m_open = _SVG_OPEN_RE.search(text)
    if not m_open:
        return ("failed", str(svg.relative_to(source_root)))
    open_tag = m_open.group(0)
    m_vb = _VB_ATTR_RE.search(open_tag)
    if not m_vb:
        return ("failed", str(svg.relative_to(source_root)))
    parts = m_vb.group(3).replace(",", " ").split()
    if len(parts) != 4:
        return ("failed", str(svg.relative_to(source_root)))
    try:
        vx, vy, vw, vh = (float(p) for p in parts)
    except ValueError:
        return ("failed", str(svg.relative_to(source_root)))
    if vw <= 0 or vh <= 0:
        return ("failed", str(svg.relative_to(source_root)))

    actual = vw / vh
    if abs(actual - target) / target < ASPECT_TOL:
        return ("ok", "")

    # Extend the narrower axis to match the target aspect and recentre
    # content. We never shrink - that would clip elements.
    if actual > target:
        new_vh = vw / target
        new_vy = vy - (new_vh - vh) / 2.0
        new_vx, new_vw = vx, vw
    else:
        new_vw = vh * target
        new_vx = vx - (new_vw - vw) / 2.0
        new_vy, new_vh = vy, vh
    new_vb = " ".join(_fmt_num(v) for v in (new_vx, new_vy, new_vw, new_vh))

    # Pin the long dimension to 1024 px so every output's pixel canvas
    # matches the rest of the bundle.
    if target >= 1.0:
        pw, ph = 1024, int(round(1024 / target))
    else:
        pw, ph = int(round(1024 * target)), 1024

    new_open = _VB_ATTR_RE.sub(
        lambda m: f'{m.group(1)}"{new_vb}"', open_tag, count=1,
    )
    if _W_ATTR_RE.search(new_open):
        new_open = _W_ATTR_RE.sub(
            lambda m: f'{m.group(1)}"{pw}"', new_open, count=1,
        )
    if _H_ATTR_RE.search(new_open):
        new_open = _H_ATTR_RE.sub(
            lambda m: f'{m.group(1)}"{ph}"', new_open, count=1,
        )

    new_text = text[: m_open.start()] + new_open + text[m_open.end():]
    svg.write_text(new_text, encoding="utf-8")
    rel = svg.relative_to(source_root)
    return ("fixed", f"{rel}: {actual:.3f} -> {target:.3f}")


def _scan_bucket(bucket: Path) -> tuple[list[Path], list[Path]]:
    """Walk one bucket (studios/<x>/) once and pull out the two things
    both lint passes need: every studio.json path, and every slot SVG
    path (thumb/primary/backdrop) that lives in a studio folder.

    Done as a single pass per studio so we readdir() each folder only
    once even though the two lints care about different filenames."""
    sj_out: list[Path] = []
    svg_out: list[Path] = []
    if not bucket.is_dir():
        return sj_out, svg_out
    for studio in bucket.iterdir():
        if not studio.is_dir():
            continue
        for f in studio.iterdir():
            name = f.name
            if name == "studio.json":
                sj_out.append(f)
            elif name in SLOT_ASPECT:
                svg_out.append(f)
    return sj_out, svg_out


def _scan_tree(source_studios: Path) -> tuple[list[Path], list[Path]]:
    """Concurrent bucket scan. With 36 buckets we get filesystem
    parallelism for free on multi-core hosts (and the kernel pipelines
    readdir() syscalls across cores too)."""
    buckets = [b for b in source_studios.iterdir() if b.is_dir()]
    all_sj: list[Path] = []
    all_svg: list[Path] = []
    workers = min(len(buckets) or 1, (os.cpu_count() or 4) * 2)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for sj_list, svg_list in ex.map(_scan_bucket, buckets):
            all_sj.extend(sj_list)
            all_svg.extend(svg_list)
    return all_sj, all_svg


def lint_aspects(source_studios: Path,
                 targets: list[Path] | None = None) -> tuple[int, int, int]:
    """Rewrite any ``thumb.svg``/``backdrop.svg``/``primary.svg`` whose
    outer viewBox aspect doesn't match the canonical aspect for its
    slot. The viewBox is extended along its narrower axis (content
    stays centred), and width/height attrs are pinned so the longer
    dimension is 1024 px.

    Returns ``(checked, fixed, failed)``. Fix is in-place in the
    SOURCE tree so the correction survives between builds and shows
    up in commits.

    ``targets`` may be supplied to skip the file-discovery scan when
    the caller has already enumerated the tree."""
    if targets is None:
        _, targets = _scan_tree(source_studios)

    source_root = source_studios.parent
    checked = len(targets)
    fixed = failed = 0
    workers = min(32, (os.cpu_count() or 4) * 4)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = [ex.submit(_lint_one_aspect, svg, source_root) for svg in targets]
        for fut in as_completed(futures):
            status, msg = fut.result()
            if status == "fixed":
                fixed += 1
                print(f"  aspect {msg}")
            elif status == "failed":
                failed += 1
                print(f"  ! {msg}")
    return checked, fixed, failed


def _lint_one_manifest(sj: Path, source_root: Path) -> tuple[str, str]:
    """Process one studio.json. Returns (status, message) where status
    is one of 'ok' (no change needed), 'fixed' (rewritten), or 'failed'
    (parse error). The message is empty for 'ok' and otherwise a
    relative path."""
    try:
        data = json.loads(sj.read_text())
    except Exception as e:
        return ("failed", f"parse {sj.relative_to(source_root)}: {e}")
    if not isinstance(data, list):
        return ("ok", "")

    # Fast path: a studio whose every entry is a placeholder with empty
    # artwork has nothing to advertise and nothing to rewrite. Skips the
    # iterdir() syscall, which is the hot loop at 235k entries - most
    # of which are bulk-imported placeholders.
    if data and all(
        isinstance(e, dict) and e.get("placeholder") is True
        and e.get("artwork") == {}
        for e in data
    ):
        return ("ok", "")

    present = set()
    for f in sj.parent.iterdir():
        if f.is_file() and f.suffix.lower() == ".svg" and f.stem in MANIFEST_SLOTS:
            present.add(f.stem)
    expected = {slot: ["svg", "webp"] for slot in sorted(present)}

    changed = False
    for entry in data:
        if not isinstance(entry, dict):
            continue
        if entry.get("artwork") != expected:
            entry["artwork"] = expected
            changed = True
    if not changed:
        return ("ok", "")
    sj.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    return ("fixed", str(sj.relative_to(source_root)))


def lint_manifests(source_studios: Path,
                   files: list[Path] | None = None) -> tuple[int, int, int]:
    """For each ``studio.json``, replace the ``artwork`` dict with
    ``{slot: ["svg", "webp"]}`` for every slot whose ``.svg`` sibling
    is present on disk. Removes manifest slots whose ``.svg`` is
    missing (so the manifest never advertises files that won't ship).

    Returns ``(checked, fixed, failed)``. ``files`` may be supplied
    when the caller has already enumerated the tree."""
    source_root = source_studios.parent
    if files is None:
        files, _ = _scan_tree(source_studios)
    checked = len(files)
    fixed = failed = 0
    # I/O-bound: most files are small JSON reads + an iterdir(). Threads
    # let the kernel overlap syscalls; ~4x over serial in practice.
    workers = min(32, (os.cpu_count() or 4) * 4)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = [ex.submit(_lint_one_manifest, sj, source_root) for sj in files]
        for fut in as_completed(futures):
            status, msg = fut.result()
            if status == "fixed":
                fixed += 1
                print(f"  manifest {msg}")
            elif status == "failed":
                failed += 1
                print(f"  ! {msg}")
    return checked, fixed, failed


def copy_studios(out_studios: Path) -> int:
    if out_studios.exists():
        shutil.rmtree(out_studios)
    shutil.copytree(SOURCE_STUDIOS, out_studios, symlinks=False)
    # Glob the slug level (one below the bucket shards) so the count
    # reflects studios, not buckets.
    return sum(1 for _ in out_studios.glob("*/*"))


def list_svgs(out_studios: Path) -> list[Path]:
    return sorted(out_studios.rglob("*.svg"))


def run_svgo(out_studios: Path) -> None:
    svgo = which("svgo")
    if not svgo:
        npx = which("npx")
        if not npx:
            sys.exit("svgo not found. Install with `npm install -g svgo`.")
        cmd = [npx, "svgo", "--multipass", "--quiet", "--recursive", str(out_studios)]
    else:
        cmd = [svgo, "--multipass", "--quiet", "--recursive", str(out_studios)]
    print(f"  $ {' '.join(cmd)}")
    subprocess.run(cmd, check=True)


def render_one_webp_magick(magick: str, svg: Path) -> tuple[Path, bool, str]:
    webp = svg.with_suffix(".webp")
    try:
        r = subprocess.run([magick, str(svg), str(webp)],
                           capture_output=True, text=True, timeout=30)
        if r.returncode != 0 or not webp.exists():
            return svg, False, (r.stderr or r.stdout).strip()[:200]
        return svg, True, ""
    except Exception as e:
        return svg, False, str(e)


def render_one_webp_pipeline(rsvg: str, cwebp: str,
                             svg: Path) -> tuple[Path, bool, str]:
    """rsvg-convert ➜ PNG on stdout ➜ cwebp ➜ WebP file."""
    webp = svg.with_suffix(".webp")
    try:
        p1 = subprocess.Popen([rsvg, str(svg)], stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE)
        p2 = subprocess.Popen([cwebp, "-quiet", "-o", str(webp), "--", "-"],
                              stdin=p1.stdout,
                              stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE)
        assert p1.stdout is not None
        p1.stdout.close()
        _, err2 = p2.communicate(timeout=30)
        p1.wait(timeout=5)
        if p2.returncode != 0 or not webp.exists():
            return svg, False, err2.decode(errors="ignore").strip()[:200]
        return svg, True, ""
    except Exception as e:
        return svg, False, str(e)


def pick_webp_renderer():
    """Probe available tools. Returns (label, render_fn) or (None, None).

    Prefers the rsvg+cwebp pipeline because it's reliable on macOS;
    falls back to ImageMagick. On Linux CI either should work.
    """
    rsvg = which("rsvg-convert")
    cwebp = which("cwebp")
    if rsvg and cwebp:
        fn = lambda svg: render_one_webp_pipeline(rsvg, cwebp, svg)
        return "rsvg-convert + cwebp", fn

    magick = which("magick", "convert", "mogrify")
    if magick:
        return magick, lambda svg: render_one_webp_magick(magick, svg)

    return None, None


def render_all_webp(out_studios: Path, workers: int) -> None:
    label, render_fn = pick_webp_renderer()
    if not render_fn:
        print("  ! No working SVG→WebP toolchain found. Install either:")
        print("      brew install librsvg webp     (preferred on macOS)")
        print("      brew install imagemagick      (matches CI)")
        sys.exit(2)
    print(f"  using {label}")
    svgs = list_svgs(out_studios)
    print(f"  rendering {len(svgs)} SVG → WebP with {workers} workers")
    failed: list[tuple[Path, str]] = []
    ok = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = [ex.submit(render_fn, s) for s in svgs]
        for i, fut in enumerate(as_completed(futures), 1):
            svg, success, err = fut.result()
            if success:
                ok += 1
            else:
                failed.append((svg, err))
            if i % 500 == 0:
                print(f"    ...{i}/{len(svgs)} ({ok} ok, {len(failed)} failed)")
    print(f"  rendered {ok}/{len(svgs)} ({len(failed)} failed)")
    for s, err in failed[:5]:
        print(f"    fail: {s.relative_to(out_studios.parent)} :: {err}")


def copy_placeholders(out_dir: Path, run_svgo: bool, run_webp: bool) -> list[Path]:
    """Copy each generic placeholder from templates/ to the root of
    out_dir, then put it through the same svgo and webp passes as the
    per-studio artwork so the bundle ships a normalised .svg + .webp
    pair for every placeholder slot.

    Returns the list of dist-side .svg paths so the caller can include
    them in any subsequent step (e.g. the zip)."""
    copied: list[Path] = []
    for name in PLACEHOLDER_FILES:
        src = TEMPLATES / name
        if not src.exists():
            print(f"  ! missing source placeholder: {src.relative_to(ROOT)}")
            continue
        dst = out_dir / name
        shutil.copy2(src, dst)
        copied.append(dst)
        print(f"  copied {dst.relative_to(out_dir.parent if out_dir.is_relative_to(ROOT) else out_dir)}")

    if not copied:
        return copied

    if run_svgo:
        svgo = which("svgo")
        if svgo:
            cmd = [svgo, "--multipass", "--quiet", *[str(p) for p in copied]]
        else:
            npx = which("npx")
            if not npx:
                sys.exit("svgo not found. Install with `npm install -g svgo`.")
            cmd = [npx, "svgo", "--multipass", "--quiet", *[str(p) for p in copied]]
        subprocess.run(cmd, check=True)

    if run_webp:
        label, render_fn = pick_webp_renderer()
        if not render_fn:
            print("  ! no SVG→WebP toolchain found; skipping placeholder webp")
        else:
            for svg in copied:
                _, ok, err = render_fn(svg)
                if not ok:
                    print(f"  ! webp render failed for {svg.name}: {err}")

    return copied


def build_studios_json(out_dir: Path) -> int:
    """Concat every source studios/<bucket>/<slug>/studio.json into a
    single flat array, stamping each entry with the slug derived from
    its parent folder. Consumers use the slug to find artwork under
    studios/<slug[0]>/<slug>/ in the release bundle.

    Reads from SOURCE studios/, not the dist copy, so we never touch
    the dist studio.json files in case the per-file remove step runs
    in parallel.
    """
    flat: list[dict] = []
    for sj in sorted(SOURCE_STUDIOS.glob("*/*/studio.json")):
        slug = sj.parent.name
        with sj.open() as f:
            data = json.load(f)
        entries = data if isinstance(data, list) else [data]
        for entry in entries:
            if isinstance(entry, dict):
                entry.setdefault("slug", slug)
            flat.append(entry)
    out = out_dir / "studios.json"
    out.write_text(json.dumps(flat, indent=2, ensure_ascii=False) + "\n")
    return len(flat)


def strip_studio_json(out_studios: Path) -> tuple[int, int]:
    """Remove every per-studio ``studio.json`` from the dist tree (the
    flat ``studios.json`` is the manifest consumers actually read) and
    then prune the studio/bucket folders that have become empty as a
    result. Placeholder studios with no on-disk artwork would otherwise
    contribute ~234k empty directories to the release zip.

    Returns ``(removed_files, removed_dirs)``."""
    removed_files = 0
    for sj in out_studios.rglob("studio.json"):
        sj.unlink()
        removed_files += 1

    # Walk bottom-up so studio folders empty before their bucket parent
    # is checked. rmdir() is a no-op (raises OSError) on non-empty dirs,
    # which is exactly the behaviour we want - studios that still have
    # artwork files stick around.
    removed_dirs = 0
    for bucket in out_studios.iterdir():
        if not bucket.is_dir():
            continue
        for studio in list(bucket.iterdir()):
            if studio.is_dir():
                try:
                    studio.rmdir()
                    removed_dirs += 1
                except OSError:
                    pass  # still has artwork - keep it
        # Bucket itself: only meaningful if EVERY studio under it was a
        # placeholder. Most buckets keep at least one real studio, but
        # we try anyway so the zip isn't carrying empty-shell buckets.
        try:
            bucket.rmdir()
            removed_dirs += 1
        except OSError:
            pass
    return removed_files, removed_dirs


def zip_release(out_dir: Path, zip_path: Path) -> None:
    if zip_path.exists():
        zip_path.unlink()
    # Match `cd dist; zip -r -D *` - paths inside the zip are relative
    # to <out>, with no leading "dist/".
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
        for p in sorted(out_dir.rglob("*")):
            if p == zip_path:
                continue  # don't zip ourselves
            if p.is_dir():
                continue
            zf.write(p, p.relative_to(out_dir))
    size_mb = zip_path.stat().st_size / 1024 / 1024
    print(f"  wrote {zip_path} ({size_mb:.1f} MiB)")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--out", type=Path, default=ROOT / "dist",
                   help="Output directory (default: ./dist)")
    p.add_argument("--skip-lint", action="store_true",
                   help="Skip the pre-build lint pass (canonical aspect "
                        "ratios for thumb/primary/backdrop and artwork-"
                        "manifest sync). Off by default; lint mutates "
                        "source studios/ in place when it finds issues.")
    p.add_argument("--skip-svgo", action="store_true")
    p.add_argument("--skip-webp", action="store_true")
    p.add_argument("--skip-zip", action="store_true")
    p.add_argument("--workers", type=int,
                   default=max(4, (os.cpu_count() or 4)),
                   help="Parallel workers for WebP rendering")
    args = p.parse_args()

    out_dir: Path = args.out
    out_dir.mkdir(parents=True, exist_ok=True)
    out_studios = out_dir / "studios"

    if args.skip_lint:
        print("[1/7] Lint source - SKIPPED (--skip-lint)")
    else:
        print("[1/7] Lint source (aspect ratios + artwork manifests)")
        # Single concurrent tree scan feeds both lints - saves the
        # second readdir() pass over 235k directories.
        sj_files, svg_files = _scan_tree(SOURCE_STUDIOS)
        a_checked, a_fixed, a_failed = lint_aspects(
            SOURCE_STUDIOS, targets=svg_files)
        m_checked, m_fixed, m_failed = lint_manifests(
            SOURCE_STUDIOS, files=sj_files)
        print(f"  aspect:   checked={a_checked} fixed={a_fixed} failed={a_failed}")
        print(f"  manifest: checked={m_checked} fixed={m_fixed} failed={m_failed}")
        if a_failed or m_failed:
            sys.exit("lint reported failures; aborting before release")

    print(f"[2/7] Copy studios/ → {out_studios.relative_to(ROOT) if out_dir.is_relative_to(ROOT) else out_studios}")
    n = copy_studios(out_studios)
    print(f"  copied {n} studio dirs")

    if args.skip_svgo:
        print("[3/7] Optimize SVGs - SKIPPED (--skip-svgo)")
    else:
        print("[3/7] Optimize SVGs with svgo")
        run_svgo(out_studios)

    if args.skip_webp:
        print("[4/7] Render WebP - SKIPPED (--skip-webp)")
    else:
        print("[4/7] Render WebP siblings")
        render_all_webp(out_studios, args.workers)

    print("[5/7] Build studios.json (flattened)")
    count = build_studios_json(out_dir)
    print(f"  flattened {count} entries → {out_dir / 'studios.json'}")

    print("[5b] Bundle generic placeholder artwork")
    copy_placeholders(
        out_dir,
        run_svgo=not args.skip_svgo,
        run_webp=not args.skip_webp,
    )

    print("[6/7] Strip per-studio studio.json from dist")
    n_files, n_dirs = strip_studio_json(out_studios)
    print(f"  removed {n_files} files, {n_dirs} empty directories")

    if args.skip_zip:
        print("[7/7] Zip release - SKIPPED (--skip-zip)")
    else:
        print("[7/7] Zip release")
        zip_release(out_dir, out_dir / "release.zip")

    print(f"\nDone. Release blob in {out_dir}/")


if __name__ == "__main__":
    main()
