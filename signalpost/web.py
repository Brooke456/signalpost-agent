"""Official-website discovery, verification and company-owned extraction.

A website is only published when it resolves back to the exact legal entity:
  * the organisation number is printed on the site (strongest), or
  * the company itself declared the domain to an org-number-keyed register (Enhetsregisteret
    `hjemmeside`/`epostadresse`, NAV employer record) AND the site does not contradict it
    (parked page, or a different organisation number with no name match), or
  * a guessed / third-party candidate is corroborated by the organisation number or the registry phone.
Everything else is reported as `ambiguous` or `not_available`.
"""
from __future__ import annotations

import json
import re
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

from bs4 import BeautifulSoup

from .http import Response, fetch, resolve, robots_allowed
from .registry import core_name, fold, norm_name

FREE_MAIL = {
    "gmail.com", "googlemail.com", "hotmail.com", "hotmail.no", "outlook.com", "outlook.no", "live.com", "live.no", "msn.com",
    "yahoo.com", "yahoo.no", "icloud.com", "me.com", "mac.com", "online.no", "frisurf.no", "start.no", "broadpark.no",
    "getmail.no", "c2i.net", "chello.no", "lyse.net", "altibox.no", "tele2.no", "telia.no", "nextgentel.com", "ngt.no",
    "haugnett.no", "bluezone.no", "ebnett.no", "enivest.net", "mail.com", "protonmail.com", "proton.me", "aol.com",
    "gmx.com", "gmx.net", "tdcadsl.no", "loqal.no", "sensewave.com", "post.com", "epost.no", "kpnmail.no", "tussa.com",
    "neasonline.no", "hesbynett.no", "vkt.no", "frisurf.com", "netcom.no", "eunet.no", "vikenfiber.no", "rl.telia.no",
    "email.com", "yandex.com", "live.se", "hotmail.se", "gmail.no", "icloud.no", "ymail.com", "rocketmail.com",
}

PARKED_MARKERS = [
    "domain is for sale", "domenet er til salgs", "this domain may be for sale", "buy this domain", "kjøp dette domenet",
    "parked free", "domain parking", "parkert domene", "domenet er registrert", "this domain is registered",
    "website coming soon", "under construction", "under konstruksjon", "kommer snart", "domeneshop", "hostinger",
    "registrert hos one.com", "web hosting by", "default web site page", "it works!", "index of /", "welcome to nginx",
    "this site can’t be reached", "account suspended", "sedo", "dan.com", "afternic", "godaddy",
]
# markers that must appear AND page be tiny to count as parked
WEAK_PARKED = {"domeneshop", "hostinger", "sedo", "godaddy", "kommer snart", "under construction", "under konstruksjon",
               "website coming soon", "domenet er registrert", "this domain is registered", "registrert hos one.com"}

SOCIAL_HOSTS = {
    "facebook.com": "facebook", "fb.com": "facebook", "instagram.com": "instagram", "linkedin.com": "linkedin",
    "youtube.com": "youtube", "youtu.be": None, "twitter.com": "x", "x.com": "x", "tiktok.com": "tiktok",
    "pinterest.com": "pinterest", "vimeo.com": "vimeo", "github.com": "github", "threads.net": "threads",
    "snapchat.com": "snapchat",
}
SOCIAL_BLOCK_PATHS = re.compile(r"^/(sharer|share|sharing|intent|plugins|dialog|hashtag|home|watch|embed|tr|login|signup|p/|reel|stories|explore|search|groups/?$|events|privacy|policies|legal|help|about|terms|in/|pub/|feed|posts|photo|photos|video|videos|story\.php|permalink\.php|profile\.php\?(?!id=))", re.I)
GENERIC_HANDLES = {"facebook", "instagram", "linkedin", "youtube", "twitter", "x", "tiktok", "wix", "squarespace", "wordpress",
                   "shopify", "webflow", "google", "pages", "company", "user", "channel", "c", "home", "tr", "share"}

PRIORITY_PATTERNS = [
    (re.compile(r"kontakt|contact", re.I), 10),
    (re.compile(r"om-oss|om_oss|omoss|/om/?$|about|hvem-er-vi|historie|history|company|selskapet|firma", re.I), 9),
    (re.compile(r"personvern|privacy|cookie|vilkar|vilkår|terms|salgsbetingelser|kjopsvilkar|kjøpsvilkår|impressum", re.I), 7),
    (re.compile(r"karriere|career|jobb|jobs|ledige|stilling|vacanc|join-us|bli-med", re.I), 8),
    (re.compile(r"nyheter|news|aktuelt|blogg|blog|presse|press|artikler|media", re.I), 6),
    (re.compile(r"ledelse|leadership|management|team|ansatte|people|styret|board|medarbeidere|folka", re.I), 5),
    (re.compile(r"investor|ir/|ir$", re.I), 4),
]
CAREER_RE = re.compile(r"karriere|career|ledige[- ]stilling|ledig[- ]stilling|jobb[- ]hos|jobbe[- ]hos|/jobs?\b|/jobb\b|vacanc|stillinger", re.I)
NEWS_RE = re.compile(r"nyheter|news|aktuelt|blogg|blog|presse", re.I)

ORG_CONTEXT = re.compile(r"(org\.?\s*(nr|nummer|no)|organisasjonsn|foretaksregisteret|mva|vat|business\s*reg|company\s*(no|number|reg)|reg\.?\s*nr)", re.I)
NINE = re.compile(r"(?<![\d])(?:NO\s?)?(\d{3}[\s\u00a0.]?\d{3}[\s\u00a0.]?\d{3})(?![\d])")


def mod11_ok(n: str) -> bool:
    if len(n) != 9 or not n.isdigit():
        return False
    w = [3, 2, 7, 6, 5, 4, 3, 2]
    s = sum(int(a) * b for a, b in zip(n[:8], w))
    r = 11 - s % 11
    r = 0 if r == 11 else r
    return r != 10 and r == int(n[8])


def find_org_numbers(text: str) -> tuple[set[str], set[str]]:
    """Return (all valid-looking org numbers, those with an org-number context nearby)."""
    allv: set[str] = set()
    ctx: set[str] = set()
    for m in NINE.finditer(text):
        n = re.sub(r"\D", "", m.group(1))
        if not mod11_ok(n):
            continue
        allv.add(n)
        window = text[max(0, m.start() - 60): m.end() + 20]
        if ORG_CONTEXT.search(window) or m.group(0).upper().startswith("NO"):
            ctx.add(n)
    return allv, ctx


def org_span(text: str, org: str) -> str | None:
    pat = re.compile(r"(?<!\d)" + r"[\s\u00a0.]?".join([org[0:3], org[3:6], org[6:9]]) + r"(?!\d)")
    m = pat.search(text)
    if not m:
        return None
    return re.sub(r"\s+", " ", text[max(0, m.start() - 80): m.end() + 40]).strip()


def registered_domain(host: str) -> str:
    host = (host or "").lower().strip(".")
    if host.startswith("www."):
        host = host[4:]
    parts = host.split(".")
    if len(parts) >= 3 and parts[-2] in {"co", "com", "org", "net", "gov", "ac"} and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def normalize_url(value: str | None) -> str | None:
    if not value:
        return None
    v = value.strip().strip("/").replace(" ", "")
    if not v or "@" in v and "/" not in v:
        return None
    if not re.match(r"^https?://", v, re.I):
        v = "https://" + v
    try:
        p = urllib.parse.urlsplit(v)
    except ValueError:
        return None
    if not p.hostname or "." not in p.hostname:
        return None
    return f"{p.scheme.lower()}://{p.hostname.lower()}{p.path or '/'}"


def phone_digits(p: str | None) -> str | None:
    if not p:
        return None
    d = re.sub(r"\D", "", p)
    if d.startswith("0047"):
        d = d[4:]
    elif d.startswith("47") and len(d) == 10:
        d = d[2:]
    return d if len(d) == 8 else None


# ---------------------------------------------------------------- page model
@dataclass
class Page:
    url: str
    final_url: str
    status: int
    sha256: str | None
    retrieved_at: str
    title: str = ""
    description: str = ""
    site_name: str = ""
    text: str = ""
    lang: str = ""
    links: list[tuple[str, str]] = field(default_factory=list)  # (abs url, anchor text)
    jsonld: list[dict[str, Any]] = field(default_factory=list)
    feeds: list[str] = field(default_factory=list)
    times: list[str] = field(default_factory=list)
    tel: list[str] = field(default_factory=list)
    mail: list[str] = field(default_factory=list)
    soup: Any = None


def parse_page(resp: Response) -> Page:
    final = resp.redirects[-1] if False else resp.final_url
    page = Page(resp.url, resp.headers.get("x-final-url", final), resp.status, resp.sha256, resp.retrieved_at)
    ctype = resp.headers.get("content-type", "")
    if "html" not in ctype and not resp.body.lstrip()[:200].lower().startswith((b"<!doctype", b"<html", b"<?xml", b"<head")):
        return page
    html = resp.text()
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:  # noqa: BLE001  (malformed namespaced markup can crash the lxml tree builder)
        try:
            soup = BeautifulSoup(html, "html.parser")
        except Exception:  # noqa: BLE001
            return page
    page.soup = soup
    if soup.title and soup.title.string:
        page.title = re.sub(r"\s+", " ", soup.title.string).strip()[:200]
    for m in soup.find_all("meta"):
        k = (m.get("name") or m.get("property") or "").lower()
        v = (m.get("content") or "").strip()
        if not v:
            continue
        if k in ("description", "og:description") and not page.description:
            page.description = re.sub(r"\s+", " ", v)[:500]
        if k == "og:site_name" and not page.site_name:
            page.site_name = v[:120]
    h = soup.find("html")
    page.lang = (h.get("lang") if h else "") or ""
    for s in soup.find_all("script", type=re.compile("ld\\+json", re.I)):
        try:
            data = json.loads(s.string or s.get_text() or "")
        except Exception:  # noqa: BLE001
            continue
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, dict):
                page.jsonld.append(node)
                if "@graph" in node:
                    stack.append(node["@graph"])
    for l in soup.find_all("link"):
        t = (l.get("type") or "").lower()
        rel = " ".join(l.get("rel") or []).lower()
        if "alternate" in rel and ("rss" in t or "atom" in t) and l.get("href"):
            page.feeds.append(urllib.parse.urljoin(resp.final_url, l["href"]))
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith("tel:"):
            page.tel.append(href[4:])
            continue
        if href.lower().startswith("mailto:"):
            page.mail.append(href[7:].split("?")[0].strip().lower())
            continue
        if href.startswith(("javascript:", "#")):
            continue
        absu = urllib.parse.urljoin(resp.final_url, href)
        page.links.append((absu, re.sub(r"\s+", " ", a.get_text(" ", strip=True))[:80]))
    for t in soup.find_all("time"):
        if t.get("datetime"):
            page.times.append(t["datetime"])
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()
    page.text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))[:200_000]
    return page


def fetch_page(url: str) -> tuple[Page | None, Response | None, str | None]:
    if not robots_allowed(url):
        return None, None, "robots_disallowed"
    resp = fetch(url, headers={"Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5"}, timeout=12, attempts=2)
    if resp.error:
        return None, resp, resp.error
    # record final URL after redirects
    resp.final_url = resp.final_url
    if resp.status >= 400:
        return None, resp, f"http_{resp.status}"
    return parse_page(resp), resp, None


def fetch_site_home(url: str) -> tuple[Page | None, Response | None, str | None]:
    """Fetch homepage following redirects; returns parsed page with final URL."""
    p, r, err = fetch_page(url)
    if err and err != "robots_disallowed" and not err.startswith("unsafe_url"):
        parts = urllib.parse.urlsplit(url)
        host = parts.hostname or ""
        alts = []
        if not host.startswith("www."):
            alts.append(f"https://www.{host}{parts.path or '/'}")
        alts.append(f"http://{host}{parts.path or '/'}")
        for alt in alts:
            p2, r2, err2 = fetch_page(alt)
            if not err2:
                return p2, r2, None
    return p, r, err


# ---------------------------------------------------------------- site verification
@dataclass
class SiteResult:
    candidate: str
    origin: str
    domain: str = ""
    final_url: str = ""
    pages: list[Page] = field(default_factory=list)
    error: str | None = None
    org_found: bool = False
    org_evidence: tuple[str, str] | None = None  # (page url, span)
    other_orgs: set[str] = field(default_factory=set)
    name_match: bool = False
    phone_match: bool = False
    address_match: bool = False
    email_match: bool = False
    parked: bool = False
    verdict: str = "not_checked"  # verified | accepted_registry | ambiguous | rejected | unreachable
    confidence: float = 0.0
    reason: str = ""


def _name_match(ident: dict[str, Any], page: Page, domain: str) -> bool:
    core = core_name(ident.get("name") or "")
    if not core:
        return False
    compact = core.replace(" ", "").replace("&", "og")
    label = registered_domain(domain).split(".")[0].replace("-", "")
    if len(compact) >= 4 and (compact == label or (len(label) >= 5 and label in compact and len(label) >= 0.6 * len(compact))):
        return True
    hay = fold(" ".join([page.title, page.site_name, page.text[:5000]]))
    hay = re.sub(r"[^a-z0-9& ]+", " ", hay)
    toks = [t for t in core.split() if len(t) >= 3]
    if not toks:
        return False
    return f" {core} " in f" {re.sub(r'\s+', ' ', hay)} " if len(core) >= 5 else False


def _is_parked(page: Page) -> bool:
    t = (page.title + " " + page.text[:3000]).lower()
    if len(page.text) < 1200:
        if any(m in t for m in PARKED_MARKERS):
            return True
        if len(page.text) < 40 and not page.links:
            return True
    strong = [m for m in PARKED_MARKERS if m not in WEAK_PARKED]
    return any(m in t for m in strong[:6])


def priority_links(page: Page, domain: str, limit: int = 6) -> list[str]:
    scored: dict[str, int] = {}
    for u, txt in page.links:
        try:
            p = urllib.parse.urlsplit(u)
        except ValueError:
            continue
        if p.scheme not in ("http", "https") or registered_domain(p.hostname or "") != domain:
            continue
        if re.search(r"\.(pdf|jpe?g|png|gif|zip|docx?|xlsx?|mp4|svg|webp)$", p.path, re.I):
            continue
        clean = urllib.parse.urlunsplit((p.scheme, p.netloc, p.path or "/", "", ""))
        if clean.rstrip("/") == page.final_url.rstrip("/"):
            continue
        hay = p.path + " " + txt
        best = 0
        for pat, sc in PRIORITY_PATTERNS:
            if pat.search(hay):
                best = max(best, sc)
        if best:
            scored[clean] = max(scored.get(clean, 0), best)
    ranked = sorted(scored.items(), key=lambda kv: (-kv[1], len(kv[0])))
    out: list[str] = []
    # first one page per category (contact, about, terms/privacy, careers, news, ...), then a second of each
    for rnd in (1, 2):
        seen_cat: dict[int, int] = {}
        for u, sc in ranked:
            if u in out:
                continue
            if seen_cat.get(sc, 0) >= 1:
                continue
            if rnd == 2 or not any(sc == s2 for s2 in seen_cat):
                seen_cat[sc] = seen_cat.get(sc, 0) + 1
                out.append(u)
            if len(out) >= limit:
                return out
    return out


def check_site(candidate: str, origin: str, ident: dict[str, Any], *, max_pages: int = 6, deep: bool = True, shared: int = 0) -> SiteResult:
    org = ident["organisation_number"]
    res = SiteResult(candidate=candidate, origin=origin)
    res.shared = shared  # type: ignore[attr-defined]
    home, resp, err = fetch_site_home(candidate)
    if err or home is None:
        res.error = err or "no_html"
        res.verdict = "blocked" if err == "robots_disallowed" else "unreachable"
        return res
    res.final_url = resp.final_url if resp else candidate
    host = urllib.parse.urlsplit(res.final_url).hostname or ""
    res.domain = registered_domain(host)
    res.pages.append(home)
    res.parked = _is_parked(home)
    reg_phone = phone_digits(ident.get("phone"))
    ba = ident.get("business_address") or {}
    street = fold((ba.get("adresse") or [""])[0]).strip() if isinstance(ba, dict) else ""
    street = street if len(street) >= 6 and re.search(r"\d", street) else ""
    pnr = (ba.get("postnummer") or "") if isinstance(ba, dict) else ""
    reg_email = (ident.get("email") or "").strip().lower()
    if "@" not in reg_email or reg_email.rsplit("@", 1)[1] in FREE_MAIL:
        reg_email = ""

    def scan(pg: Page) -> None:
        if not res.org_found:
            sp = org_span(pg.text, org)
            if sp is None:
                # also check raw jsonld / footer attributes
                blob = json.dumps(pg.jsonld, ensure_ascii=False)
                sp = org_span(blob, org)
            if sp:
                res.org_found = True
                res.org_evidence = (pg.final_url or pg.url, sp)
        _all, ctx = find_org_numbers(pg.text)
        res.other_orgs |= {n for n in ctx if n != org}
        if reg_phone and not res.phone_match:
            digits = re.sub(r"\D", "", pg.text)
            tels = "".join(re.sub(r"\D", "", t) for t in pg.tel)
            if reg_phone in tels or re.search(r"(?<!\d)" + r"[\s\u00a0.-]?".join(reg_phone) + r"(?!\d)", pg.text):
                res.phone_match = True
            elif reg_phone in digits and len(reg_phone) == 8 and pg.text.count(reg_phone[:2]) and False:
                res.phone_match = True
        if not res.name_match:
            res.name_match = _name_match(ident, pg, res.domain)
        if not res.email_match and reg_email and reg_email.endswith("@" + res.domain) and (reg_email in pg.mail or reg_email in pg.text.lower()):
            res.email_match = True
        if not res.address_match and street and pnr:
            t = fold(pg.text)
            if pnr in t and street in re.sub(r"\s+", " ", t):
                res.address_match = True

    scan(home)
    if deep and not res.parked:
        for link in priority_links(home, res.domain, limit=max_pages - 1):
            pg, r, e = fetch_page(link)
            if pg is None:
                continue
            h2 = urllib.parse.urlsplit(r.final_url if r else link).hostname or ""
            if registered_domain(h2) != res.domain:
                continue
            res.pages.append(pg)
            scan(pg)
    _decide(res, ident)
    return res


def label_matches_name(ident: dict[str, Any], domain: str) -> bool:
    core = core_name(ident.get("name") or "")
    toks = [t for t in core.replace("&", " ").split() if t not in ("og", "and", "i")]
    if not toks:
        return False
    label = registered_domain(domain).split(".")[0].replace("-", "")
    compact = "".join(toks)
    if len(label) < 4:
        return False
    return label == compact or (len(label) >= 5 and (label in compact and len(label) >= 0.6 * len(compact)))


LANG_PATH = re.compile(r"^/((no|nb|nn|en|se|sv|da|de)([-_][a-z]{2})?/?)*((index|home|forside|no|en)(\.html?|\.php)?)?/?$", re.I)


def is_section_of_shared_site(url: str, domain: str, ident: dict[str, Any]) -> bool:
    path = urllib.parse.urlsplit(url).path or "/"
    if LANG_PATH.match(path):
        return False
    return not label_matches_name(ident, domain)


def _decide(res: SiteResult, ident: dict[str, Any]) -> None:
    o = res.origin
    shared = getattr(res, "shared", 0)
    if any(res.domain == d or res.domain.endswith("." + d) for d in SOCIAL_HOSTS) or res.domain in ("wix.com", "wixsite.com", "squarespace.com", "proff.no", "gulesider.no", "1881.no", "finn.no"):
        res.verdict, res.reason = "rejected", f"candidate resolves to a third-party platform ({res.domain}), not a company-owned site"
        return
    if res.parked and not res.org_found:
        res.verdict, res.reason = "rejected", "parked or placeholder page"
        return
    label_ok = label_matches_name(ident, res.domain)
    if res.org_found:
        if o == "name_guess" and len(res.other_orgs) >= 3 and not (res.name_match or label_ok):
            res.verdict = "rejected"
            res.reason = f"page lists {len(res.other_orgs) + 1} organisation numbers (directory or group listing), not a company-owned site"
            return
        res.verdict, res.confidence = "verified", 0.99
        res.reason = "organisation number printed on the site"
        home = res.pages[0] if res.pages else None
        on_home = bool(res.org_evidence and home and res.org_evidence[0] in (home.final_url, home.url))
        if not on_home and not (res.name_match or label_ok) and not (home and _name_match(ident, home, res.domain)):
            # e.g. a store page on a chain / franchise domain: publish only that page, not the domain's own profiles
            res.chain_page = True  # type: ignore[attr-defined]
            res.reason = "organisation number printed on a company page of a shared chain/franchise site"
        return
    contradict = bool(res.other_orgs) and not res.name_match and not label_ok
    if o in ("registry_declared", "registry_subunit_declared", "nav_employer_homepage"):
        if contradict:
            res.verdict, res.reason = "ambiguous", f"site shows other organisation number(s) {sorted(res.other_orgs)[:3]} and not this company's"
            return
        if shared > 1 and not (label_ok or res.name_match):
            res.verdict, res.reason = "ambiguous", f"domain is declared by {shared} registered entities (brand, franchise or group site) and the site does not name this company"
            return
        if res.name_match or label_ok:
            res.verdict, res.confidence = "accepted_registry", 0.96
            res.reason = "domain declared by the company to an org-number-keyed register; company name corroborated on site"
        elif res.phone_match:
            res.verdict, res.confidence = "accepted_registry", 0.94
            res.reason = "domain declared by the company to an org-number-keyed register; registry phone found on site"
        else:
            res.verdict, res.confidence = "accepted_registry", 0.9
            res.reason = "domain declared by the company to an org-number-keyed register; no contradiction on site"
        return
    if o == "registry_email_domain":
        if contradict:
            res.verdict, res.reason = "ambiguous", "e-mail domain site shows other organisation numbers"
            return
        if shared > 3 and not label_ok:
            res.verdict, res.reason = "ambiguous", f"e-mail domain used by {shared} registered entities (accountant, employer or group domain)"
            return
        if res.name_match or label_ok and (res.phone_match or res.name_match):
            res.verdict, res.confidence = "accepted_registry", 0.93
            res.reason = "registry e-mail domain; the site names this company"
            return
        res.verdict, res.reason = "ambiguous", "registry e-mail domain but the site does not name this company (could be an employer/accountant/parent domain)"
        return
    # third-party or guessed candidates need hard corroboration
    if (res.phone_match or res.address_match or res.email_match) and res.name_match and not res.other_orgs:
        res.verdict, res.confidence = "verified", 0.95
        res.reason = "company name and registry " + ("phone number" if res.phone_match else "business address" if res.address_match else "e-mail address") + " both found on site"
        return
    res.verdict, res.reason = "rejected", "no organisation number (or legal name + registry phone) on site"


# ---------------------------------------------------------------- candidates
GENERIC_WORDS = {"holding", "holdings", "invest", "investment", "investering", "eiendom", "eiendommer", "consult", "consulting",
                 "service", "services", "og", "and", "i", "norge", "norway", "norsk", "norske", "group", "gruppen", "drift",
                 "utvikling", "capital", "kapital", "management", "as", "asa", "partners", "partner", "prosjekt", "ii", "iii",
                 "iv", "1", "2", "3", "the", "nordic", "nord", "sor", "vest", "ost", "avd", "avdeling", "co", "company",
                 "selskap", "selskapet", "firma", "handel", "bygg", "transport", "regnskap", "tjenester", "media", "data",
                 "it", "film", "design", "eiendomsselskap", "naering", "naeringseiendom", "byggservice", "maskin"}


COMPOUND_SUFFIXES = ("produkter", "produksjon", "service", "senteret", "senter", "sentral", "entreprenor", "entrepenor", "transport",
                     "utleie", "handel", "tjenester", "teknikk", "verksted", "forretning", "industri", "byggservice")


def _alt_fold(text: str) -> list[str]:
    """Two Norwegian transliterations: ø→o/æ→ae/å→a (the usual one) and ø→oe/å→aa."""
    a = norm_name(text)
    low = (text or "").lower().replace("ø", "oe").replace("å", "aa").replace("æ", "ae")
    b = norm_name(low)
    return [a] if a == b else [a, b]


def domain_labels(name: str) -> list[str]:
    """Candidate domain labels for a company / brand name, most specific first."""
    from .registry import LEGAL_SUFFIX
    out: list[str] = []
    for folded in _alt_fold(LEGAL_SUFFIX.sub(" ", name or "")):
        toks = [t for t in folded.replace("&", " ").split() if t not in ("og", "and")]
        toks = [t for t in toks if t not in ("as", "asa", "avd", "avdeling")]
        if not toks or len(toks) > 6:
            continue
        if all(t in GENERIC_WORDS or len(t) <= 2 for t in toks):
            continue
        core = [t for t in toks if t not in GENERIC_WORDS]
        variants = ["".join(toks), "-".join(toks)]
        if core and core != toks:
            variants += ["".join(core), "-".join(core)]
        if len(toks) >= 3:
            variants += ["".join(toks[:2]), "-".join(toks[:2])]
        last = toks[-1]
        for suf in COMPOUND_SUFFIXES:
            if last.endswith(suf) and len(last) - len(suf) >= 4:
                stem = toks[:-1] + [last[: -len(suf)]]
                variants += ["".join(stem), "-".join(stem)]
                if stem[-1].endswith("s") and len(stem[-1]) >= 6:
                    stem2 = stem[:-1] + [stem[-1][:-1]]
                    variants += ["".join(stem2)]
                break
        if len(toks) >= 2 and len(toks[0]) >= 5 and toks[0] not in GENERIC_WORDS and not toks[0].isdigit():
            variants.append(toks[0])
        for v in variants:
            if 4 <= len(v) <= 40 and v not in out and not v.isdigit():
                out.append(v)
    return out


def guess_domains(ident: dict[str, Any], limit: int = 4, extra_names: list[str] | None = None) -> list[str]:
    """Resolve candidate .no/.com domains built from the legal name and the company's own registered names
    (distinct sub-unit names, earlier legal names). Candidates are only hypotheses: each one must print the
    organisation number, or the legal name together with the registry phone, address or e-mail, before it is published."""
    labels: list[str] = []
    for n in [ident.get("name") or ""] + list(extra_names or []):
        for l in domain_labels(n):
            if l not in labels:
                labels.append(l)
    out: list[str] = []
    for l in labels[:12]:
        for tld in (".no", ".com"):
            d = l + tld
            if resolve(d) or resolve("www." + d):
                out.append("https://" + d + "/")
            if len(out) >= limit:
                return out
    return out


# ---------------------------------------------------------------- extraction from a verified site
def social_profiles(res: SiteResult, ident: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return (accepted, ambiguous) company-owned social profile links."""
    found: dict[tuple[str, str], dict[str, Any]] = {}
    for pg in res.pages:
        for u, txt in pg.links:
            s = normalize_social(u)
            if s:
                key = (s["platform"], s["handle"].lower())
                if key not in found:
                    found[key] = {**s, "found_on": pg.final_url or pg.url, "anchor": txt, "page_sha256": pg.sha256,
                                  "retrieved_at": pg.retrieved_at}
        for node in pg.jsonld:
            same = node.get("sameAs") if isinstance(node, dict) else None
            if isinstance(same, str):
                same = [same]
            for u in same or []:
                s = normalize_social(str(u)) if isinstance(u, str) else None
                if s:
                    key = (s["platform"], s["handle"].lower())
                    found.setdefault(key, {**s, "found_on": pg.final_url or pg.url, "anchor": "JSON-LD sameAs",
                                           "page_sha256": pg.sha256, "retrieved_at": pg.retrieved_at})
    by_platform: dict[str, list[dict[str, Any]]] = {}
    for v in found.values():
        by_platform.setdefault(v["platform"], []).append(v)
    accepted, ambiguous = [], []
    core = core_name(ident.get("name") or "").replace(" ", "")
    dom_label = res.domain.split(".")[0].replace("-", "")
    for plat, items in by_platform.items():
        if len(items) == 1:
            accepted.append(items[0])
            continue
        def score(it: dict[str, Any]) -> int:
            h = re.sub(r"[^a-z0-9]", "", fold(it["handle"]))
            return int(bool(h) and (h in core or core in h or dom_label in h or h in dom_label))
        good = [it for it in items if score(it)]
        if len(good) == 1:
            accepted.append(good[0])
        else:
            ambiguous.extend(items)
    return accepted, ambiguous


def normalize_social(url: str) -> dict[str, str] | None:
    try:
        p = urllib.parse.urlsplit(url.strip())
    except ValueError:
        return None
    host = (p.hostname or "").lower()
    if host.startswith("www.") or host.startswith("m.") or host.startswith("no.") or host.startswith("nb-no."):
        host = host.split(".", 1)[1]
    plat = None
    for d, label in SOCIAL_HOSTS.items():
        if host == d or host.endswith("." + d):
            plat = label
            break
    if not plat:
        return None
    path = p.path or "/"
    if SOCIAL_BLOCK_PATHS.match(path) and not (plat == "linkedin" and path.lower().startswith(("/company/", "/school/", "/showcase/"))):
        if not (plat == "facebook" and path.startswith("/profile.php") and "id=" in (p.query or "")):
            return None
    segs = [s for s in path.split("/") if s]
    if not segs:
        return None
    handle = None
    if plat == "linkedin":
        if len(segs) >= 2 and segs[0].lower() in ("company", "school", "showcase"):
            handle = segs[1]
            canon = f"https://www.linkedin.com/{segs[0].lower()}/{handle}"
        else:
            return None
    elif plat == "youtube":
        if segs[0].startswith("@"):
            handle = segs[0]
            canon = f"https://www.youtube.com/{handle}"
        elif segs[0] in ("channel", "c", "user") and len(segs) >= 2:
            handle = segs[1]
            canon = f"https://www.youtube.com/{segs[0]}/{handle}"
        else:
            return None
    elif plat == "tiktok":
        if not segs[0].startswith("@"):
            return None
        handle = segs[0]
        canon = f"https://www.tiktok.com/{handle}"
    elif plat == "facebook" and segs[0] == "profile.php":
        q = urllib.parse.parse_qs(p.query).get("id", [""])[0]
        if not q.isdigit():
            return None
        handle = q
        canon = f"https://www.facebook.com/profile.php?id={q}"
    elif plat == "facebook" and segs[0].lower() == "pages" and len(segs) >= 2:
        handle = segs[-1] if segs[-1].isdigit() else segs[1]
        canon = "https://www.facebook.com/" + "/".join(segs)
    else:
        handle = segs[0]
        if plat in ("vimeo", "github", "pinterest", "threads", "snapchat") and len(segs) > 1 and plat != "snapchat":
            pass
        canon = f"https://{ {'facebook':'www.facebook.com','instagram':'www.instagram.com','x':'x.com','pinterest':'www.pinterest.com','vimeo':'vimeo.com','github':'github.com','threads':'www.threads.net','snapchat':'www.snapchat.com'}[plat] }/{handle}"
        if plat == "snapchat" and segs[0] == "add" and len(segs) > 1:
            handle = segs[1]
            canon = f"https://www.snapchat.com/add/{handle}"
    if not handle or handle.lower().strip("@") in GENERIC_HANDLES or len(handle) < 2:
        return None
    if re.fullmatch(r"[\w.\-@%]+", handle) is None:
        return None
    return {"platform": plat, "handle": handle, "url": canon}


def describe(res: SiteResult) -> dict[str, Any]:
    home = res.pages[0] if res.pages else None
    out: dict[str, Any] = {}
    if not home:
        return out
    org_nodes = [n for p in res.pages for n in p.jsonld if isinstance(n, dict) and str(n.get("@type", "")).lower() in
                 ("organization", "corporation", "localbusiness", "store", "restaurant", "professionalservice", "homeandconstructionbusiness",
                  "medicalbusiness", "automotivebusiness", "legalservice", "accountingservice", "realestateagent", "lodgingbusiness")]
    out["title"] = home.title
    out["description"] = home.description
    out["site_name"] = home.site_name or next((str(n.get("name")) for n in org_nodes if n.get("name")), "")
    out["lang"] = home.lang
    out["jsonld_org"] = org_nodes[:2]
    return out


def careers_pages(res: SiteResult) -> list[dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for pg in res.pages:
        for u, txt in pg.links:
            try:
                p = urllib.parse.urlsplit(u)
            except ValueError:
                continue
            same = registered_domain(p.hostname or "") == res.domain
            ats = any(x in (p.hostname or "") for x in ("teamtailor.com", "webcruiter", "jobylon", "recman", "hrmanager", "easycruit", "reachmee", "talentech", "workday", "smartrecruiters", "lever.co", "greenhouse.io", "varbi", "jobbnorge"))
            if (same or ats) and (CAREER_RE.search(p.path) or CAREER_RE.search(txt)):
                clean = urllib.parse.urlunsplit((p.scheme, p.netloc, p.path, "", ""))
                out.setdefault(clean, {"url": clean, "anchor": txt, "found_on": pg.final_url or pg.url, "page_sha256": pg.sha256 or "",
                                       "retrieved_at": pg.retrieved_at, "ats": "yes" if ats else "no"})
    return list(out.values())[:3]


DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})")


def site_jobpostings(res: SiteResult) -> list[dict[str, Any]]:
    out = []
    for pg in res.pages:
        for n in pg.jsonld:
            if isinstance(n, dict) and str(n.get("@type", "")).lower() == "jobposting" and n.get("title"):
                out.append({"title": str(n.get("title"))[:200], "date_posted": str(n.get("datePosted") or "")[:10] or None,
                            "valid_through": str(n.get("validThrough") or "")[:10] or None, "url": n.get("url") or pg.final_url,
                            "found_on": pg.final_url, "page_sha256": pg.sha256, "retrieved_at": pg.retrieved_at})
    return out[:20]


def news_candidates(res: SiteResult) -> tuple[list[str], list[dict[str, Any]]]:
    """Return (feed urls, dated article items from JSON-LD)."""
    feeds: list[str] = []
    items: list[dict[str, Any]] = []
    for pg in res.pages:
        for f in pg.feeds:
            if registered_domain(urllib.parse.urlsplit(f).hostname or "") == res.domain and f not in feeds and "comments" not in f:
                feeds.append(f)
        for n in pg.jsonld:
            if not isinstance(n, dict):
                continue
            t = str(n.get("@type", "")).lower()
            if t in ("newsarticle", "article", "blogposting", "pressrelease") and n.get("datePublished"):
                d = DATE_RE.match(str(n.get("datePublished")))
                if d and (n.get("headline") or n.get("name")):
                    items.append({"title": str(n.get("headline") or n.get("name"))[:240], "date": d.group(1),
                                  "url": n.get("url") or pg.final_url, "found_on": pg.final_url, "page_sha256": pg.sha256,
                                  "retrieved_at": pg.retrieved_at, "method": "jsonld_article"})
    return feeds[:2], items


def parse_feed(resp: Response) -> list[dict[str, Any]]:
    from email.utils import parsedate_to_datetime
    out = []
    try:
        soup = BeautifulSoup(resp.body, "xml")
    except Exception:  # noqa: BLE001
        return out
    entries = soup.find_all("item") or soup.find_all("entry")
    for it in entries[:30]:
        title = it.find("title")
        link = it.find("link")
        href = (link.get("href") if link and link.get("href") else (link.get_text(strip=True) if link else "")) or ""
        dt = it.find("pubDate") or it.find("published") or it.find("updated") or it.find("dc:date") or it.find("date")
        date = None
        if dt and dt.get_text(strip=True):
            raw = dt.get_text(strip=True)
            m = DATE_RE.match(raw)
            if m:
                date = m.group(1)
            else:
                try:
                    date = parsedate_to_datetime(raw).date().isoformat()
                except Exception:  # noqa: BLE001
                    date = None
        if title and title.get_text(strip=True) and date:
            out.append({"title": title.get_text(" ", strip=True)[:240], "date": date, "url": href.strip()})
    return out
