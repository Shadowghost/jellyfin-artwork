<h1 align="center">Jellyfin Artwork Repository</h1>
<h3 align="center">Part of the <a href="https://jellyfin.org">Jellyfin Project</a></h3>

---

<p align="center">
<img alt="Logo Banner" src="https://raw.githubusercontent.com/jellyfin/jellyfin-ux/master/branding/SVG/banner-logo-solid.svg?sanitize=true"/>
</p>

---

Source for the artwork bundle served by Jellyfin: logos, thumbs, and primary cards for studios, networks, music labels, and genres.
Consumed by the [Jellyfin Artwork Plugin](https://github.com/jellyfin/jellyfin-plugin-artwork).

## Repository layout

Studios live under `studios/<bucket>/<slug>/`, where `<bucket>` is the first character of the slug (`a`–`z`, `0`–`9`).

```
studios/
  a/
    abc/
      logo.svg
      primary.svg
      thumb.svg
      studio.json
    abc2/
      ...
  z/
    zylon-pictures/
      studio.json     # placeholder only - no per-studio artwork
templates/
  thumb.svg              # 16:9 reference overlay (640x360)
  primary.svg            # 1:1 reference overlay (360x360)
  placeholder-thumb.svg  # generic placeholder shipped at the release root
  placeholder-primary.svg
scripts/                 # build, generation, and TMDB enrichment tooling
.github/
  studios.schema.json    # source of truth for studio.json shape
```

Each `studio.json` is an array of entries. Required fields: `name`, `providers` (array of `{provider_name, id, logo_uri?}`), and `artwork` (map of slot → `["svg", "webp"]`, empty when the studio is a placeholder).
Slugs (folder names) are derived from the name via Jellyfin's project-wide ICU rule chain: `Any-Latin; Latin-ASCII; Lower; NFD; [:Nonspacing Mark:] Remove; NFC`.
The full schema lives in [`.github/studios.schema.json`](.github/studios.schema.json).

## Release bundle

`scripts/build_release.py` produces `dist/release.zip` containing:

| Path                            | Purpose |
| ---                             | --- |
| `studios.json`                  | Flattened manifest, one entry per studio, stamped with its `slug`. |
| `studios/<bucket>/<slug>/*.{svg,webp}` | Per-studio artwork (omitted for placeholder entries). |
| `placeholder-thumb.{svg,webp}`  | Generic 16:9 fallback for entries with `placeholder: true`. |
| `placeholder-primary.{svg,webp}`| Generic 1:1 fallback. |

Consumers find a studio's artwork at `studios/<slug[0]>/<slug>/<slot>.<ext>` using the `slug` from `studios.json`. When `placeholder: true` (or the file is missing), fall back to the bundle-root placeholders.

## Adding a studio

----

> **Licensing**
> Any logo or artwork added to this repository must come from a source that is either in the **public domain** or licensed under a **Creative Commons** license that permits redistribution.
> Do not submit logos scraped from a studio's website, ripped from streaming apps, or otherwise of unclear provenance - they get rejected on review and have to be removed.
> When in doubt, link the source of the file or a brand's guidelines regarding its usage in your pull request so a reviewer can confirm the license.

----

1. Pick a slug - kebab-case ASCII of the studio name (run it through ICU if the name is in a non-Latin script).
2. Create `studios/<slug[0]>/<slug>/` with at minimum a `logo.svg`.
3. Generate template-aligned `thumb.svg` and `primary.svg`:

   ```sh
   python3 scripts/generate_thumbs.py --aspect 16x9 --write --only <slug>
   python3 scripts/generate_thumbs.py --aspect 1x1  --write --only <slug>
   ```

4. Open `studios/<slug[0]>/<slug>/studio.json` and add at least `name`, `providers`, and `artwork`. See existing entries for the full shape; the schema enforces details (URI format on `homepage`, ISO‑2 country codes, etc.).

## Building locally

The same pipeline runs in CI.

```sh
python3 scripts/build_release.py             # dist/release.zip + dist/studios.json
python3 scripts/build_release.py --skip-zip  # iterate without re-packing
```

Native toolchain:

```sh
brew install librsvg webp svgo   # macOS
# or
sudo apt-get install -y librsvg2-bin webp && npm install -g svgo   # Linux
```

## Scripts

See [`scripts/README.md`](scripts/README.md) for the tooling reference (per-script purpose and Python dependencies).
