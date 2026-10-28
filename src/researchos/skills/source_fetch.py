"""Bounded fetching and readable-text extraction for public web pages.

Everything fetched here is untrusted input. The skill enforces the limits the
research workflow relies on: HTTP(S) only, public destinations only (every
redirect hop is re-validated), a per-call timeout, a bounded redirect count,
a response-size cap, a content-type allowlist, a per-host request interval and
``robots.txt``. It never sends credentials and does not try to get around
authentication, paywalls or rate limits.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
import threading
import time
from dataclasses import dataclass
from enum import Enum
from html.parser import HTMLParser
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse
from urllib.robotparser import RobotFileParser

import httpx

logger = logging.getLogger(__name__)

FETCH_TOOL_NAME = "fetch_source"
USER_AGENT = "ResearchOS/0.2 (+https://github.com/ProjectZero24/CtxFlow; research assistant)"
READABLE_CONTENT_TYPES = frozenset(
    {"text/html", "application/xhtml+xml", "text/plain"}
)
_ALLOWED_PORTS = frozenset({80, 443})
_REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})
_BLOCKED_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home.arpa")
_MIN_READABLE_CHARS = 80
_MAX_ROBOTS_BYTES = 128 * 1024


class FetchStatus(str, Enum):
    OK = "ok"
    BLOCKED = "blocked"  # refused by policy before or instead of fetching
    ERROR = "error"


@dataclass
class FetchOutcome:
    url: str
    status: FetchStatus
    final_url: str = ""
    title: str = ""
    text: str = ""
    content_type: str = ""
    bytes_read: int = 0
    error: str = ""
    error_type: str = ""
    elapsed_ms: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        """Serializable summary; the page body is intentionally excluded."""
        return {
            "url": self.url,
            "final_url": self.final_url,
            "status": self.status.value,
            "title": self.title,
            "content_type": self.content_type,
            "bytes_read": self.bytes_read,
            "text_chars": len(self.text),
            "error": self.error,
            "error_type": self.error_type,
            "elapsed_ms": round(self.elapsed_ms, 3),
        }


class _Blocked(Exception):
    def __init__(self, message: str, error_type: str = "Blocked") -> None:
        super().__init__(message)
        self.error_type = error_type


_TRACKING_PARAMS = frozenset({"fbclid", "gclid", "mc_cid", "mc_eid", "ref", "ref_src"})


def canonical_url(url: str) -> str:
    """Normalize a URL for identity comparison (not for fetching)."""
    parsed = urlparse((url or "").strip())
    if not parsed.netloc:
        return ""
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    try:
        port = parsed.port
    except ValueError:
        port = None
    netloc = host if port in (None, 80, 443) else f"{host}:{port}"
    query = urlencode(
        sorted(
            (k, v)
            for k, v in parse_qsl(parsed.query, keep_blank_values=True)
            if not k.lower().startswith("utm_") and k.lower() not in _TRACKING_PARAMS
        )
    )
    return urlunparse(("https", netloc, parsed.path.rstrip("/"), "", query, ""))


# ---------------------------------------------------------------------------
# URL safety
# ---------------------------------------------------------------------------

def _default_resolver(host: str) -> List[str]:
    infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return sorted({info[4][0] for info in infos})


def _is_public_ip(value: str) -> bool:
    try:
        return ipaddress.ip_address(value.split("%", 1)[0]).is_global
    except ValueError:
        return False


def check_public_url(
    url: str, resolver: Optional[Callable[[str], List[str]]] = None
) -> Optional[str]:
    """Return a refusal reason for *url*, or ``None`` when it may be fetched."""
    try:
        parsed = urlparse(url)
        port = parsed.port
    except ValueError:
        return "malformed URL"
    if parsed.scheme not in {"http", "https"}:
        return f"unsupported URL scheme {parsed.scheme!r}"
    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if not host:
        return "URL has no host"
    if parsed.username or parsed.password:
        return "URLs with embedded credentials are not allowed"
    if port is not None and port not in _ALLOWED_PORTS:
        return f"port {port} is not allowed"
    single_label = "." not in host and ":" not in host
    if host == "localhost" or host.endswith(_BLOCKED_SUFFIXES) or single_label:
        return f"host {host!r} is not a public host"

    try:
        ipaddress.ip_address(host)
        literal = True
    except ValueError:
        literal = False
    if literal:
        return None if _is_public_ip(host) else f"address {host} is not public"

    try:
        addresses = (resolver or _default_resolver)(host)
    except OSError as exc:
        return f"could not resolve {host}: {exc}"
    if not addresses:
        return f"could not resolve {host}"
    for address in addresses:
        if not _is_public_ip(address):
            return f"host {host!r} resolves to a non-public address"
    return None


# ---------------------------------------------------------------------------
# Readable-text extraction
# ---------------------------------------------------------------------------

_SKIP_TAGS = frozenset(
    {"script", "style", "noscript", "svg", "nav", "header", "footer", "aside",
     "form", "template", "iframe", "button", "select", "head"}
)
_BLOCK_TAGS = frozenset(
    {"p", "div", "br", "li", "ul", "ol", "tr", "table", "section", "article",
     "main", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre", "figcaption"}
)
_MAIN_TAGS = frozenset({"article", "main"})


class _ReadableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self._in_title = False
        self._skip_depth = 0
        self._main_depth = 0
        self._all: List[str] = []
        self._main: List[str] = []

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag == "title":
            self._in_title = True
        if tag in _SKIP_TAGS and tag != "head":
            self._skip_depth += 1
        if tag in _MAIN_TAGS:
            self._main_depth += 1
        if tag in _BLOCK_TAGS:
            self._break()

    def handle_startendtag(self, tag: str, attrs: Any) -> None:
        if tag in _BLOCK_TAGS:
            self._break()

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        if tag in _SKIP_TAGS and tag != "head" and self._skip_depth:
            self._skip_depth -= 1
        if tag in _MAIN_TAGS and self._main_depth:
            self._main_depth -= 1
        if tag in _BLOCK_TAGS:
            self._break()

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
            return
        if self._skip_depth:
            return
        self._all.append(data)
        if self._main_depth:
            self._main.append(data)

    def _break(self) -> None:
        if self._skip_depth:
            return
        self._all.append("\n")
        if self._main_depth:
            self._main.append("\n")

    @staticmethod
    def _clean(parts: List[str]) -> str:
        lines = (" ".join(line.split()) for line in "".join(parts).split("\n"))
        return "\n".join(line for line in lines if line)

    def result(self) -> Tuple[str, str]:
        main = self._clean(self._main)
        body = main if len(main) >= 200 else self._clean(self._all)
        return " ".join(self.title.split()), body


def extract_readable_text(html: str) -> Tuple[str, str]:
    """Return ``(title, text)`` for an HTML document using the stdlib parser."""
    parser = _ReadableParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception as exc:  # noqa: BLE001 — malformed markup must not crash the run
        logger.debug("HTML parse problem: %s", exc)
    return parser.result()


# ---------------------------------------------------------------------------
# Skill
# ---------------------------------------------------------------------------

class SourceFetchSkill:
    """Fetch one public page and return bounded readable text."""

    def __init__(
        self,
        *,
        timeout: float = 8.0,
        max_bytes: int = 1_000_000,
        max_redirects: int = 3,
        max_text_chars: int = 20_000,
        min_host_interval: float = 0.5,
        respect_robots: bool = True,
        transport: Optional[httpx.BaseTransport] = None,
        resolver: Optional[Callable[[str], List[str]]] = None,
    ) -> None:
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.max_redirects = max_redirects
        self.max_text_chars = max_text_chars
        self.min_host_interval = max(0.0, min_host_interval)
        self.respect_robots = respect_robots
        self._transport = transport
        self._resolver = resolver
        self._lock = threading.Lock()
        self._next_slot: Dict[str, float] = {}
        self._robots: Dict[str, Optional[RobotFileParser]] = {}

    # -- public API ------------------------------------------------------
    def fetch(self, url: str) -> FetchOutcome:
        started = time.monotonic()
        try:
            with httpx.Client(
                transport=self._transport,
                timeout=self.timeout,
                headers={"User-Agent": USER_AGENT, "Accept": "text/html,text/plain;q=0.9"},
                follow_redirects=False,
            ) as client:
                outcome = self._fetch(client, url)
        except _Blocked as exc:
            outcome = FetchOutcome(
                url=url, status=FetchStatus.BLOCKED, error=str(exc), error_type=exc.error_type
            )
        except httpx.TimeoutException as exc:
            outcome = FetchOutcome(
                url=url, status=FetchStatus.ERROR, error=f"timed out: {exc}"[:300],
                error_type="Timeout",
            )
        except Exception as exc:  # noqa: BLE001 — a fetch failure is an outcome, not a crash
            outcome = FetchOutcome(
                url=url, status=FetchStatus.ERROR, error=" ".join(str(exc).split())[:300],
                error_type=type(exc).__name__,
            )
        outcome.elapsed_ms = (time.monotonic() - started) * 1000.0
        return outcome

    # -- internals -------------------------------------------------------
    def _check(self, url: str) -> None:
        reason = check_public_url(url, self._resolver)
        if reason:
            raise _Blocked(reason, "BlockedURL")

    def _throttle(self, host: str) -> None:
        if self.min_host_interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            slot = max(now, self._next_slot.get(host, 0.0))
            self._next_slot[host] = slot + self.min_host_interval
        delay = slot - now
        if delay > 0:
            time.sleep(min(delay, self.timeout))

    def _fetch(self, client: httpx.Client, url: str) -> FetchOutcome:
        current = url
        for _ in range(self.max_redirects + 1):
            self._check(current)
            if self.respect_robots:
                self._check_robots(client, current)
            host = (urlparse(current).hostname or "").lower()
            self._throttle(host)
            with client.stream("GET", current) as response:
                if response.status_code in _REDIRECT_CODES:
                    location = response.headers.get("location")
                    if not location:
                        raise _Blocked("redirect without a Location header", "BadRedirect")
                    current = urljoin(current, location)
                    continue
                return self._read(response, url, current)
        raise _Blocked(f"more than {self.max_redirects} redirects", "TooManyRedirects")

    def _read(self, response: httpx.Response, url: str, final_url: str) -> FetchOutcome:
        if response.status_code >= 400:
            return FetchOutcome(
                url=url, final_url=final_url, status=FetchStatus.ERROR,
                error=f"HTTP {response.status_code}", error_type="HTTPStatus",
            )
        content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
        if content_type not in READABLE_CONTENT_TYPES:
            raise _Blocked(f"unsupported content type {content_type or 'unknown'!r}", "ContentType")
        declared = response.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > self.max_bytes:
            raise _Blocked(f"response exceeds {self.max_bytes} bytes", "TooLarge")

        body = bytearray()
        for chunk in response.iter_bytes():
            body.extend(chunk)
            if len(body) > self.max_bytes:
                raise _Blocked(f"response exceeds {self.max_bytes} bytes", "TooLarge")

        encoding = response.encoding or "utf-8"
        try:
            raw = bytes(body).decode(encoding, errors="replace")
        except LookupError:
            raw = bytes(body).decode("utf-8", errors="replace")
        if content_type == "text/plain":
            title, text = "", "\n".join(" ".join(l.split()) for l in raw.splitlines() if l.strip())
        else:
            title, text = extract_readable_text(raw)
        text = text[: self.max_text_chars]
        if len(text) < _MIN_READABLE_CHARS:
            return FetchOutcome(
                url=url, final_url=final_url, status=FetchStatus.ERROR,
                content_type=content_type, bytes_read=len(body),
                error="page has no readable text", error_type="NotReadable",
            )
        return FetchOutcome(
            url=url, final_url=final_url, status=FetchStatus.OK, title=title,
            text=text, content_type=content_type, bytes_read=len(body),
        )

    def _check_robots(self, client: httpx.Client, url: str) -> None:
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        with self._lock:
            known = origin in self._robots
            parser = self._robots.get(origin)
        if not known:
            parser = self._load_robots(client, origin)
            with self._lock:
                self._robots[origin] = parser
        if parser is None:
            return
        if not parser.can_fetch(USER_AGENT, url):
            raise _Blocked("disallowed by robots.txt", "RobotsDisallowed")

    def _load_robots(self, client: httpx.Client, origin: str) -> Optional[RobotFileParser]:
        """Return a parser, ``None`` when no robots file applies, or refuse."""
        robots_url = f"{origin}/robots.txt"
        self._check(robots_url)
        self._throttle(urlparse(origin).hostname or "")
        try:
            with client.stream("GET", robots_url) as response:
                if 400 <= response.status_code < 500:
                    return None
                if response.status_code >= 500:
                    raise _Blocked("robots.txt unavailable", "RobotsUnavailable")
                if response.status_code in _REDIRECT_CODES:
                    return None
                data = bytearray()
                for chunk in response.iter_bytes():
                    data.extend(chunk)
                    if len(data) > _MAX_ROBOTS_BYTES:
                        break
        except httpx.HTTPError as exc:
            raise _Blocked(f"robots.txt unavailable: {exc}", "RobotsUnavailable") from exc
        parser = RobotFileParser()
        parser.parse(bytes(data).decode("utf-8", errors="replace").splitlines())
        return parser
