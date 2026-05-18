#!/usr/bin/env python3
"""Enrich every studios/<slug>/studio.json with TMDB data.

Two-phase, both cached and resumable:

  1. SEARCH  — for every studio entry not already in
     downloads/_tmdb_candidates.json, hit /search/company and store all
     candidates. Promote the top match to providers.tmdb when name
     similarity >= PROMOTE_THRESHOLD.

  2. DETAILS — for every entry with a confirmed providers.tmdb (either
     promoted by phase 1 or already present from manual data), hit
     /company/{id} and store the full record in
     downloads/_tmdb_details.json. Then merge:
       - description  (only if entry has no description yet)
       - country      (only if missing; uppercased ISO2)
       - providers.homepage      (from TMDB homepage)
       - providers.tmdb_logo_path (TMDB-hosted PNG path)
       - parent_company (only if missing)
     The `placeholder: true` flag is removed once any real metadata gets
     filled in.

Usage:
  TMDB_API_KEY=xxxx python3 tmdb_enrich.py            # dry-run
  TMDB_API_KEY=xxxx python3 tmdb_enrich.py --apply
  TMDB_API_KEY=xxxx python3 tmdb_enrich.py --apply --skip-search
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

ROOT = Path(__file__).resolve().parent.parent
STUDIOS = ROOT / "studios"
DOWNLOADS = ROOT / "downloads"
CAND_CACHE = DOWNLOADS / "_tmdb_candidates.json"
DETAILS_CACHE = DOWNLOADS / "_tmdb_details.json"

PROMOTE_THRESHOLD = 0.85
SLEEP = 0.1  # seconds between TMDB requests


def sim(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def load_json(path: Path, default):
    if path.exists():
        with path.open() as f:
            return json.load(f)
    return default


def save_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False))


def all_studio_files() -> list[Path]:
    return sorted(STUDIOS.glob("*/studio.json"))


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
    """Populate candidates for any entry not yet in cache."""
    pending = []
    for sf in files:
        slug = sf.parent.name
        if slug in cache and "candidates" in cache[slug]:
            continue
        data = json.loads(sf.read_text())
        for entry in data:
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
    """Fetch /company/{id} for every entry with a confirmed providers.tmdb."""
    wanted: set[int] = set()
    for sf in files:
        slug = sf.parent.name
        data = json.loads(sf.read_text())
        for entry in data:
            tmdb_id = (entry.get("providers") or {}).get("tmdb")
            if not tmdb_id:
                # Promote from cache if possible
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


def merge_into_studios(cand: dict, details: dict, files: list[Path],
                       apply: bool) -> dict:
    stats = {"enriched": 0, "promoted_tmdb": 0, "placeholder_dropped": 0,
             "no_tmdb": 0}
    for sf in files:
        slug = sf.parent.name
        data = json.loads(sf.read_text())
        changed = False
        for entry in data:
            providers = entry.setdefault("providers", {})

            # 1) Promote tmdb id from candidates if missing
            if not providers.get("tmdb"):
                rec = cand.get(slug)
                if rec and rec.get("candidates"):
                    top = rec["candidates"][0]
                    if top["name_similarity"] >= PROMOTE_THRESHOLD:
                        providers["tmdb"] = str(top["id"])
                        stats["promoted_tmdb"] += 1
                        changed = True
                    # Always preserve full candidate list (already deduped)
                    if "tmdb_candidates" not in providers:
                        providers["tmdb_candidates"] = rec["candidates"]
                        changed = True

            tmdb_id = providers.get("tmdb")
            if not tmdb_id:
                stats["no_tmdb"] += 1
                continue

            info = details.get(str(tmdb_id))
            if not info or info.get("_error"):
                continue

            entry_pre = json.dumps(entry, sort_keys=True)

            # 2) Description (only if absent or empty)
            desc = (info.get("description") or "").strip()
            if desc and not entry.get("description"):
                entry["description"] = desc

            # 3) Country (only if absent)
            oc = (info.get("origin_country") or "").strip().upper()
            if oc and len(oc) == 2 and not entry.get("country"):
                entry["country"] = oc

            # 4) homepage / logo_path → providers
            hp = (info.get("homepage") or "").strip()
            if hp and not providers.get("homepage"):
                providers["homepage"] = hp
            lp = info.get("logo_path")
            if lp and not providers.get("tmdb_logo_path"):
                providers["tmdb_logo_path"] = lp

            # 5) parent_company
            pc = info.get("parent_company")
            if pc and isinstance(pc, dict) and pc.get("name") \
                    and not entry.get("parent_company"):
                entry["parent_company"] = pc.get("name")

            # 6) headquarters → store under providers for now
            hq = (info.get("headquarters") or "").strip()
            if hq and not providers.get("headquarters"):
                providers["headquarters"] = hq

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

        if changed and apply:
            sf.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    return stats


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="Without --apply, no studio.json files are written.")
    ap.add_argument("--skip-search", action="store_true",
                    help="Don't run TMDB search (use existing candidates).")
    ap.add_argument("--skip-details", action="store_true",
                    help="Don't fetch /company/{id} (use existing details).")
    args = ap.parse_args()

    api_key = os.environ.get("TMDB_API_KEY")
    if not api_key:
        sys.exit("TMDB_API_KEY env var required")
    import tmdbsimple as tmdb
    tmdb.API_KEY = api_key

    files = all_studio_files()
    print(f"Studios on disk: {len(files)}")

    cand = load_json(CAND_CACHE, {})
    details = load_json(DETAILS_CACHE, {})
    print(f"Cache: {len(cand)} candidate lookups, {len(details)} detail records")

    if not args.skip_search:
        phase_search(cand, files, save_every=50)
    if not args.skip_details:
        phase_details(cand, details, files)

    stats = merge_into_studios(cand, details, files, apply=args.apply)
    print(f"Merge stats: {stats}")
    if not args.apply:
        print("  (re-run with --apply to actually write studio.json files)")


if __name__ == "__main__":
    main()
