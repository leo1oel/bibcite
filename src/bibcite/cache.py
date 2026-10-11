"""Local cache of successful publication matches.

Keyed by normalized title. Only *published* matches are stored — a paper that
is published stays published, while a preprint may get published tomorrow, so
negative/preprint results are never cached. Re-running `fix`/`upgrade` or
re-adding known papers therefore costs zero API calls.

A hit is a shortcut, never an authority: callers re-check a cached record
against the identity they asked for before using it.

Disable with --no-cache or BIBCITE_NO_CACHE=1. Lives at
$XDG_CACHE_HOME/bibcite/published.json (~/.cache/bibcite/published.json).
"""

import json
import os
import sys
from pathlib import Path

DISABLED = os.environ.get("BIBCITE_NO_CACHE", "") == "1"

# Records are only as trustworthy as the matching rules that wrote them, and
# they never expire. Bump this when a rule changes so a fix also retires the
# wrong answers the old rule already stored on every machine.
NAMESPACE = "v2"


def _path() -> Path:
    root = os.environ.get("XDG_CACHE_HOME") or "~/.cache"
    return Path(root).expanduser() / "bibcite" / "published.json"


def _load() -> dict:
    try:
        return json.loads(_path().read_text())
    except Exception:
        return {}


def get(key: str) -> dict | None:
    if DISABLED or not key:
        return None
    return _load().get(f"{NAMESPACE}:{key}")


def put(key: str, value: dict):
    if DISABLED or not key:
        return
    try:
        data = _load()
        data[f"{NAMESPACE}:{key}"] = value
        p = _path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, ensure_ascii=False))
    except Exception as e:  # cache must never break resolution
        print(f"[cache] write failed: {e}", file=sys.stderr)
