"""Org-number-keyed external sources: NAV job ads, Wikidata (P2333), GLEIF LEI, Brreg update feed."""
from __future__ import annotations

import re
import threading
import urllib.parse
from typing import Any

from .http import fetch, fetch_json, utc_now
from .registry import core_name, norm_name

NAV_SEARCH = "https://arbeidsplassen.nav.no/stillinger/api/search?q={q}&size=100"
NAV_TOKEN_URL = "https://pam-stilling-feed.nav.no/api/publicToken"
NAV_ENTRY = "https://pam-stilling-feed.nav.no/api/v1/feedentry/{uuid}"
NAV_AD_PAGE = "https://arbeidsplassen.nav.no/stillinger/stilling/{uuid}"
NAV_RIGHTS = "NAV arbeidsplassen open job-ad data (NLOD; feed terms https://arbeidsplassen.nav.no/vilkar-api)"

_token_lock = threading.Lock()
_token: str | None = None


def nav_token() -> str | None:
    global _token
    with _token_lock:
        if _token is None:
            r = fetch(NAV_TOKEN_URL, check_public=False, attempts=3, persist=False)
            m = re.search(r"eyJ[\w\-]+\.[\w\-]+\.[\w\-]+", r.text() if r.ok else "")
            _token = m.group(0) if m else ""
        return _token or None


def _strip_html(s: str | None) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s or "")).strip()


NAV_FEED = "https://pam-stilling-feed.nav.no"


class NavFeedIndex:
    """Active NAV job ads indexed by normalised business name, built from the official feed
    (If-Modified-Since window, immutable full pages cached on disk so refresh runs are incremental)."""

    def __init__(self) -> None:
        self.by_name: dict[str, list[dict[str, Any]]] = {}
        self.by_first: dict[str, set[str]] = {}
        self.pages = 0
        self.active = 0
        self.error: str | None = None
        self.retrieved_at = utc_now()

    def candidates(self, names: list[str]) -> list[dict[str, Any]]:
        """Exact core-name matches first, then token-prefix matches (brand vs. legal name). Every candidate is
        verified against the ad's employer org number before publication, so loose matching cannot leak."""
        out: dict[str, dict[str, Any]] = {}
        for n in names:
            for k in {core_name(n), norm_name(n)}:
                for it in self.by_name.get(k, []):
                    out[it["uuid"]] = it
        for n in names:
            c = core_name(n)
            if len(c) < 5 or not c.split():
                continue
            for k in self.by_first.get(c.split()[0], []):
                if k in (c,) or not (k.startswith(c + " ") or c.startswith(k + " ")) or len(k) < 5:
                    continue
                for it in self.by_name.get(k, []):
                    out.setdefault(it["uuid"], it)
        return list(out.values())


def build_nav_index(cache_dir: "Path | None", days: int = 150, deadline: float | None = None) -> NavFeedIndex:
    import json as _json
    import time as _time
    from datetime import datetime, timedelta, timezone
    from email.utils import format_datetime
    from pathlib import Path

    idx = NavFeedIndex()
    token = nav_token()
    if not token:
        idx.error = "no NAV public token"
        return idx
    since = datetime.now(timezone.utc) - timedelta(days=days)
    headers = {"Authorization": "Bearer " + token, "Accept": "application/json"}
    cache = Path(cache_dir) if cache_dir else None
    if cache:
        cache.mkdir(parents=True, exist_ok=True)
    latest: dict[str, dict[str, Any]] = {}
    # resume point: first page id for this window is not stable, so start from If-Modified-Since
    url = "/api/v1/feed"
    first = True
    seen_pages = set()
    while url:
        if deadline and _time.monotonic() > deadline:
            idx.error = "time budget reached while reading NAV feed"
            break
        page_id = url.rsplit("/", 1)[-1] if not first else None
        cached = None
        if cache and page_id and (cache / f"{page_id}.json").exists():
            try:
                cached = _json.loads((cache / f"{page_id}.json").read_text())
            except Exception:  # noqa: BLE001
                cached = None
        if cached is not None:
            body = cached
        else:
            h = dict(headers)
            if first:
                h["If-Modified-Since"] = format_datetime(since, usegmt=True)
            r, body = fetch_json(NAV_FEED + url, headers=h, timeout=40, persist=False)
            if not r.ok or not isinstance(body, dict):
                idx.error = f"feed page HTTP {r.status} {r.error}"
                break
            if False and cache:  # page ids are not stable across window starts; no on-disk page cache
                pid = body.get("id")
                (cache / f"{pid}.json").write_text(_json.dumps({"id": pid, "next_url": body.get("next_url"), "next_id": body.get("next_id"),
                                                                "items": body.get("items")}))
        first = False
        if body.get("id") in seen_pages:
            break
        seen_pages.add(body.get("id"))
        idx.pages += 1
        for it in body.get("items") or []:
            fe = it.get("_feed_entry") or {}
            u = fe.get("uuid")
            if u:
                latest[u] = {"uuid": u, "status": fe.get("status"), "title": fe.get("title"), "business_name": fe.get("businessName"),
                             "municipal": fe.get("municipal"), "modified": fe.get("sistEndret") or it.get("date_modified")}
        url = body.get("next_url")
        if not body.get("items"):
            break
    for it in latest.values():
        if it["status"] != "ACTIVE" or not it.get("business_name"):
            continue
        idx.active += 1
        for k in {core_name(it["business_name"]), norm_name(it["business_name"])}:
            if k:
                idx.by_name.setdefault(k, []).append(it)
                idx.by_first.setdefault(k.split()[0], set()).add(k)
    idx.retrieved_at = utc_now()
    return idx


_search_lock = threading.Lock()
_search_state = {"last": 0.0, "fails": 0, "disabled": False}


def _throttled_search(url: str) -> tuple[Any, Any]:
    import time as _time
    with _search_lock:
        if _search_state["disabled"]:
            return None, None
        wait = 2.0 - (_time.monotonic() - _search_state["last"])
        if wait > 0:
            _time.sleep(wait)
        _search_state["last"] = _time.monotonic()
    resp, body = fetch_json(url, timeout=20, attempts=1)
    with _search_lock:
        if resp.status == 429:
            _search_state["fails"] += 1
            if _search_state["fails"] >= 3:
                _search_state["disabled"] = True
        elif resp.ok:
            _search_state["fails"] = 0
    return resp, body


def _verify_entry(uuid: str, org: str, subunit_orgs: set[str], token: str | None) -> dict[str, Any] | None:
    if not token:
        return None
    eurl = NAV_ENTRY.format(uuid=uuid)
    er, ebody = fetch_json(eurl, headers={"Authorization": "Bearer " + token}, timeout=20)
    if not er.ok or not isinstance(ebody, dict):
        return None
    content = ebody.get("ad_content") or ebody.get("json") or {}
    emp_d = content.get("employer") or {}
    orgnr = re.sub(r"\D", "", str(emp_d.get("orgnr") or ""))
    locs = sorted({(x.get("city") or x.get("municipal") or "").title() for x in (content.get("workLocations") or []) if (x.get("city") or x.get("municipal"))})
    return {"orgnr": orgnr, "match": orgnr and (orgnr == org or orgnr in subunit_orgs), "entry_url": eurl, "entry_sha256": er.sha256,
            "entry_retrieved_at": er.retrieved_at, "status": ebody.get("status"), "title": content.get("title"),
            "published": (content.get("published") or "")[:10] or None, "expires": (content.get("expires") or "")[:10] or None,
            "application_due": content.get("applicationDue"), "employer_name": emp_d.get("name"), "homepage": emp_d.get("homepage"),
            "extent": content.get("extent"), "engagement_type": content.get("engagementtype"), "position_count": content.get("positioncount"),
            "sector": content.get("sector"), "locations": locs, "source_medium": content.get("source"),
            "occupation": ((content.get("occupationCategories") or [{}])[0] or {}).get("level2")}


def nav_jobs(ident: dict[str, Any], subunits: list[dict[str, Any]], name_unique: bool, feed: NavFeedIndex | None,
             use_search: bool) -> dict[str, Any]:
    """1) official NAV feed index (employer org number verified on each ad);
    2) optionally the arbeidsplassen search (covers FINN-sourced ads) for companies with registered employees."""
    org = ident["organisation_number"]
    name = ident.get("name") or ""
    core = core_name(name)
    sub_orgs = {s["organisation_number"] for s in subunits if s.get("organisation_number")}
    out: dict[str, Any] = {"jobs": [], "homepages": [], "rejected": 0, "searched": None, "error": None, "checked": []}
    if not core or len(core) < 3:
        return out
    token = None
    seen: set[str] = set()
    if feed is not None and feed.pages:
        out["checked"].append(NAV_FEED + "/api/v1/feed")
        names = [name] + [s.get("name") or "" for s in subunits[:50]]
        for cand in feed.candidates(names)[:40]:
            token = token or nav_token()
            v = _verify_entry(cand["uuid"], org, sub_orgs, token)
            if not v:
                continue
            if not v["match"] or v.get("status") not in (None, "ACTIVE"):
                out["rejected"] += 1
                continue
            seen.add(cand["uuid"])
            job = {**v, "uuid": cand["uuid"], "url": NAV_AD_PAGE.format(uuid=cand["uuid"]), "match": "nav_feed_employer_orgnr",
                   "employer_orgnr": v["orgnr"], "feed_business_name": cand["business_name"]}
            out["jobs"].append(job)
            if v.get("homepage"):
                out["homepages"].append({"url": v["homepage"], "entry_url": v["entry_url"], "sha256": v["entry_sha256"],
                                         "retrieved_at": v["entry_retrieved_at"], "span": f"employer.orgnr={v['orgnr']}; employer.homepage={v['homepage']}"})
    employs = bool(ident.get("employees")) or any(s.get("registered_employees") for s in subunits)
    if not (use_search and employs):
        return out
    url = NAV_SEARCH.format(q=urllib.parse.quote(core))
    resp, body = _throttled_search(url)
    if resp is None:
        out["error_search"] = "search disabled after rate limiting"
        return out
    out["searched"] = url
    out["checked"].append(url)
    if not resp.ok or not isinstance(body, dict):
        out["error_search"] = f"HTTP {resp.status} {resp.error}"
        return out
    want = norm_name(name)
    for h in ((body.get("hits") or {}).get("hits")) or []:
        s = h.get("_source") or {}
        uuid = s.get("uuid")
        if not uuid or uuid in seen or s.get("status") not in (None, "ACTIVE"):
            continue
        emp = (s.get("employer") or {}).get("name") or ""
        bn = s.get("businessName") or ""
        if norm_name(emp) != want and core_name(emp) != core and core_name(bn) != core:
            continue
        base = {"uuid": uuid, "title": s.get("title"), "published": (s.get("published") or "")[:10] or None,
                "expires": (s.get("expires") or "")[:10] or None, "employer_name": emp, "source_medium": s.get("medium") or s.get("source"),
                "url": NAV_AD_PAGE.format(uuid=uuid),
                "locations": sorted({(x.get("city") or x.get("municipal") or "").title() for x in (s.get("locationList") or []) if (x.get("city") or x.get("municipal"))}),
                "application_due": (s.get("properties") or {}).get("applicationdue"),
                "occupation": ((s.get("occupationList") or [{}])[0] or {}).get("level2"),
                "search_url": url, "search_sha256": resp.sha256, "search_retrieved_at": resp.retrieved_at}
        if str(base["source_medium"] or "").upper() != "FINN":
            token = token or nav_token()
            v = _verify_entry(uuid, org, sub_orgs, token)
            if v and v["match"]:
                out["jobs"].append({**base, **{k: v[k] for k in ("entry_url", "entry_sha256", "entry_retrieved_at", "extent", "engagement_type",
                                                                  "position_count", "sector") if v.get(k)}, "employer_orgnr": v["orgnr"],
                                    "match": "nav_feed_employer_orgnr"})
                seen.add(uuid)
                continue
            if v and v["orgnr"]:
                out["rejected"] += 1
                continue
        if name_unique and norm_name(emp) == want and len(want.split()) >= 2:
            base["match"] = "exact_unique_legal_name"
            out["jobs"].append(base)
            seen.add(uuid)
        else:
            out["rejected"] += 1
    return out


# ---------------------------------------------------------------- Wikidata
WD_SPARQL = "https://query.wikidata.org/sparql"
WD_PROPS = {"P856": "website", "P2013": "facebook", "P2003": "instagram", "P4264": "linkedin", "P2002": "x",
            "P2397": "youtube", "P7085": "tiktok", "P571": "inception", "P946": "isin", "P414": "exchange", "P1128": "employees"}


def wikidata_batch(orgs: list[str]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for i in range(0, len(orgs), 150):
        chunk = orgs[i:i + 150]
        vals = " ".join(f'"{o}"' for o in chunk) + " " + " ".join(f'"{o[:3]} {o[3:6]} {o[6:]}"' for o in chunk)
        q = ("SELECT ?item ?org ?itemLabel ?site ?fb ?ig ?li ?x ?yt ?tt ?inception ?isin WHERE { VALUES ?org { %s } "
             "?item p:P2333 ?st . ?st ps:P2333 ?org . FILTER NOT EXISTS { ?st wikibase:rank wikibase:DeprecatedRank } "
             "OPTIONAL{?item p:P856 ?sw . ?sw ps:P856 ?site . ?sw wikibase:rank ?wr FILTER(?wr != wikibase:DeprecatedRank)} "
             "OPTIONAL{?item wdt:P2013 ?fb} OPTIONAL{?item wdt:P2003 ?ig} OPTIONAL{?item wdt:P4264 ?li} "
             "OPTIONAL{?item wdt:P2002 ?x} OPTIONAL{?item wdt:P2397 ?yt} OPTIONAL{?item wdt:P7085 ?tt} "
             "OPTIONAL{?item wdt:P571 ?inception} OPTIONAL{?item wdt:P946 ?isin} "
             "SERVICE wikibase:label { bd:serviceParam wikibase:language \"nb,nn,en\". } }") % vals
        url = WD_SPARQL + "?" + urllib.parse.urlencode({"query": q, "format": "json"})
        resp, body = fetch_json(url, headers={"Accept": "application/sparql-results+json"}, timeout=40)
        if not resp.ok or not isinstance(body, dict):
            for o in chunk:
                out[o] = {"error": f"HTTP {resp.status} {resp.error}"}
            continue
        items_per_org: dict[str, set[str]] = {}
        for b in body.get("results", {}).get("bindings", []):
            o = re.sub(r"\D", "", b["org"]["value"])
            items_per_org.setdefault(o, set()).add(b["item"]["value"])
            rec = out.setdefault(o, {"item": b["item"]["value"], "label": (b.get("itemLabel") or {}).get("value"),
                                     "values": {}, "query_url": url, "sha256": resp.sha256, "retrieved_at": resp.retrieved_at})
            for k in ("site", "fb", "ig", "li", "x", "yt", "tt", "inception", "isin"):
                if k in b:
                    rec["values"].setdefault(k, set()).add(b[k]["value"])
        for o, items in items_per_org.items():
            if len(items) > 1:
                out[o] = {"ambiguous": sorted(items), "query_url": url, "sha256": resp.sha256, "retrieved_at": resp.retrieved_at}
        for o in chunk:
            out.setdefault(o, {"none": True, "query_url": url, "sha256": resp.sha256, "retrieved_at": resp.retrieved_at})
    for rec in out.values():
        if "values" in rec:
            rec["values"] = {k: sorted(v) for k, v in rec["values"].items()}
    return out


# ---------------------------------------------------------------- GLEIF
GLEIF = "https://api.gleif.org/api/v1/lei-records?filter%5Bentity.registeredAs%5D={ids}&page%5Bsize%5D=200"


def gleif_batch(orgs: list[str]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for i in range(0, len(orgs), 40):
        chunk = orgs[i:i + 40]
        ids = ",".join(chunk + [f"{o[:3]} {o[3:6]} {o[6:]}" for o in chunk])
        url = GLEIF.format(ids=urllib.parse.quote(ids, safe=","))
        resp, body = fetch_json(url, headers={"Accept": "application/vnd.api+json"}, timeout=30)
        if not resp.ok or not isinstance(body, dict):
            continue
        for rec in body.get("data") or []:
            a = rec.get("attributes") or {}
            ent = a.get("entity") or {}
            o = re.sub(r"\D", "", str(ent.get("registeredAs") or ""))
            if o not in chunk:
                continue
            if (ent.get("jurisdiction") or "NO").upper() not in ("NO",) and not str(ent.get("jurisdiction") or "").upper().startswith("NO"):
                continue
            out.setdefault(o, {"records": [], "url": url, "sha256": resp.sha256, "retrieved_at": resp.retrieved_at})
            out[o]["records"].append({"lei": a.get("lei") or rec.get("id"), "legal_name": (ent.get("legalName") or {}).get("name"),
                                      "status": (a.get("registration") or {}).get("status"),
                                      "entity_status": ent.get("status"),
                                      "next_renewal": (a.get("registration") or {}).get("nextRenewalDate", "")[:10],
                                      "registered_as": ent.get("registeredAs")})
        for o in chunk:
            out.setdefault(o, {"records": [], "url": url, "sha256": resp.sha256, "retrieved_at": resp.retrieved_at})
    return out


# ---------------------------------------------------------------- Brreg update feed (dated registry activity)
UPDATES = "https://data.brreg.no/enhetsregisteret/api/oppdateringer/enheter?organisasjonsnummer={orgs}&includeChanges=true&size=10000&dato={since}"
ROLE_UPDATES = "https://data.brreg.no/enhetsregisteret/api/oppdateringer/roller?organisasjonsnummer={orgs}&size=10000&afterTime={since}"

FIELD_LABELS = {
    "/navn": "legal name", "/antallAnsatte": "registered employee count", "/forretningsadresse": "business address",
    "/postadresse": "postal address", "/hjemmeside": "website", "/epostadresse": "e-mail", "/telefon": "phone",
    "/mobil": "mobile phone", "/naeringskode1": "primary industry code", "/naeringskode2": "secondary industry code",
    "/naeringskode3": "tertiary industry code", "/sisteInnsendteAarsregnskap": "latest filed annual accounts",
    "/kapital": "share capital", "/vedtektsdato": "articles of association", "/vedtektsfestetFormaal": "statutory purpose",
    "/aktivitet": "registered activity", "/konkurs": "bankruptcy status", "/underAvvikling": "liquidation status",
    "/registrertIMvaregisteret": "VAT registration", "/organisasjonsform": "legal form", "/erIKonsern": "group membership",
    "/hjelpeenhetskode": "auxiliary unit code", "/institusjonellSektorkode": "institutional sector",
}


def _label(path: str) -> str | None:
    for k, v in FIELD_LABELS.items():
        if path == k or path.startswith(k + "/"):
            return v
    return None


def registry_updates_batch(orgs: list[str], since: str) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {o: {"events": []} for o in orgs}
    for i in range(0, len(orgs), 50):
        chunk = orgs[i:i + 50]
        url = UPDATES.format(orgs=",".join(chunk), since=since)
        resp, body = fetch_json(url, timeout=40)
        for o in chunk:
            out[o].update({"url": url, "sha256": resp.sha256, "retrieved_at": resp.retrieved_at, "ok": resp.ok})
        if not resp.ok or not isinstance(body, dict):
            continue
        for u in (body.get("_embedded") or {}).get("oppdaterteEnheter") or []:
            o = u.get("organisasjonsnummer")
            if o not in out:
                continue
            labels: list[str] = []
            detail: list[dict[str, Any]] = []
            for ch in u.get("endringer") or []:
                lab = _label(ch.get("path") or "")
                if lab and lab not in labels:
                    labels.append(lab)
                if lab and len(detail) < 6:
                    v = ch.get("value")
                    if isinstance(v, (dict, list)):
                        v = None
                    detail.append({"field": lab, "op": ch.get("op"), "path": ch.get("path"), **({"value": v} if v is not None else {})})
            out[o]["events"].append({"date": (u.get("dato") or "")[:10], "timestamp": u.get("dato"), "type": u.get("endringstype"),
                                     "update_id": u.get("oppdateringsid"), "fields": labels, "changes": detail})
    return out
