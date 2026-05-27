#!/usr/bin/env python3
"""Bulk-import TMDB production companies into studios/<slug>/.

Downloads the most recent TMDB daily ID export for production companies
and materialises a placeholder studio.json for every company that is not
already represented in studios/.

Decision matrix per export row:
  * TMDB id already exists in any studio.json (any slug) → skip silently;
    the company is already represented even if under a different slug.
  * Slug already exists on disk → skip with collision warning; merging
    blind would risk attaching the wrong TMDB id to a different company
    that just happens to share a slugified name.
  * Slug collides with an earlier row in this same run → skip with
    in-run collision warning (first row wins).
  * Otherwise → create studios/<slug[0]>/<slug>/studio.json with a
    minimal placeholder entry. Folders are sharded one level deep by
    the slug's first character (a-z, 0-9) to keep individual buckets
    tractable for git, IDEs, and filesystem tools. No artwork files
    are written; consumers fall back to the generic placeholder
    shipped at the release root (placeholder-thumb.svg /
    placeholder-primary.svg).

Usage:
    # Dry-run summary (default)
    python3 scripts/import_tmdb_companies.py

    # Actually write folders + studio.json
    python3 scripts/import_tmdb_companies.py --apply

    # Re-use a previously downloaded export (avoid hammering TMDB)
    python3 scripts/import_tmdb_companies.py --cache
"""
from __future__ import annotations

import argparse
import functools
import gzip
import io
import json
import re
import sys
import unicodedata
import urllib.request
from datetime import date, timedelta
from pathlib import Path

from _studio_safety import clean_str, validate_entries

ROOT = Path(__file__).resolve().parent.parent
STUDIOS = ROOT / "studios"
DOWNLOADS = ROOT / "downloads"
CACHE_PATH = DOWNLOADS / "_tmdb_company_export.json.gz"

# Daily export URL. TMDB names them in US date format.
EXPORT_URL_FMT = (
    "http://files.tmdb.org/p/exports/"
    "production_company_ids_{m:02d}_{d:02d}_{y}.json.gz"
)


def slugify(name: str) -> str:
    """Match the slug convention already in use under studios/ - lower-
    case ASCII with non-alphanumerics collapsed to single hyphens.

    Folders like ``warner-bros-pictures`` and ``20th-century-studios``
    are produced by this exact transform applied to their TMDB names.
    """
    n = unicodedata.normalize("NFKD", name)
    n = n.encode("ascii", "ignore").decode("ascii").lower()
    n = re.sub(r"[^a-z0-9]+", "-", n).strip("-")
    return n


# Jellyfin's project-wide ICU transliteration chain. Mirrors what the
# Jellyfin server / artwork plugin themselves use to slug studio names,
# so a slug we produce here lines up with what the consumer expects to
# look up. Without this, names in non-Latin scripts (Han, Cyrillic,
# Greek, Arabic, Thai, ...) ASCII-fold to empty and we have to fall
# back to a tmdb-{id} placeholder folder.
#
#   Any-Latin      Han → pinyin, Cyrillic → BGN, Greek/Arabic/Thai/...
#                  → their ICU "Any-Latin" rule chain.
#   Latin-ASCII    Strip remaining Latin diacritics ("é" → "e").
#   Lower          Casefold so "Café" → "cafe" cleanly.
#   NFD;
#   [:Nonspacing Mark:] Remove;
#   NFC            Decompose, drop combining marks, recompose. Catches
#                  diacritics Latin-ASCII missed (e.g. composed Korean
#                  jamo).
ICU_TRANSLIT_RULES = (
    "Any-Latin; Latin-ASCII; Lower; NFD; [:Nonspacing Mark:] Remove; NFC"
)


PYICU_INSTALL_HINT = (
    "PyICU is required for non-Latin company names. Install it locally:\n"
    "    brew install icu4c\n"
    "    ICU_PREFIX=$(brew --prefix icu4c@78)\n"
    "    PKG_CONFIG_PATH=$ICU_PREFIX/lib/pkgconfig \\\n"
    "        python3 -m pip install --break-system-packages PyICU\n"
    "Or on Debian/Ubuntu:\n"
    "    sudo apt-get install -y libicu-dev pkg-config\n"
    "    python3 -m pip install PyICU"
)


@functools.lru_cache(maxsize=1)
def _icu_transliterator():
    """Returns a configured ICU Transliterator. Exits the process with
    the install hint if PyICU is missing - there's no silent degradation
    here because a fall-back transliterator would produce slugs that
    diverge from Jellyfin's own canonical slugification, defeating the
    point of matching the consumer's chain."""
    try:
        from icu import Transliterator
    except ImportError:
        sys.exit(PYICU_INSTALL_HINT)
    try:
        return Transliterator.createInstance(ICU_TRANSLIT_RULES)
    except Exception as e:
        sys.exit(f"could not build ICU transliterator: {e}\n\n{PYICU_INSTALL_HINT}")


def transliterate_slug(name: str) -> str:
    """Run ``name`` through Jellyfin's ICU rule chain and then through
    the standard slugify."""
    return slugify(_icu_transliterator().transliterate(name))


def slug_candidates(name: str) -> list[str]:
    """Slugs to try for one company, in preference order:
      1. plain ASCII-fold of the name (cheap, no ICU pass needed)
      2. ICU-transliterated form via Jellyfin's chain (Han→pinyin,
         Cyrillic→BGN, Greek/Arabic/Thai/... → their respective ICU
         Any-Latin rules)

    No tmdb-{id} floor - names that slug to empty *and* don't
    transliterate to anything usable are skipped with a warning so a
    human can decide what to do with them, and slug collisions are
    skipped the same way (the conservative choice: refuse to attach a
    new TMDB id to a folder that already represents a different one)."""
    out: list[str] = []
    seen: set[str] = set()
    for s in (slugify(name), transliterate_slug(name)):
        if s and s not in seen:
            out.append(s)
            seen.add(s)
    return out


def fetch_export() -> tuple[bytes, str]:
    """Try today/yesterday/… until one of the recent dates is available.
    TMDB's export job runs once a day and the most recent file is usually
    available by ~08:00 UTC, so a same-day fetch before that fails over
    to the previous day."""
    today = date.today()
    last_err: Exception | None = None
    for offset in range(0, 7):
        d = today - timedelta(days=offset)
        url = EXPORT_URL_FMT.format(m=d.month, d=d.day, y=d.year)
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "jellyfin-artwork/1.0"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                if resp.status == 200:
                    print(f"  fetched export for {d.isoformat()}")
                    return resp.read(), d.isoformat()
        except Exception as e:
            last_err = e
            continue
    raise RuntimeError(f"could not fetch export, last error: {last_err}")


def parse_export(data: bytes) -> list[dict]:
    """Each line of the gz is a separate JSON object: {id, name, …}."""
    out: list[dict] = []
    with gzip.open(io.BytesIO(data), "rt", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            cid = obj.get("id")
            name = clean_str(obj.get("name"))
            if cid is None or name is None:
                # Drops rows whose name is blank/whitespace/None - they
                # would otherwise produce an empty slug and either get
                # silently skipped or, worse, slip into the schema
                # with name="" (the homepage=" " class of bug).
                continue
            out.append({"id": cid, "name": name})
    return out


def existing_tmdb_ids() -> set[str]:
    """Every TMDB id already attached to *any* studio.json entry,
    regardless of slug. Used to short-circuit re-import of companies
    that live under a non-derived slug (``abc-au`` vs. ``abc``)."""
    out: set[str] = set()
    for sj in STUDIOS.glob("*/*/studio.json"):
        try:
            data = json.loads(sj.read_text())
        except Exception:
            continue
        if not isinstance(data, list):
            continue
        for entry in data:
            if not isinstance(entry, dict):
                continue
            for p in entry.get("providers", []) or []:
                if not isinstance(p, dict):
                    continue
                if p.get("provider_name") != "tmdb":
                    continue
                pid = p.get("id")
                if pid is not None:
                    out.add(str(pid))
    return out


def build_entry(name: str, tmdb_id: int) -> dict:
    """Construct a minimal placeholder entry. Consumers fall back to
    the generic placeholder shipped at the release root, so artwork is
    intentionally empty - no per-studio art on disk for these stubs."""
    return {
        "name": name,
        "providers": [{"provider_name": "tmdb", "id": str(tmdb_id)}],
        "artwork": {},
        "placeholder": True,
    }


def make_studio_json(name: str, tmdb_id: int) -> str:
    return json.dumps([build_entry(name, tmdb_id)],
                      indent=2, ensure_ascii=False) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true",
                    help="Actually write files (default: dry-run summary)")
    ap.add_argument("--limit", type=int, default=0,
                    help="Process at most N entries (debug)")
    ap.add_argument("--cache", action="store_true",
                    help=f"Read/write the export from "
                         f"{CACHE_PATH.relative_to(ROOT)} instead of "
                         f"refetching every run.")
    ap.add_argument("--sample", type=int, default=15,
                    help="How many to-create slugs to preview (default 15)")
    args = ap.parse_args()

    if not STUDIOS.is_dir():
        sys.exit(f"missing directory: {STUDIOS}")

    # Eagerly build the transliterator so a missing PyICU surfaces the
    # install hint before we go fetch a 3MB export and walk 235k JSONs.
    _icu_transliterator()

    # Pre-flight: validate a synthetic entry against the repo schema so a
    # template regression (e.g. someone changing build_entry's shape)
    # explodes here on the first row instead of after 100k bad writes.
    sample_errors = validate_entries(
        [build_entry("Sample Studio", 1)],
        source_label="build_entry template",
    )
    if sample_errors:
        for e in sample_errors:
            print(f"  ! {e}")
        sys.exit("build_entry template no longer matches studios.schema.json")

    if args.cache and CACHE_PATH.exists():
        print(f"  using cached export at {CACHE_PATH.relative_to(ROOT)}")
        data = CACHE_PATH.read_bytes()
    else:
        data, _ = fetch_export()
        if args.cache:
            DOWNLOADS.mkdir(exist_ok=True)
            CACHE_PATH.write_bytes(data)
            print(f"  cached at {CACHE_PATH.relative_to(ROOT)}")

    entries = parse_export(data)
    print(f"  parsed {len(entries)} companies from export")
    if args.limit:
        entries = entries[: args.limit]
        print(f"  limit applied → {len(entries)} considered")

    # studios/ is sharded one level deep by slug[0] (a-z, 0-9), so the
    # "existing slugs" set has to walk both levels.
    existing_slugs = {
        d.name for bucket in STUDIOS.iterdir() if bucket.is_dir()
        for d in bucket.iterdir() if d.is_dir()
    }
    existing_ids = existing_tmdb_ids()
    print(f"  {len(existing_slugs)} existing studio directories")
    print(f"  {len(existing_ids)} existing TMDB ids on disk")

    to_create: list[tuple[str, dict]] = []
    skip_id_known = 0
    skip_empty_slug = 0
    skip_slug_existing = 0
    skip_slug_in_run = 0
    used_transliteration = 0
    slug_seen: dict[str, dict] = {}

    for ent in entries:
        if str(ent["id"]) in existing_ids:
            skip_id_known += 1
            continue
        plain = slugify(ent["name"])
        candidates = slug_candidates(ent["name"])
        if not candidates:
            skip_empty_slug += 1
            continue
        chosen: str | None = None
        in_run_clash = False
        for candidate in candidates:
            if candidate in existing_slugs:
                continue
            if candidate in slug_seen:
                in_run_clash = True
                continue
            chosen = candidate
            break
        if chosen is None:
            if in_run_clash:
                skip_slug_in_run += 1
            else:
                skip_slug_existing += 1
            continue
        if chosen != plain:
            used_transliteration += 1
        slug_seen[chosen] = ent
        to_create.append((chosen, ent))

    print()
    print(f"to create:                                {len(to_create)}")
    print(f"  via ICU transliteration (Jellyfin chain): {used_transliteration}")
    print(f"skip (TMDB id already known):             {skip_id_known}")
    print(f"skip (slug already on disk):              {skip_slug_existing}")
    print(f"skip (in-run slug collision):             {skip_slug_in_run}")
    print(f"skip (unsluggable name):                  {skip_empty_slug}")

    if to_create:
        print()
        print(f"sample of new slugs ({min(args.sample, len(to_create))} of "
              f"{len(to_create)}):")
        for slug, ent in to_create[: args.sample]:
            print(f"  {slug[:60]:60s}  ← {ent['name']} (id {ent['id']})")

    if not args.apply:
        print()
        print("dry-run: re-run with --apply to write directories.")
        return 0

    written = 0
    for slug, ent in to_create:
        target = STUDIOS / slug[0] / slug
        target.mkdir(parents=True, exist_ok=True)
        (target / "studio.json").write_text(
            make_studio_json(ent["name"], ent["id"]), encoding="utf-8")
        written += 1
        if written % 1000 == 0:
            print(f"  wrote {written}/{len(to_create)}")
    print(f"  wrote {written} new studio directories")
    return 0


if __name__ == "__main__":
    sys.exit(main())
