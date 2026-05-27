# Scripts

| Script | Purpose |
| --- | --- |
| `build_release.py` | Full build pipeline: lint, copy, svgo, webp, flatten, bundle placeholders, zip. |
| `generate_thumbs.py` | Build `thumb.svg`/`primary.svg` from a `logo.svg` using the templates and WCAG contrast picker. |
| `rescale_svgs.py` | Crop a `logo.svg` to its content bbox and normalise the longer dimension to 1024 px. |
| `import_tmdb_companies.py` | Bulk-import TMDB production companies as placeholder studios. Requires PyICU. |
| `enrich_tmdb_info.py` | Fill description, country, homepage, logo URI, etc. on existing studios from TMDB. |
| `_studio_safety.py` | Shared input sanitisers + a `studios.schema.json` validator used before writes. |

## Python dependencies

Most scripts need just the stdlib. A few extras:

```sh
python3 -m pip install svgelements Pillow jsonschema
```

`import_tmdb_companies.py` additionally needs **PyICU** so it can slug non-Latin company names through Jellyfin's exact rule chain (Han → pinyin, Cyrillic → BGN, etc.). It will print an install hint on startup if PyICU is missing.
