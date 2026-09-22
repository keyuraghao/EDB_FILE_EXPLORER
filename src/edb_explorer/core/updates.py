"""Help ▸ Check for updates: compare the running version with the latest GitHub release (on request only)."""

from __future__ import annotations

import json
import re
import urllib.request

RELEASES_API = "https://api.github.com/repos/keyuraghao/EDB_FILE_EXPLORER/releases/latest"
RELEASES_PAGE = "https://github.com/keyuraghao/EDB_FILE_EXPLORER/releases"


def parse_version(text: str) -> tuple[int, ...]:
    """``'v0.7.1'`` -> ``(0, 7, 1)``; non-numeric suffixes are ignored."""
    return tuple(int(n) for n in re.findall(r"\d+", text)[:3]) or (0,)


def check_latest_release(current: str, timeout: float = 8.0) -> tuple[str, str, bool]:
    """Return ``(latest tag, release url, newer_than_current)``; raises on network / API errors."""
    req = urllib.request.Request(
        RELEASES_API, headers={"Accept": "application/vnd.github+json", "User-Agent": "edb-explorer"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    tag = str(data.get("tag_name") or "")
    url = str(data.get("html_url") or RELEASES_PAGE)
    return tag.lstrip("v"), url, parse_version(tag) > parse_version(current)
