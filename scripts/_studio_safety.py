"""Shared input-sanitisation and schema-check helpers for the scripts
that mutate studios/<bucket>/<slug>/studio.json (tmdb_enrich,
import_tmdb_companies, ...).

The motivating regression: a TMDB record with ``homepage: " "`` (a
single space) flowed through tmdb_enrich.py unchanged because the
inline ``.strip()`` happened in one spot but the truthiness check after
it accepted the empty string. Centralising the cleanup avoids a
sprinkling of subtly different sanitisers across the scripts.

A secondary regression this guards against is the shape of
``providers``: the schema declares it as ``[{provider_name, id,
logo_uri?}]`` but older scripts treated it as ``{tmdb: "...", ...}``,
which would silently corrupt every entry on the next run.
``find_provider`` / ``upsert_provider`` make the array layout the only
way callers can interact with the list.
"""
from __future__ import annotations

import functools
import json
import re
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / ".github" / "studios.schema.json"


# ─── field-level sanitisers ───────────────────────────────────────────

def clean_str(v: Any) -> str | None:
    """Strip whitespace; return None for empty, blank, or non-string."""
    if not isinstance(v, str):
        return None
    s = v.strip()
    return s or None


def clean_url(v: Any) -> str | None:
    """clean_str + require an http(s):// prefix. The schema's
    ``format: uri`` rejects bare strings like "example.com" or " ".

    IDN hostnames are IDNA-encoded to Punycode so the result stays
    pure ASCII - AJV's strict RFC3986 ``uri`` format (used in CI)
    rejects IRIs even though they read fine in a browser."""
    s = clean_str(v)
    if not s:
        return None
    if not re.match(r"^https?://", s, re.IGNORECASE):
        return None
    if not s.isascii():
        from urllib.parse import urlsplit, urlunsplit
        try:
            parts = urlsplit(s)
            host = parts.hostname or ""
            ascii_host = host.encode("idna").decode("ascii")
        except (UnicodeError, ValueError):
            return None
        netloc = ascii_host
        if parts.port is not None:
            netloc = f"{ascii_host}:{parts.port}"
        s = urlunsplit(
            (parts.scheme, netloc, parts.path, parts.query, parts.fragment)
        )
        if not s.isascii():
            return None
    return s


def clean_iso2(v: Any) -> str | None:
    """clean_str + uppercase + enforce the schema's ``^[A-Z]{2}$``."""
    s = clean_str(v)
    if not s:
        return None
    s = s.upper()
    return s if re.fullmatch(r"[A-Z]{2}", s) else None


# ─── providers array helpers ──────────────────────────────────────────

def find_provider(providers: Any, name: str) -> dict | None:
    """Return the dict for ``name`` in a providers array, or None.
    Tolerates malformed input (returns None) - callers should then
    refuse to write that entry rather than corrupt it further."""
    if not isinstance(providers, list):
        return None
    for p in providers:
        if isinstance(p, dict) and p.get("provider_name") == name:
            return p
    return None


def upsert_provider(entry: dict, name: str, provider_id: str,
                    logo_uri: str | None = None) -> bool:
    """Add or update the ``name`` provider in ``entry["providers"]``.

    Returns True if anything actually changed. Refuses to touch
    ``entry`` if its ``providers`` exists but isn't a list - that's a
    shape regression that should be flagged loudly rather than papered
    over. Caller is expected to inspect the return value plus the
    entry's current shape if it cares about that distinction.
    """
    providers = entry.setdefault("providers", [])
    if not isinstance(providers, list):
        return False
    p = find_provider(providers, name)
    pid = str(provider_id)
    if p is None:
        new: dict[str, Any] = {"provider_name": name, "id": pid}
        if logo_uri:
            new["logo_uri"] = logo_uri
        providers.append(new)
        return True
    changed = False
    if p.get("id") != pid:
        p["id"] = pid
        changed = True
    if logo_uri and not p.get("logo_uri"):
        p["logo_uri"] = logo_uri
        changed = True
    return changed


# ─── schema validation ────────────────────────────────────────────────

@functools.lru_cache(maxsize=1)
def _validator():
    """Returns a Draft7Validator for studios.schema.json, or None if the
    jsonschema package is not installed (caller-controlled degrade).

    A custom ``uri`` format checker is registered because the stock
    jsonschema FormatChecker silently passes every ``format: uri``
    string unless ``rfc3987`` is also installed - shipping that extra
    dep just to detect blank/scheme-less URIs isn't worth it. The
    rejection rule here is intentionally aligned with what AJV (the
    CI validator) rejects so anything that passes locally also passes
    the GitHub workflow."""
    try:
        from jsonschema import Draft7Validator, FormatChecker
    except ImportError:
        return None
    schema = json.loads(SCHEMA_PATH.read_text())
    checker = FormatChecker()

    @checker.checks("uri", raises=ValueError)
    def _check_uri(value: Any) -> bool:
        if not isinstance(value, str):
            return True  # the type-level check handles non-string
        if value != value.strip() or not value:
            raise ValueError("blank or padded URI")
        if not value.isascii():
            raise ValueError(
                "non-ASCII URI (IDN host must be Punycode-encoded)"
            )
        if not re.match(r"^[a-z][a-z0-9+.\-]*:", value, re.IGNORECASE):
            raise ValueError("missing scheme")
        return True

    return Draft7Validator(schema, format_checker=checker)


def validate_entries(entries: Any, *, source_label: str = "<unknown>") -> list[str]:
    """Validate a list of studio entries against the repo schema.
    Returns a list of human-readable error strings (empty when valid,
    or when jsonschema is not installed). Callers should refuse to
    write the file when this returns a non-empty list."""
    v = _validator()
    if v is None:
        return []
    if not isinstance(entries, list):
        return [f"{source_label}: top-level must be an array"]
    errors: list[str] = []
    for err in v.iter_errors(entries):
        loc = "/".join(str(p) for p in err.absolute_path) or "<root>"
        errors.append(f"{source_label}: {loc}: {err.message}")
    return errors
