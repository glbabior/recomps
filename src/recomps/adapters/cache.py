"""A cache for fetched pages.

Two reasons this is not optional.

**Politeness.** A re-run within the cache window costs the source nothing. The
alternative is refetching dozens of pages from the same handful of hosts every
time someone tries a slightly different question, which is exactly the behavior
a site is entitled to block.

**Iteration.** Debugging an extraction prompt against live pages is slow, costs
money, and gives a moving target. With a warm cache the pages are fixed and the
only variable is the code.

The key includes an adapter version, so fixing an adapter's parsing does not
keep reading pages captured under the old assumptions -- bump the version and
the old entries are ignored rather than deleted, which keeps them available for
comparison.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path

CACHE_DIRNAME = "http-cache"
DEFAULT_TTL_SECONDS = 6 * 60 * 60  # six hours: long enough for one sitting


@dataclass
class CacheEntry:
    url: str
    status: int
    text: str
    fetched_at: float
    adapter_version: str = "1"
    content_type: str = ""

    @property
    def age_seconds(self) -> float:
        return max(0.0, time.time() - self.fetched_at)

    def is_fresh(self, ttl_seconds: float) -> bool:
        return self.age_seconds < ttl_seconds


class PageCache:
    """A plain directory of JSON files, one per fetched URL."""

    def __init__(
        self,
        directory: str | Path,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        enabled: bool = True,
    ) -> None:
        self.directory = Path(directory)
        self.ttl_seconds = ttl_seconds
        self.enabled = enabled
        self.hits = 0
        self.misses = 0
        self.writes = 0

    def _path(self, url: str, adapter_version: str) -> Path:
        digest = hashlib.sha256(f"{adapter_version}\x00{url}".encode()).hexdigest()
        # Two levels of fan-out so a long run does not put ten thousand files
        # in one directory.
        return self.directory / digest[:2] / f"{digest}.json"

    def get(self, url: str, adapter_version: str = "1") -> CacheEntry | None:
        if not self.enabled:
            return None
        path = self._path(url, adapter_version)
        if not path.is_file():
            self.misses += 1
            return None
        try:
            entry = CacheEntry(**json.loads(path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, TypeError, OSError):
            # A corrupt entry is a miss, not a crash.
            self.misses += 1
            return None
        if not entry.is_fresh(self.ttl_seconds):
            self.misses += 1
            return None
        self.hits += 1
        return entry

    def put(self, entry: CacheEntry) -> None:
        if not self.enabled:
            return
        path = self._path(entry.url, entry.adapter_version)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write then move, so an interrupted run cannot leave a half-written
        # entry that later reads as a corrupt cache hit.
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(asdict(entry)), encoding="utf-8")
        temporary.replace(path)
        self.writes += 1

    def clear(self) -> int:
        """Delete every cached page. Returns how many were removed."""
        if not self.directory.is_dir():
            return 0
        removed = 0
        for path in self.directory.rglob("*.json"):
            path.unlink()
            removed += 1
        return removed

    @property
    def summary(self) -> str:
        total = self.hits + self.misses
        rate = f"{self.hits / total:.0%}" if total else "n/a"
        return f"{self.hits} hit / {self.misses} miss ({rate}), {self.writes} written"
