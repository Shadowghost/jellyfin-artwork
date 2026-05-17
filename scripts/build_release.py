#!/usr/bin/env python3
"""Build the release blob locally, mirroring .github/workflows/build_and_release.yml.

Steps (identical to CI):
  1. Wipe and recreate <out>/studios/.
  2. Copy studios/ → <out>/studios/.
  3. Run svgo --multipass on every SVG inside <out>/studios/.
  4. Render every SVG to a sibling .webp using ImageMagick (mogrify or
     magick — whichever is available). Skipped with a warning if
     ImageMagick is not installed.
  5. Build <out>/studios.json by flattening every source
     studios/*/studio.json (reads from the original studios/, NOT the
     dist tree, so original files stay untouched).
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
import shutil
import subprocess
import sys
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SOURCE_STUDIOS = ROOT / "studios"


def which(*names: str) -> str | None:
    for n in names:
        if shutil.which(n):
            return shutil.which(n)
    return None


def copy_studios(out_studios: Path) -> int:
    if out_studios.exists():
        shutil.rmtree(out_studios)
    shutil.copytree(SOURCE_STUDIOS, out_studios, symlinks=False)
    n = sum(1 for _ in out_studios.glob("*"))
    return n


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


def build_studios_json(out_dir: Path) -> int:
    """Concat every source studios/*/studio.json into a single flat array.

    Mirrors `jq -s '.[0]=([.[]]|flatten)|.[0]' studios/**/studio.json`.
    Reads from SOURCE studios/, not the dist copy, so we never touch
    the dist studio.json files in case the per-file remove step runs
    in parallel.
    """
    flat: list[dict] = []
    for sj in sorted(SOURCE_STUDIOS.glob("*/studio.json")):
        with sj.open() as f:
            data = json.load(f)
        if isinstance(data, list):
            flat.extend(data)
        else:
            flat.append(data)
    out = out_dir / "studios.json"
    out.write_text(json.dumps(flat, indent=2, ensure_ascii=False) + "\n")
    return len(flat)


def strip_studio_json(out_studios: Path) -> int:
    removed = 0
    for sj in out_studios.rglob("studio.json"):
        sj.unlink()
        removed += 1
    return removed


def zip_release(out_dir: Path, zip_path: Path) -> None:
    if zip_path.exists():
        zip_path.unlink()
    # Match `cd dist; zip -r -D *` — paths inside the zip are relative
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

    print(f"[1/6] Copy studios/ → {out_studios.relative_to(ROOT) if out_dir.is_relative_to(ROOT) else out_studios}")
    n = copy_studios(out_studios)
    print(f"  copied {n} studio dirs")

    if args.skip_svgo:
        print("[2/6] Optimize SVGs — SKIPPED (--skip-svgo)")
    else:
        print("[2/6] Optimize SVGs with svgo")
        run_svgo(out_studios)

    if args.skip_webp:
        print("[3/6] Render WebP — SKIPPED (--skip-webp)")
    else:
        print("[3/6] Render WebP siblings")
        render_all_webp(out_studios, args.workers)

    print("[4/6] Build studios.json (flattened)")
    count = build_studios_json(out_dir)
    print(f"  flattened {count} entries → {out_dir / 'studios.json'}")

    print("[5/6] Strip per-studio studio.json from dist")
    n = strip_studio_json(out_studios)
    print(f"  removed {n} files")

    if args.skip_zip:
        print("[6/6] Zip release — SKIPPED (--skip-zip)")
    else:
        print("[6/6] Zip release")
        zip_release(out_dir, out_dir / "release.zip")

    print(f"\nDone. Release blob in {out_dir}/")


if __name__ == "__main__":
    main()
