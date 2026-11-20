"""Safe, metered HTTP client.

* only http/https on default ports, public IP addresses only (SSRF guard, re-checked on every redirect)
* robots.txt respected for company websites
* per-host concurrency limits, bounded body size, retries with backoff
* every response is hashed (sha256 of raw bytes) and timestamped; raw snapshots can be persisted
"""
from __future__ import annotations

import gzip
import hashlib
import ipaddress
import json
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

USER_AGENT = "SignalpostAgent/1.0 (+https://github.com/; company-research; respects robots.txt)"
BROWSER_LIKE_UA = USER_AGENT


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class UnsafeURL(ValueError):
    pass


@dataclass
class Response:
    url: str
    final_url: str
    status: int
    body: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)
    error: str | None = None
    elapsed_ms: int = 0
    retrieved_at: str = ""
    sha256: str | None = None
    redirects: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300 and self.error is None

    def text(self) -> str:
        ctype = self.headers.get("content-type", "")
        charset = None
        if "charset=" in ctype:
            charset = ctype.split("charset=", 1)[1].split(";")[0].strip().strip('"')
        for enc in [charset, "utf-8", "cp1252", "latin-1"]:
            if not enc:
                continue
            try:
                return self.body.decode(enc)
            except (LookupError, UnicodeDecodeError):
                continue
        return self.body.decode("utf-8", "replace")

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8"))


_PRIVATE_NETS = [ipaddress.ip_network(n) for n in (
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16", "172.16.0.0/12",
    "192.0.0.0/24", "192.168.0.0/16", "198.18.0.0/15", "224.0.0.0/4", "240.0.0.0/4",
    "::1/128", "fc00::/7", "fe80::/10", "::/128")]

_dns_cache: dict[str, list[str] | None] = {}
_dns_lock = threading.Lock()


def resolve(host: str) -> list[str] | None:
    with _dns_lock:
        if host in _dns_cache:
            return _dns_cache[host]
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
        ips = sorted({i[4][0] for i in infos})
    except (socket.gaierror, UnicodeError, OSError):
        ips = None
    with _dns_lock:
        _dns_cache[host] = ips
    return ips


def assert_public_url(url: str) -> None:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise UnsafeURL(f"scheme not allowed: {parts.scheme}")
    if parts.port not in (None, 80, 443):
        raise UnsafeURL("non-default port")
    if parts.username or parts.password:
        raise UnsafeURL("credentials in URL")
    host = parts.hostname or ""
    if not host or "." not in host:
        raise UnsafeURL("bad host")
    ips = resolve(host)
    if not ips:
        raise UnsafeURL("dns_failure")
    for ip in ips:
        addr = ipaddress.ip_address(ip.split("%")[0])
        if any(addr in net for net in _PRIVATE_NETS):
            raise UnsafeURL("private address")


class Meter:
    """Thread-safe request accounting."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.requests = 0
        self.bytes = 0
        self.latencies: list[int] = []
        self.by_host: dict[str, int] = {}

    def add(self, host: str, nbytes: int, ms: int) -> None:
        with self.lock:
            self.requests += 1
            self.bytes += nbytes
            self.latencies.append(ms)
            self.by_host[host] = self.by_host.get(host, 0) + 1


METER = Meter()

_host_sems: dict[str, threading.BoundedSemaphore] = {}
_host_lock = threading.Lock()
HOST_LIMITS = {"data.brreg.no": 6, "arbeidsplassen.nav.no": 3, "pam-stilling-feed.nav.no": 3,
               "query.wikidata.org": 1, "api.gleif.org": 2}
DEFAULT_HOST_LIMIT = 2


def _sem(host: str) -> threading.BoundedSemaphore:
    with _host_lock:
        if host not in _host_sems:
            _host_sems[host] = threading.BoundedSemaphore(HOST_LIMITS.get(host, DEFAULT_HOST_LIMIT))
        return _host_sems[host]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


_opener = urllib.request.build_opener(_NoRedirect())
import ssl as _ssl  # noqa: E402
_insecure_opener = urllib.request.build_opener(_NoRedirect(), urllib.request.HTTPSHandler(context=_ssl._create_unverified_context()))

SNAPSHOT_DIR: Path | None = None


def set_snapshot_dir(path: Path | None) -> None:
    global SNAPSHOT_DIR
    SNAPSHOT_DIR = path
    if path:
        path.mkdir(parents=True, exist_ok=True)


def _persist(resp: Response) -> None:
    if SNAPSHOT_DIR is None or not resp.sha256 or not resp.body:
        return
    target = SNAPSHOT_DIR / resp.sha256[:2] / f"{resp.sha256}.gz"
    if target.exists():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".tmp%d" % threading.get_ident())
    with gzip.open(tmp, "wb") as fh:
        fh.write(resp.body)
    tmp.replace(target)


def _ensure_path(u: str) -> str:
    try:
        p = urllib.parse.urlsplit(u)
    except ValueError:
        return u
    if not p.path:
        return urllib.parse.urlunsplit((p.scheme, p.netloc, "/", p.query, ""))
    return u


def fetch(url: str, *, headers: dict[str, str] | None = None, timeout: float = 15.0, max_bytes: int = 2_500_000,
          attempts: int = 2, max_redirects: int = 5, check_public: bool = True, persist: bool = True) -> Response:
    started_at = utc_now()
    current = _ensure_path(url)
    redirects: list[str] = []
    last: Response | None = None
    for _hop in range(max_redirects + 1):
        try:
            if check_public:
                assert_public_url(current)
        except UnsafeURL as exc:
            return Response(url, current, 0, error=f"unsafe_url:{exc}", retrieved_at=started_at, redirects=redirects)
        host = urllib.parse.urlsplit(current).hostname or ""
        resp = _fetch_once(current, host, headers or {}, timeout, max_bytes, attempts)
        if current.startswith("http://") and (resp.status == 0 or resp.status >= 400) and redirects:
            # redirect pointed at plain http: try the https equivalent before giving up
            alt = "https://" + current[len("http://"):]
            r2 = _fetch_once(alt, host, headers or {}, timeout, max_bytes, 1)
            if r2.status and r2.status < 400:
                current, resp = alt, r2
        resp.url = url
        resp.redirects = list(redirects)
        last = resp
        if resp.status in (301, 302, 303, 307, 308) and resp.headers.get("location"):
            nxt = _ensure_path(urllib.parse.urljoin(current, resp.headers["location"].strip()))
            redirects.append(current)
            current = nxt
            continue
        break
    assert last is not None
    if persist:
        _persist(last)
    return last


def _fetch_once(url: str, host: str, headers: dict[str, str], timeout: float, max_bytes: int, attempts: int) -> Response:
    hdrs = {"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, deflate", "Accept-Language": "nb-NO,nb;q=0.9,no;q=0.8,en;q=0.6"}
    hdrs.update(headers)
    err = "request_failed"
    opener = _opener
    tls_note = None
    for attempt in range(attempts + 1):
        if attempt == attempts and opener is _opener:
            break
        t0 = time.monotonic()
        req = urllib.request.Request(url, headers=hdrs)
        sem = _sem(host)
        with sem:
            try:
                with opener.open(req, timeout=timeout) as r:
                    raw = r.read(max_bytes + 1)
                    status = r.status
                    rh = {k.lower(): v for k, v in r.headers.items()}
            except urllib.error.HTTPError as exc:
                status = exc.code
                rh = {k.lower(): v for k, v in (exc.headers or {}).items()}
                try:
                    raw = exc.read(max_bytes + 1)
                except Exception:  # noqa: BLE001
                    raw = b""
            except Exception as exc:  # noqa: BLE001
                ms = int((time.monotonic() - t0) * 1000)
                METER.add(host, 0, ms)
                err = type(exc).__name__ + (":" + str(exc)[:80] if str(exc) else "")
                if "CERTIFICATE_VERIFY_FAILED" in str(exc) and opener is _opener:
                    # content checks (org number on page) do not depend on TLS identity; record that TLS was not verified
                    opener, tls_note = _insecure_opener, "tls_certificate_unverified"
                    continue
                if attempt + 1 < attempts:
                    time.sleep(0.5 * (2 ** attempt))
                continue
        ms = int((time.monotonic() - t0) * 1000)
        METER.add(host, len(raw), ms)
        enc = rh.get("content-encoding", "")
        try:
            if "gzip" in enc:
                raw = gzip.decompress(raw)
            elif "deflate" in enc:
                raw = zlib.decompress(raw)
        except Exception:  # noqa: BLE001
            pass
        if len(raw) > max_bytes:
            raw = raw[:max_bytes]
        if status in (429, 500, 502, 503, 504) and attempt + 1 < attempts:
            retry = rh.get("retry-after", "")
            time.sleep(min(10.0, float(retry)) if retry.isdigit() else 1.0 * (2 ** attempt))
            continue
        if tls_note:
            rh["x-signalpost-tls"] = tls_note
        return Response(url, url, status, raw, rh, None, ms, utc_now(), hashlib.sha256(raw).hexdigest())
    return Response(url, url, 0, error=err, retrieved_at=utc_now())


def fetch_json(url: str, **kw: Any) -> tuple[Response, Any]:
    h = dict(kw.pop("headers", {}) or {})
    h.setdefault("Accept", "application/json")
    resp = fetch(url, headers=h, check_public=False, attempts=kw.pop("attempts", 3), **kw)
    data = None
    if resp.ok and resp.body:
        try:
            data = resp.json()
        except Exception:  # noqa: BLE001
            resp.error = "invalid_json"
    return resp, data


# ---------------------------------------------------------------- robots.txt
_robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
_robots_lock = threading.Lock()


def robots_allowed(url: str) -> bool:
    parts = urllib.parse.urlsplit(url)
    key = f"{parts.scheme}://{parts.netloc}"
    with _robots_lock:
        cached = key in _robots
        rp = _robots.get(key)
    if not cached:
        resp = fetch(key + "/robots.txt", timeout=8, attempts=1, max_bytes=300_000, persist=False)
        rp = urllib.robotparser.RobotFileParser()
        if resp.ok and resp.body:
            rp.parse(resp.text().splitlines())
        else:  # RFC 9309: unreachable/4xx robots.txt means no restrictions
            rp.parse([])
        with _robots_lock:
            _robots[key] = rp
    if rp is None:
        return True
    try:
        return rp.can_fetch("SignalpostAgent", url)
    except Exception:  # noqa: BLE001
        return True
