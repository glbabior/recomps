"""Fetching pages, politely.

The fetching happens here rather than on someone else's servers for one reason:
the project's rules -- rate limit, respect robots.txt, identify honestly, never
evade a block -- can only be *enforced* where the requests are actually made.
Handing a URL to a remote fetching service means hoping it behaves; doing it
here means knowing it does.

Four rules, all of them non-negotiable:

**Identify honestly.** The user agent says what the tool is and links to the
project. No pretending to be a browser. A site that wants to refuse this tool
must be able to recognize it, and a site that blocks it has answered.

**Ask robots.txt first.** Every URL is checked against the host's rules before
it is requested, and a disallowed path is simply not fetched. Robots files are
themselves cached so checking them is not its own flood of requests.

**One request at a time per host.** Workers run in parallel across the whole
run, but a per-host lock plus a configurable delay means any single site sees a
polite, serial trickle no matter how wide the fan-out.

**Fail soft.** A refusal, a timeout, a block: all of them return a result that
says so and let the run continue with less data. None of them raises. A partial
dataset that reports its gaps is worth more than a crash, and far more than a
dataset that hides them.

What this module will not do: retry a 403 with different headers, rotate user
agents, solve a captcha, or route around a block. A block is an answer.
"""

from __future__ import annotations

import threading
import time
import urllib.robotparser
from dataclasses import dataclass, field
from urllib.parse import urlparse

from recomps.adapters.cache import CacheEntry, PageCache

#: Identifies the tool and points at the project. Deliberately not a browser.
DEFAULT_USER_AGENT = (
    "REComps/0.1 (real-estate comp research; "
    "+https://github.com/gbabior/recomps)"
)
DEFAULT_DELAY_SECONDS = 3.0
DEFAULT_TIMEOUT_SECONDS = 30.0
#: Statuses worth one retry: transient server trouble, not a refusal.
RETRYABLE = frozenset({429, 500, 502, 503, 504})


@dataclass
class FetchResult:
    """What came back, including the ways it did not."""

    url: str
    status: int | None = None
    text: str = ""
    from_cache: bool = False
    blocked_by_robots: bool = False
    error: str = ""
    elapsed: float = 0.0

    @property
    def ok(self) -> bool:
        return self.status is not None and 200 <= self.status < 300 and bool(self.text)

    @property
    def refused(self) -> bool:
        """The host declined to serve us. Not something to work around."""
        return self.blocked_by_robots or self.status in (401, 403, 405, 451)

    def describe(self) -> str:
        if self.blocked_by_robots:
            return f"{self.url} -- disallowed by robots.txt, not fetched"
        if self.error:
            return f"{self.url} -- {self.error}"
        if self.status and not self.ok:
            note = " (host declined)" if self.refused else ""
            return f"{self.url} -- HTTP {self.status}{note}"
        return f"{self.url} -- ok{' (cached)' if self.from_cache else ''}"


@dataclass
class HostState:
    lock: threading.Lock = field(default_factory=threading.Lock)
    last_request: float = 0.0


class RobotsPolicy:
    """Per-host robots.txt, fetched once and remembered."""

    def __init__(self, user_agent: str, timeout: float = 15.0) -> None:
        self.user_agent = user_agent
        self.timeout = timeout
        self._parsers: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self._lock = threading.Lock()

    def _parser(self, base: str) -> urllib.robotparser.RobotFileParser | None:
        with self._lock:
            if base in self._parsers:
                return self._parsers[base]
        parser = urllib.robotparser.RobotFileParser()
        parser.set_url(f"{base}/robots.txt")
        try:
            parser.read()
        except Exception:
            # A robots.txt we cannot read is not permission to ignore it, but
            # it is also not a rule. Treat the host as unrestricted and let its
            # actual responses speak -- a host that means "no" answers 403.
            parser = None
        with self._lock:
            self._parsers[base] = parser
        return parser

    def allows(self, url: str) -> bool:
        parts = urlparse(url)
        if not parts.scheme or not parts.netloc:
            return False
        parser = self._parser(f"{parts.scheme}://{parts.netloc}")
        if parser is None:
            return True
        return parser.can_fetch(self.user_agent, url)

    def crawl_delay(self, url: str) -> float | None:
        """A host's own requested delay, which always outranks our default."""
        parts = urlparse(url)
        parser = self._parser(f"{parts.scheme}://{parts.netloc}")
        if parser is None:
            return None
        try:
            delay = parser.crawl_delay(self.user_agent)
        except Exception:
            return None
        return float(delay) if delay else None


class PoliteFetcher:
    """Fetches pages while behaving itself."""

    def __init__(
        self,
        cache: PageCache,
        user_agent: str = DEFAULT_USER_AGENT,
        delay_seconds: float = DEFAULT_DELAY_SECONDS,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        respect_robots: bool = True,
        max_retries: int = 1,
    ) -> None:
        self.cache = cache
        self.user_agent = user_agent
        self.delay_seconds = delay_seconds
        self.timeout = timeout
        self.respect_robots = respect_robots
        self.max_retries = max_retries
        self.robots = RobotsPolicy(user_agent)
        self._hosts: dict[str, HostState] = {}
        self._hosts_lock = threading.Lock()
        self._client = None
        self.requests_made = 0

    # -- politeness --------------------------------------------------------

    def _host_state(self, url: str) -> HostState:
        host = urlparse(url).netloc
        with self._hosts_lock:
            if host not in self._hosts:
                self._hosts[host] = HostState()
            return self._hosts[host]

    def _wait_turn(self, url: str, state: HostState) -> None:
        """Hold this host's slot open for the required gap."""
        delay = max(self.delay_seconds, self.robots.crawl_delay(url) or 0.0)
        elapsed = time.monotonic() - state.last_request
        if state.last_request and elapsed < delay:
            time.sleep(delay - elapsed)
        state.last_request = time.monotonic()

    # -- fetching ----------------------------------------------------------

    def _http_client(self):
        if self._client is None:
            import httpx2 as httpx

            self._client = httpx.Client(
                headers={
                    "User-Agent": self.user_agent,
                    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
                    "Accept-Language": "en-US,en;q=0.9",
                },
                timeout=self.timeout,
                follow_redirects=True,
            )
        return self._client

    def fetch(self, url: str, adapter_version: str = "1") -> FetchResult:
        cached = self.cache.get(url, adapter_version)
        if cached is not None:
            return FetchResult(
                url=url, status=cached.status, text=cached.text, from_cache=True
            )

        if self.respect_robots and not self.robots.allows(url):
            return FetchResult(url=url, blocked_by_robots=True)

        state = self._host_state(url)
        started = time.monotonic()
        # The lock is held across the wait and the request, so two workers can
        # never overlap on one host however wide the fan-out is.
        with state.lock:
            self._wait_turn(url, state)
            result = self._request(url)
        result.elapsed = time.monotonic() - started

        if result.ok:
            self.cache.put(
                CacheEntry(
                    url=url,
                    status=result.status or 0,
                    text=result.text,
                    fetched_at=time.time(),
                    adapter_version=adapter_version,
                )
            )
        return result

    def _request(self, url: str) -> FetchResult:
        client = self._http_client()
        attempts = self.max_retries + 1
        last = FetchResult(url=url)
        for attempt in range(attempts):
            try:
                self.requests_made += 1
                response = client.get(url)
            except Exception as exc:
                last = FetchResult(url=url, error=f"{type(exc).__name__}: {exc}")
            else:
                last = FetchResult(
                    url=url, status=response.status_code, text=response.text
                )
                if response.status_code not in RETRYABLE:
                    return last
            if attempt + 1 < attempts:
                # One backoff, then give up. This is a research tool, not a
                # crawler; hammering a struggling host is the thing to avoid.
                time.sleep(self.delay_seconds * (attempt + 2))
        return last

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def __enter__(self) -> PoliteFetcher:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
