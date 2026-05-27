#!/usr/bin/env python3
"""Enrich every studios/<slug[0]>/<slug>/studio.json with TMDB data.

Two-phase, both cached and resumable. The phases are id-first: any
studio that already carries a tmdb provider id goes straight to the
DETAILS phase; only id-less studios trigger a name search.

  1. SEARCH  - for studios *without* a tmdb provider id and not yet in
     downloads/_tmdb_candidates.json, hit /search/company and store all
     candidates. Promote the top match to providers.tmdb when name
     similarity >= PROMOTE_THRESHOLD. Bulk-imported studios skip this
     phase entirely because their canonical id was set at import time.

  2. DETAILS - for every entry with a confirmed providers.tmdb id
     (either already-present from import / manual data, or promoted by
     phase 1), hit /company/{id} and cache the full record in
     downloads/_tmdb_details.json. Then merge into the entry:
       - description       (top-level, only if missing)
       - country           (top-level, ISO-2; only if missing)
       - homepage          (top-level URL; only if missing)
       - headquarters      (top-level; only if missing)
       - parent_company    (top-level; only if missing)
       - logo_uri          (on the tmdb provider entry; built from
                            logo_path against the TMDB CDN)
     The `placeholder: true` flag is removed once any real metadata
     gets filled in.

Every write is validated against .github/studios.schema.json before
it commits - the safeguard that catches the homepage=" " / providers-
as-dict class of regression.

Usage:
  TMDB_API_KEY=xxxx python3 enrich_tmdb_info.py            # dry-run
  TMDB_API_KEY=xxxx python3 enrich_tmdb_info.py --apply
  TMDB_API_KEY=xxxx python3 enrich_tmdb_info.py --apply --skip-search
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from _studio_safety import (
    clean_iso2,
    clean_str,
    clean_url,
    find_provider,
    upsert_provider,
    validate_entries,
)

ROOT = Path(__file__).resolve().parent.parent
STUDIOS = ROOT / "studios"
DOWNLOADS = ROOT / "downloads"
CAND_CACHE = DOWNLOADS / "_tmdb_candidates.json"
DETAILS_CACHE = DOWNLOADS / "_tmdb_details.json"

PROMOTE_THRESHOLD = 0.85
SLEEP = 0.1  # seconds between TMDB requests

# TMDB's image CDN. logo_path returned by /company/{id} is a leading-slash
# resource path; we prepend this to land at a valid logo_uri per schema.
TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p/original"


def sim(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def load_json(path: Path, default):
    if path.exists():
        with path.open() as f:
            return json.load(f)
    return default


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False))


def all_studio_files() -> list[Path]:
    return sorted(STUDIOS.glob("*/*/studio.json"))


def tmdb_search(name: str) -> list[dict[str, Any]]:
    import tmdbsimple as tmdb
    out, page = [], 1
    while True:
        r = tmdb.Search().company(query=name, page=page)
        out.extend(r.get("results", []))
        if page >= r.get("total_pages", 1) or page >= 3:
            break
        page += 1
    return out


def tmdb_details(company_id: int) -> dict[str, Any]:
    import tmdbsimple as tmdb
    return tmdb.Companies(company_id).info()


def rank(query: str, raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ranked = []
    for r in raw:
        ranked.append({
            "id": r.get("id"),
            "name": r.get("name"),
            "logo_path": r.get("logo_path"),
            "origin_country": r.get("origin_country"),
            "name_similarity": round(sim(query, r.get("name", "")), 3),
        })
    ranked.sort(key=lambda c: c["name_similarity"], reverse=True)
    return ranked


def phase_search(cache: dict, files: list[Path], save_every: int) -> dict:
    """Populate candidates only for studios that don't already carry a
    tmdb provider id.

    Name-based /search/company is unreliable for disambiguation (two
    real "ABC" studios sit at different ids; the top-ranked match isn't
    always the right one) and it's also slow at scale - ~250k network
    calls if we re-searched every studio every run. Since most entries
    were bulk-imported with their canonical TMDB id already set, the
    only ones that *need* a name search are studios that arrived
    without a provider id (hand-authored, branding-research import,
    pre-schema legacy data)."""
    pending = []
    for sf in files:
        slug = sf.parent.name
        if slug in cache and "candidates" in cache[slug]:
            continue
        data = json.loads(sf.read_text())
        if not isinstance(data, list):
            continue
        # Skip the whole file if any entry already names a tmdb provider -
        # phase_details will fetch /company/{id} for it directly.
        if any(find_provider(e.get("providers"), "tmdb") is not None
               for e in data if isinstance(e, dict)):
            continue
        for entry in data:
            if not isinstance(entry, dict):
                continue
            pending.append((slug, entry.get("name") or slug))
            break  # one query per slug
    print(f"  search: {len(pending)} slugs need TMDB candidates")
    for i, (slug, name) in enumerate(pending, 1):
        try:
            raw = tmdb_search(name)
        except Exception as e:
            cache[slug] = {"query": name, "candidates": [], "error": str(e)}
            continue
        ranked = rank(name, raw)
        cache[slug] = {
            "query": name,
            "candidates": ranked,
            "best": ranked[0] if ranked else None,
        }
        if i % 50 == 0:
            save_json(CAND_CACHE, cache)
            print(f"    ...{i}/{len(pending)}")
        time.sleep(SLEEP)
    save_json(CAND_CACHE, cache)
    return cache


def phase_details(cand: dict, details: dict, files: list[Path]) -> dict:
    """Fetch /company/{id} for every entry with a confirmed tmdb provider."""
    wanted: set[int] = set()
    for sf in files:
        slug = sf.parent.name
        data = json.loads(sf.read_text())
        for entry in data:
            tmdb_provider = find_provider(entry.get("providers"), "tmdb")
            tmdb_id = tmdb_provider.get("id") if tmdb_provider else None
            if not tmdb_id:
                # Promote from candidate cache if a high-similarity match exists
                rec = cand.get(slug)
                if rec and rec.get("candidates"):
                    top = rec["candidates"][0]
                    if top["name_similarity"] >= PROMOTE_THRESHOLD:
                        tmdb_id = top["id"]
            if tmdb_id:
                try:
                    wanted.add(int(tmdb_id))
                except (TypeError, ValueError):
                    pass

    to_fetch = sorted(i for i in wanted if str(i) not in details)
    print(f"  details: {len(wanted)} IDs in scope, {len(to_fetch)} new to fetch")
    for i, cid in enumerate(to_fetch, 1):
        try:
            info = tmdb_details(cid)
            details[str(cid)] = info
        except Exception as e:
            details[str(cid)] = {"_error": str(e)}
        if i % 50 == 0:
            save_json(DETAILS_CACHE, details)
            print(f"    ...{i}/{len(to_fetch)}")
        time.sleep(SLEEP)
    save_json(DETAILS_CACHE, details)
    return details


# Fields the merge can fill from TMDB. Anything in --overwrite that
# isn't in this set is rejected at CLI parse time. ``all`` is treated
# as a synonym for the whole set.
ENRICHABLE_FIELDS = (
    "description",
    "country",
    "homepage",
    "parent_company",
    "headquarters",
    "logo_uri",
)


def _should_overwrite(field: str, overwrite: set[str]) -> bool:
    return "all" in overwrite or field in overwrite


def merge_into_studios(cand: dict, details: dict, files: list[Path],
                       apply: bool, *,
                       overwrite: set[str] | None = None) -> dict:
    """Merge cached TMDB metadata into each studio.json. Every field is
    routed through a clean_* sanitiser before being written so blank/
    whitespace/garbage values from upstream never land on disk, and
    every modified file is validated against studios.schema.json before
    the write commits.

    By default a field is only set when the entry doesn't have one
    already. Pass ``overwrite={"description", "homepage", ...}`` (or
    ``{"all"}``) to replace existing values from the TMDB record."""
    overwrite = overwrite or set()
    stats = {"enriched": 0, "promoted_tmdb": 0, "placeholder_dropped": 0,
             "no_tmdb": 0, "shape_skipped": 0, "validation_failed": 0}
    for sf in files:
        slug = sf.parent.name
        try:
            data = json.loads(sf.read_text())
        except Exception as e:
            print(f"  ! parse {sf.relative_to(ROOT)}: {e}")
            continue
        if not isinstance(data, list):
            continue
        changed = False
        rel = sf.relative_to(ROOT)
        for entry in data:
            if not isinstance(entry, dict):
                continue
            providers = entry.setdefault("providers", [])
            if not isinstance(providers, list):
                # Old-format providers dict (or any other malformed shape).
                # Refuse to silently corrupt it - log loudly and skip.
                print(f"  ! {rel}: providers is {type(providers).__name__}, "
                      "expected list; skipping entry")
                stats["shape_skipped"] += 1
                continue

            # 1) Promote tmdb candidate to a real provider entry if we
            #    don't already have one.
            tmdb_provider = find_provider(providers, "tmdb")
            if tmdb_provider is None:
                rec = cand.get(slug)
                if rec and rec.get("candidates"):
                    top = rec["candidates"][0]
                    if top.get("name_similarity", 0) >= PROMOTE_THRESHOLD:
                        upsert_provider(entry, "tmdb", str(top["id"]))
                        stats["promoted_tmdb"] += 1
                        changed = True
                        tmdb_provider = find_provider(providers, "tmdb")

            if tmdb_provider is None:
                stats["no_tmdb"] += 1
                continue
            tmdb_id = tmdb_provider.get("id")

            info = details.get(str(tmdb_id))
            if not info or info.get("_error"):
                continue

            entry_pre = json.dumps(entry, sort_keys=True)

            # 2) description - top-level
            d = clean_str(info.get("description"))
            if d and (_should_overwrite("description", overwrite)
                      or not clean_str(entry.get("description"))):
                entry["description"] = d

            # 3) country - ISO-2, schema enforces ^[A-Z]{2}$
            c = clean_iso2(info.get("origin_country"))
            if c and (_should_overwrite("country", overwrite)
                      or not entry.get("country")):
                entry["country"] = c

            # 4) homepage - top-level, must look like a URL
            hp = clean_url(info.get("homepage"))
            if hp and (_should_overwrite("homepage", overwrite)
                       or not clean_url(entry.get("homepage"))):
                entry["homepage"] = hp

            # 5) parent_company - top-level string
            pc = info.get("parent_company")
            if isinstance(pc, dict):
                pc_name = clean_str(pc.get("name"))
                if pc_name and (_should_overwrite("parent_company", overwrite)
                                or not clean_str(entry.get("parent_company"))):
                    entry["parent_company"] = pc_name

            # 6) headquarters - top-level string
            hq = clean_str(info.get("headquarters"))
            if hq and (_should_overwrite("headquarters", overwrite)
                       or not clean_str(entry.get("headquarters"))):
                entry["headquarters"] = hq

            # 7) logo_uri on the tmdb provider entry. TMDB returns a
            #    leading-slash path; we prepend the CDN base so the
            #    result passes the schema's format=uri.
            lp = info.get("logo_path")
            if (isinstance(lp, str) and lp.startswith("/")
                    and (_should_overwrite("logo_uri", overwrite)
                         or not tmdb_provider.get("logo_uri"))):
                tmdb_provider["logo_uri"] = f"{TMDB_IMAGE_BASE}{lp}"

            entry_post = json.dumps(entry, sort_keys=True)
            if entry_pre != entry_post:
                stats["enriched"] += 1
                # Drop the placeholder flag if we filled real fields
                if entry.get("placeholder") and (
                    entry.get("description") or entry.get("country")
                    or entry.get("parent_company")
                ):
                    entry.pop("placeholder", None)
                    stats["placeholder_dropped"] += 1
                changed = True

        if not changed:
            continue

        # 8) Validate the proposed content against the repo schema.
        #    Refuse to write if it'd land in an invalid state - that's
        #    the safeguard that would have caught homepage=" ".
        errs = validate_entries(data, source_label=str(rel))
        if errs:
            stats["validation_failed"] += 1
            for e in errs:
                print(f"  ! {e}")
            continue

        if apply:
            sf.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    return stats


def _parse_overwrite(spec: str) -> set[str]:
    """Parse the --overwrite argument (comma-separated field list, or
    "all"). Empty string → empty set (default additive behavior)."""
    if not spec:
        return set()
    requested = {f.strip().lower() for f in spec.split(",") if f.strip()}
    if not requested:
        return set()
    if "all" in requested:
        return {"all"}
    unknown = requested - set(ENRICHABLE_FIELDS)
    if unknown:
        valid = ", ".join(sorted(ENRICHABLE_FIELDS)) + ", all"
        sys.exit(f"unknown --overwrite field(s): {sorted(unknown)}. "
                 f"Valid values: {valid}")
    return requested


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--apply", action="store_true",
                    help="Without --apply, no studio.json files are written.")
    ap.add_argument("--skip-search", action="store_true",
                    help="Don't run TMDB search (use existing candidates).")
    ap.add_argument("--skip-details", action="store_true",
                    help="Don't fetch /company/{id} (use existing details).")
    ap.add_argument("--only", metavar="SLUG", action="append", default=[],
                    help="Process only this studio slug (repeatable). "
                         "All TMDB calls and writes are scoped to the "
                         "matching folders.")
    ap.add_argument("--limit", type=int, default=0,
                    help="After --only filtering, cap processing at the "
                         "first N studios (0 = no cap). Useful for "
                         "smoke-testing a change without burning all "
                         "your TMDB budget.")
    ap.add_argument("--overwrite", metavar="FIELDS", default="",
                    help=f"Comma-separated fields whose existing value "
                         f"should be replaced when TMDB has one. "
                         f"Pass 'all' to overwrite every enrichable "
                         f"field. Default: additive only (existing "
                         f"values are preserved). "
                         f"Valid fields: {', '.join(ENRICHABLE_FIELDS)}.")
    args = ap.parse_args()

    # Argument parsing first, so typos in --overwrite explode before we
    # blow up on a missing tmdbsimple or TMDB_API_KEY (both of which
    # would otherwise mask the real error).
    overwrite = _parse_overwrite(args.overwrite)

    api_key = os.environ.get("TMDB_API_KEY")
    if not api_key:
        sys.exit("TMDB_API_KEY env var required")
    import tmdbsimple as tmdb
    tmdb.API_KEY = api_key

    files = all_studio_files()
    print(f"Studios on disk: {len(files)}")

    if args.only:
        wanted = set(args.only)
        before = len(files)
        files = [f for f in files if f.parent.name in wanted]
        missing = wanted - {f.parent.name for f in files}
        print(f"  --only filter: {len(files)} of {before} match "
              f"{len(wanted)} requested slug(s)")
        for slug in sorted(missing):
            print(f"  ! unknown slug: {slug}")
        if not files:
            sys.exit("no studios match --only; nothing to do")

    if args.limit and args.limit < len(files):
        files = files[: args.limit]
        print(f"  --limit applied: {len(files)} studios will be processed")

    if overwrite:
        target = "every enrichable field" if "all" in overwrite \
            else ", ".join(sorted(overwrite))
        print(f"  --overwrite: existing {target} will be replaced from TMDB")

    cand = load_json(CAND_CACHE, {})
    details = load_json(DETAILS_CACHE, {})
    print(f"Cache: {len(cand)} candidate lookups, {len(details)} detail records")

    if not args.skip_search:
        phase_search(cand, files, save_every=50)
    if not args.skip_details:
        phase_details(cand, details, files)

    stats = merge_into_studios(cand, details, files,
                               apply=args.apply, overwrite=overwrite)
    print(f"Merge stats: {stats}")
    if not args.apply:
        print("  (re-run with --apply to actually write studio.json files)")


if __name__ == "__main__":
    main()
