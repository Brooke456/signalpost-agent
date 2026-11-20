"""Per-company research pipeline: registry → accounts → roles/workplaces → web identity → jobs → activity."""
from __future__ import annotations

import time
import urllib.parse
from datetime import date, timedelta
from typing import Any

from . import web
from .claims import Collector
from .external import NAV_RIGHTS, nav_jobs
from .http import fetch, utc_now
from .registry import (Bulk, collect_accounts, collect_entity, collect_group, collect_roles, collect_subunits, norm_name, _domain_of)

GENERIC_MAILBOXES = {"post", "postmottak", "info", "kontakt", "contact", "firmapost", "mail", "epost", "office", "kontor", "salg", "sales",
                     "service", "kundeservice", "support", "hei", "hello", "hallo", "booking", "bestilling", "ordre", "order", "resepsjon",
                     "admin", "administrasjon", "regnskap", "faktura", "invoice", "marked", "marketing", "jobb", "karriere", "hr", "presse", "press"}
WEB_RIGHTS = "Company-owned public website; fetched once per run, robots.txt respected"
WD_RIGHTS = "Wikidata (CC0)"
GLEIF_RIGHTS = "GLEIF LEI data (CC0)"


def research_company(org: str, *, bulk: Bulk, prefetched: dict[str, Any], deadline: float, options: dict[str, Any]) -> dict[str, Any]:
    t0 = time.monotonic()
    col = Collector(org)
    bulk_row = bulk.rows.get(org)
    bulk_meta = {"sha256": bulk.sha256, "retrieved_at": prefetched.get("bulk_loaded_at")}
    ident = collect_entity(col, org, bulk_row, bulk_meta)
    modules: dict[str, str] = {}

    if ident.get("name") is None:
        for s in ("financials", "leadership", "workplaces", "web_presence", "hiring", "activity"):
            col.section(s, "failed", reason="identity could not be anchored; nothing published")
        return _result(col, ident, {}, t0)

    periods = collect_accounts(col, org)
    roles = collect_roles(col, org)
    subunits = collect_subunits(col, org)
    group = collect_group(col, org, ident)
    _identifiers(col, org, prefetched)

    # ------------------------------------------------------------ website
    unique = bulk.name_counts.get(norm_name(ident.get("name") or ""), 0) == 1 if bulk.name_counts else False
    site_info: dict[str, Any] = {}
    wd = prefetched.get("wikidata", {}).get(org) or {}
    if time.monotonic() > deadline:
        col.section("web_presence", "blocked", reason="run time budget exhausted before website step")
    else:
        site_info = _website(col, ident, bulk, {"homepages": []}, wd, deadline, {**options, "subunits": subunits})

    # ------------------------------------------------------------ hiring (NAV feed + search)
    needs_jobs = False
    if options.get("jobs", True) and time.monotonic() < deadline:
        fut = prefetched.get("nav_feed_future")
        if fut is not None and not fut.done():
            needs_jobs = True  # finished in phase 2 once the feed index is ready (see run.py)
        else:
            run_jobs(col, ident, bulk, subunits, unique, fut.result() if fut is not None else None, site_info, deadline, options)

    # ------------------------------------------------------------ activity
    _activity(col, org, prefetched.get("updates", {}).get(org), periods)
    if "activity" not in col.section_state:
        col.section("activity", "not_available", reason="no dated public activity in checked sources")
    out = _result(col, ident, {"periods": periods, "roles": roles, "subunits": subunits, "group": group, "site": site_info,
                               "unique": unique}, t0)
    out["needs_jobs"] = needs_jobs
    if not needs_jobs:
        finish_hiring(col)
    return out


def finish_hiring(col: Collector) -> None:
    if "hiring" not in col.section_state:
        col.section("hiring", "not_available", reason="no verified job postings or careers page found")


def run_jobs(col: Collector, ident: dict[str, Any], bulk: Bulk, subunits: list[dict[str, Any]], unique: bool, feed: Any,
             site_info: dict[str, Any], deadline: float, options: dict[str, Any]) -> None:
    nav = nav_jobs(ident, subunits, unique, feed, options.get("nav_search", True))
    _jobs_claims(col, nav)
    if not site_info.get("official_website") and nav.get("homepages") and time.monotonic() < deadline:
        # the employer record on a verified NAV ad names a homepage: verify it like a registry-declared site
        if col.section_state.get("web_presence", {}).get("availability") != "available":
            col.section_state.pop("web_presence", None)
        _website(col, ident, bulk, nav, {}, deadline, {**options, "guess_domains": False, "skip_registry": True})


def _result(col: Collector, ident: dict[str, Any], extra: dict[str, Any], t0: float) -> dict[str, Any]:
    return {"collector": col, "ident": ident, "extra": extra, "runtime_ms": int((time.monotonic() - t0) * 1000)}


# ---------------------------------------------------------------- identifiers (LEI)
def _identifiers(col: Collector, org: str, pre: dict[str, Any]) -> None:
    g = (pre.get("gleif") or {}).get(org)
    if g and g.get("records"):
        for r in g["records"][:2]:
            e = col.ev(url=g["url"], source_class="gleif_lei_api", retrieved_at=g["retrieved_at"], sha256=g["sha256"],
                       span=f"lei {r['lei']}; entity.registeredAs={r['registered_as']}; legalName={r['legal_name']}; registration.status={r['status']}",
                       method="gleif_lei_records_filter_registeredAs", rights=GLEIF_RIGHTS)
            col.claim("identity", "lei", {"lei": r["lei"], "registration_status": r["status"], "next_renewal": r.get("next_renewal")}, [e], key=r["lei"],
                      confidence=0.99, entity_match="exact_org_number_in_lei_record")


# ---------------------------------------------------------------- jobs
def _jobs_claims(col: Collector, nav: dict[str, Any]) -> None:
    jobs = nav.get("jobs") or []
    for j in jobs:
        evs = []
        if j.get("entry_url"):
            evs.append(col.ev(url=j["entry_url"], source_class="nav_job_feed_api", retrieved_at=j["entry_retrieved_at"], sha256=j["entry_sha256"],
                              span=f"ad {j['uuid']}: title={j.get('title')!r}; employer.orgnr={j['employer_orgnr']}; published={j.get('published')}",
                              method="nav_feed_entry_employer_orgnr", rights=NAV_RIGHTS))
        if j.get("search_url"):
            evs.append(col.ev(url=j["search_url"], source_class="nav_arbeidsplassen_search", retrieved_at=j["search_retrieved_at"],
                              sha256=j["search_sha256"], span=f"uuid {j['uuid']}: \"{j.get('title')}\" employer.name={j.get('employer_name')} published {j.get('published')}",
                              method="nav_search_hit", rights=NAV_RIGHTS))
        value = {k: j.get(k) for k in ("title", "published", "expires", "application_due", "locations", "occupation", "url",
                                        "source_medium", "employer_name", "extent", "engagement_type", "position_count") if j.get(k)}
        col.claim("hiring", "job_posting", value, evs, key=j["uuid"], effective_date=j.get("published"),
                  confidence=0.99 if j["match"] == "nav_feed_employer_orgnr" else 0.93, entity_match=j["match"])
    srcs = nav.get("checked") or []
    if jobs:
        col.section("hiring", "available", sources=srcs)
        e = col.ev(url=srcs[0] if srcs else "https://arbeidsplassen.nav.no/", source_class="nav_job_feed_api", retrieved_at=utc_now(), sha256=None,
                   span=f"{len(jobs)} active ads verified to this organisation", method="count_of_verified_ads", rights=NAV_RIGHTS)
        col.claim("hiring", "active_job_postings_count", len(jobs), [e], key="count", effective_date=utc_now()[:10])
    elif srcs:
        col.section("hiring", "not_available", reason="no active NAV job ads verified to this organisation number", sources=srcs)


# ---------------------------------------------------------------- website
def _website(col: Collector, ident: dict[str, Any], bulk: Bulk, nav: dict[str, Any], wd: dict[str, Any], deadline: float,
             options: dict[str, Any]) -> dict[str, Any]:
    org = ident["organisation_number"]
    cands: list[tuple[str, str, dict[str, Any]]] = []
    seen_domains: set[str] = set()

    def add(url: str | None, origin: str, meta: dict[str, Any]) -> None:
        n = web.normalize_url(url)
        if not n:
            return
        d = web.registered_domain(urllib.parse.urlsplit(n).hostname or "")
        if not d or d in seen_domains:
            return
        if d in web.FREE_MAIL or any(d.endswith(x) for x in ("facebook.com", "instagram.com", "linkedin.com", "proff.no", "gulesider.no", "1881.no",
                                                              "finn.no", "google.com", "wix.com", "wixsite.com", "blogspot.com", "mittanbud.no")):
            return
        seen_domains.add(d)
        cands.append((n, origin, meta))

    if not options.get("skip_registry"):
        add(ident.get("website"), "registry_declared", {"span": f"hjemmeside: {ident.get('website')}"})
    for hp in nav.get("homepages") or []:
        add(hp["url"], "nav_employer_homepage", hp)
    if wd.get("values", {}).get("site") and not wd.get("ambiguous"):
        sites = wd["values"]["site"]
        if len(sites) == 1:
            add(sites[0], "wikidata_P856", wd)
    email = (ident.get("email") or "").strip().lower()
    if "@" in email and not options.get("skip_registry"):
        dom = email.rsplit("@", 1)[1]
        shared = bulk.email_domain_counts.get(dom, 0) if bulk.email_domain_counts else 0
        if dom not in web.FREE_MAIL and shared <= 25:
            add("https://" + dom + "/", "registry_email_domain", {"span": f"epostadresse: {email}", "shared": shared})
    subunits = options.get("subunits") or []
    if not options.get("skip_registry"):
        # websites / e-mail domains the company declared for its own registered sub-units (same legal entity)
        for su in subunits[:30]:
            dec = su.get("_declared") or {}
            if dec.get("website"):
                add(dec["website"], "registry_subunit_declared", {**dec, "subunit": su.get("organisation_number"),
                                                                  "span": f"underenhet {su.get('organisation_number')} hjemmeside: {dec['website']}"})
            sem = (dec.get("email") or "").strip().lower()
            if "@" in sem:
                sd = sem.rsplit("@", 1)[1]
                shared = bulk.email_domain_counts.get(sd, 0) if bulk.email_domain_counts else 0
                if sd not in web.FREE_MAIL and shared <= 25:
                    add("https://" + sd + "/", "registry_email_domain", {**dec, "subunit": su.get("organisation_number"), "shared": shared,
                                                                         "span": f"underenhet {su.get('organisation_number')} epostadresse: {sem}"})
    if options.get("guess_domains", True):
        core = web.norm_name(web.core_name(ident.get("name") or ""))
        extra: list[str] = []
        for su in subunits[:30]:
            n = su.get("name") or ""
            c = web.core_name(n)
            if c and not c.startswith(core) and n not in extra:
                extra.append(n)  # a trading name registered for one of the company's own workplaces
        for h in (ident.get("historical_names") or [])[-2:]:
            n = h.get("navn") if isinstance(h, dict) else str(h)
            if n and n not in extra:
                extra.append(n)
        for g in web.guess_domains(ident, limit=4, extra_names=extra[:4]):
            add(g, "name_guess", {})

    results: list[web.SiteResult] = []
    chosen: web.SiteResult | None = None
    checked = 0
    for url, origin, meta in cands:
        if time.monotonic() > deadline:
            col.error("website", "time budget exhausted during website verification")
            break
        if checked >= 7:
            break
        checked += 1
        d0 = web.registered_domain(urllib.parse.urlsplit(url).hostname or "")
        shared = max(bulk.web_domain_counts.get(d0, 0), bulk.email_domain_counts.get(d0, 0)) if bulk.rows_scanned else 0
        res = web.check_site(url, origin, ident, max_pages=6 if origin != "name_guess" else 5, shared=shared)
        res.meta = meta  # type: ignore[attr-defined]
        results.append(res)
        if res.verdict == "verified":
            chosen = res
            break
        if res.verdict == "accepted_registry" and chosen is None:
            chosen = res
            if origin in ("registry_declared", "registry_subunit_declared", "nav_employer_homepage") and res.confidence >= 0.96:
                break
    sources = [r.candidate for r in results]
    info: dict[str, Any] = {"candidates": [{"url": r.candidate, "origin": r.origin, "verdict": r.verdict, "reason": r.reason,
                                             "final_url": r.final_url, "error": r.error} for r in results]}
    if chosen is None:
        if any(r.verdict == "ambiguous" for r in results):
            amb = [r for r in results if r.verdict == "ambiguous"]
            col.section("web_presence", "ambiguous", reason="; ".join(f"{r.domain or r.candidate}: {r.reason}" for r in amb)[:400], sources=sources)
        elif any(r.verdict == "blocked" and r.origin != "name_guess" for r in results):
            col.section("web_presence", "blocked", reason="website disallows crawling (robots.txt)", sources=sources)
        elif results and results[0].origin == "registry_declared" and results[0].verdict == "unreachable" and not any(r.verdict == "rejected" for r in results[1:]):
            col.section("web_presence", "failed", reason="registry-declared website unreachable: " + (results[0].error or ""), sources=sources)
        else:
            col.section("web_presence", "not_available", reason="no candidate website resolved to this organisation number" if results else
                        "no website declared in the registry and no verifiable candidate", sources=sources)
        _wikidata_profiles(col, wd, None)
        return info

    home = chosen.pages[0]
    final = chosen.final_url or chosen.candidate
    chain = bool(getattr(chosen, "chain_page", False))
    if chain and chosen.org_evidence:
        final = chosen.org_evidence[0]
        home = next((p for p in chosen.pages if (p.final_url or p.url) == final), home)
    evs = []
    if chosen.org_found and chosen.org_evidence:
        pu, span = chosen.org_evidence
        pg = next((p for p in chosen.pages if (p.final_url or p.url) == pu), home)
        evs.append(col.ev(url=pu, source_class="company_owned_website", retrieved_at=pg.retrieved_at, sha256=pg.sha256,
                          span=span, method="org_number_on_page", rights=WEB_RIGHTS))
    meta = getattr(chosen, "meta", {}) or {}
    if meta.get("subunit"):
        evs.append(col.ev(url=meta["span_url"], source_class="official_subunits_api", retrieved_at=meta["retrieved_at"], sha256=meta["sha256"],
                          span=meta["span"], method="brreg_subunit_declared_contact", rights="Enhetsregisteret (NLOD 2.0)"))
    elif chosen.origin in ("registry_declared", "registry_email_domain"):
        fld = "registry_declared_website" if chosen.origin == "registry_declared" else "registry_email"
        for c in col.claims:
            if c["field"] == fld:
                evs.extend(c["evidence_ids"])
    elif chosen.origin == "nav_employer_homepage":
        evs.append(col.ev(url=meta["entry_url"], source_class="nav_job_feed_api", retrieved_at=meta["retrieved_at"], sha256=meta["sha256"],
                          span=meta["span"], method="nav_feed_employer_homepage", rights=NAV_RIGHTS))
    elif chosen.origin == "wikidata_P856":
        evs.append(col.ev(url=meta["query_url"], source_class="wikidata_sparql", retrieved_at=meta["retrieved_at"], sha256=meta["sha256"],
                          span=f"{meta.get('item')} P2333={org}; P856={meta.get('values', {}).get('site')}", method="wikidata_p2333_p856", rights=WD_RIGHTS))
    evs.append(col.ev(url=final, source_class="company_owned_website", retrieved_at=home.retrieved_at, sha256=home.sha256,
                      span=f"<title>{home.title}</title>", method="homepage_fetch", rights=WEB_RIGHTS,
                      final_url=final))
    match = {"verified": "org_number_on_site" if chosen.org_found else ("name_and_registry_phone_on_site" if chosen.phone_match else
                                                                       "name_and_registry_address_on_site" if chosen.address_match else "name_and_registry_email_on_site"),
             "accepted_registry": "registry_declared_domain"}[chosen.verdict]
    if chosen.origin == "registry_email_domain" and not chosen.org_found:
        match = "registry_email_domain_corroborated"
    if chosen.origin == "nav_employer_homepage" and not chosen.org_found:
        match = "nav_employer_orgnr_homepage"
    col.claim("web_presence", "official_website", final, evs, key="site", confidence=chosen.confidence, entity_match=match,
              note=chosen.reason)
    if chain:
        # company page on a shared chain / franchise domain: the domain's profiles, news and careers belong to the chain
        col.section("web_presence", "available", sources=sources)
        info.update({"official_website": final, "verdict": chosen.verdict, "origin": chosen.origin, "domain": chosen.domain, "chain_page": True})
        return info
    col.claim("web_presence", "website_domain", chosen.domain, evs[:1], key="domain", confidence=chosen.confidence, entity_match=match)
    d = web.describe(chosen)
    hev = col.ev(url=final, source_class="company_owned_website", retrieved_at=home.retrieved_at, sha256=home.sha256,
                 span=f"title={d.get('title')!r}; meta description={d.get('description')!r}"[:600], method="html_meta", rights=WEB_RIGHTS)
    if d.get("site_name"):
        col.claim("identity", "public_brand_name", d["site_name"], [hev], key="brand", confidence=min(chosen.confidence, 0.95), entity_match=match)
    if d.get("title"):
        col.claim("web_presence", "website_title", d["title"], [hev], key="title", confidence=chosen.confidence, entity_match=match)
    if d.get("description"):
        col.claim("web_presence", "website_description", d["description"], [hev], key="desc", confidence=chosen.confidence, entity_match=match,
                  note="Company-authored self-description (promotional copy, not an independent assessment).")
    if d.get("lang"):
        col.claim("web_presence", "website_language", d["lang"], [hev], key="lang", confidence=chosen.confidence, entity_match=match)
    # contact facts from the site that match the domain
    for pg in chosen.pages:
        for m in pg.mail:
            local = m.split("@", 1)[0]
            if m.endswith("@" + chosen.domain) and local in GENERIC_MAILBOXES:
                e = col.ev(url=pg.final_url, source_class="company_owned_website", retrieved_at=pg.retrieved_at, sha256=pg.sha256,
                           span=f"mailto:{m}", method="mailto_link", rights=WEB_RIGHTS)
                col.claim("web_presence", "website_contact_email", m, [e], confidence=chosen.confidence, entity_match=match)
                break
    accepted, ambiguous = web.social_profiles(chosen, ident)
    shared_section = web.is_section_of_shared_site(final, chosen.domain, ident)
    if shared_section:
        # the company's page sits inside a larger organisation's site (e.g. one kindergarten on a chain domain):
        # profiles, careers pages and news linked there belong to the chain, so they are not attributed to this company
        accepted, ambiguous = [], []
    for s in accepted:
        e = col.ev(url=s["found_on"], source_class="company_owned_website", retrieved_at=s["retrieved_at"], sha256=s["page_sha256"],
                   span=f"link to {s['url']} (anchor: {s['anchor']!r})", method="outbound_social_link", rights=WEB_RIGHTS)
        col.claim("web_presence", "social_profile", {"platform": s["platform"], "url": s["url"], "handle": s["handle"]}, [e],
                  key=(s["platform"], s["handle"].lower()), confidence=min(chosen.confidence, 0.95), entity_match="linked_from_verified_site",
                  label=s["platform"])
    if ambiguous:
        col.claim("web_presence", "social_profile_candidates", [{"platform": s["platform"], "url": s["url"]} for s in ambiguous][:8],
                  [col.ev(url=ambiguous[0]["found_on"], source_class="company_owned_website", retrieved_at=ambiguous[0]["retrieved_at"],
                          sha256=ambiguous[0]["page_sha256"], span="several profiles per platform linked; not attributed", method="outbound_social_link")],
                  key="amb", availability="ambiguous", confidence=0.5, entity_match="unresolved")
    _wikidata_profiles(col, wd, chosen)
    for c in ([] if shared_section else web.careers_pages(chosen)):
        e = col.ev(url=c["found_on"], source_class="company_owned_website", retrieved_at=c["retrieved_at"], sha256=c["page_sha256"],
                   span=f"link {c['url']} ({c['anchor']!r})", method="careers_link", rights=WEB_RIGHTS)
        col.claim("hiring", "careers_page", c["url"], [e], confidence=min(chosen.confidence, 0.95), entity_match="linked_from_verified_site")
        col.section("hiring", "available", sources=[c["found_on"]])
    for j in ([] if shared_section else web.site_jobpostings(chosen)):
        e = col.ev(url=j["found_on"], source_class="company_owned_website", retrieved_at=j["retrieved_at"], sha256=j["page_sha256"],
                   span=f"JSON-LD JobPosting title={j['title']!r} datePosted={j['date_posted']}", method="jsonld_jobposting", rights=WEB_RIGHTS)
        col.claim("hiring", "job_posting", {"title": j["title"], "published": j["date_posted"], "expires": j["valid_through"], "url": j["url"],
                                            "source_medium": "company website"}, [e], key=("site", j["title"]), effective_date=j["date_posted"],
                  confidence=min(chosen.confidence, 0.95), entity_match="published_on_verified_site")
        col.section("hiring", "available", sources=[j["found_on"]])
    if not shared_section:
        _site_news(col, chosen, deadline)
    col.section("web_presence", "available", sources=sources)
    info.update({"official_website": final, "verdict": chosen.verdict, "origin": chosen.origin, "domain": chosen.domain,
                 "description": d.get("description"), "title": d.get("title")})
    return info


def _wikidata_profiles(col: Collector, wd: dict[str, Any], chosen: Any) -> None:
    if not wd or wd.get("ambiguous") or not wd.get("values"):
        return
    vals = wd["values"]
    tmpl = {"fb": ("facebook", "https://www.facebook.com/{}"), "ig": ("instagram", "https://www.instagram.com/{}"),
            "li": ("linkedin", "https://www.linkedin.com/company/{}"), "x": ("x", "https://x.com/{}"),
            "yt": ("youtube", "https://www.youtube.com/channel/{}"), "tt": ("tiktok", "https://www.tiktok.com/@{}")}
    e = col.ev(url=wd["query_url"], source_class="wikidata_sparql", retrieved_at=wd["retrieved_at"], sha256=wd["sha256"],
               span=f"{wd.get('item')} ({wd.get('label')}) P2333={col.org}", method="wikidata_p2333", rights=WD_RIGHTS)
    col.claim("identity", "wikidata_item", wd.get("item"), [e], key="wd", confidence=0.97, entity_match="exact_org_number_in_wikidata")
    for k, (plat, t) in tmpl.items():
        ids = vals.get(k) or []
        if len(ids) != 1:
            continue
        h = ids[0]
        col.claim("web_presence", "social_profile", {"platform": plat, "url": t.format(h), "handle": h}, [e], key=(plat, h.lower()),
                  confidence=0.9, entity_match="wikidata_item_with_org_number", label=plat)
        col.section("web_presence", "available", sources=[wd["query_url"]])
    if vals.get("isin"):
        col.claim("identity", "isin", vals["isin"][:3], [e], key="isin", confidence=0.95, entity_match="exact_org_number_in_wikidata")


def _site_news(col: Collector, chosen: Any, deadline: float) -> None:
    feeds, items = web.news_candidates(chosen)
    for f in feeds[:1]:
        if time.monotonic() > deadline or not web.robots_allowed(f):
            break
        r = fetch(f, timeout=10, attempts=1, max_bytes=1_500_000)
        if r.ok:
            for it in web.parse_feed(r)[:15]:
                items.append({**it, "found_on": f, "page_sha256": r.sha256, "retrieved_at": r.retrieved_at, "method": "rss_atom_feed"})
    cutoff = (date.today() - timedelta(days=730)).isoformat()
    today = date.today().isoformat()
    seen = set()
    items = [i for i in items if i.get("date") and cutoff <= i["date"] <= today]
    items.sort(key=lambda i: i["date"], reverse=True)
    n = 0
    for it in items:
        k = (it["title"].lower(), it["date"])
        if k in seen:
            continue
        seen.add(k)
        url = it.get("url") or it["found_on"]
        if url and web.registered_domain(urllib.parse.urlsplit(url).hostname or "") not in ("", chosen.domain):
            continue
        e = col.ev(url=it["found_on"], source_class="company_owned_website", retrieved_at=it["retrieved_at"], sha256=it["page_sha256"],
                   span=f"{it['date']} — {it['title']}", method=it["method"], rights=WEB_RIGHTS)
        col.claim("activity", "company_news", {"date": it["date"], "title": it["title"], "url": url}, [e], key=(it["title"], it["date"]),
                  effective_date=it["date"], confidence=min(chosen.confidence, 0.95), entity_match="published_on_verified_site")
        col.section("activity", "available", sources=[it["found_on"]])
        n += 1
        if n >= 10:
            break


# ---------------------------------------------------------------- dated registry activity
def _activity(col: Collector, org: str, upd: dict[str, Any] | None, periods: list[dict[str, Any]]) -> None:
    if upd and upd.get("ok"):
        evs = sorted([e for e in upd.get("events", []) if e.get("fields")], key=lambda e: e["date"], reverse=True)
        for ev in evs[:15]:
            e = col.ev(url=upd["url"], source_class="official_registry_update_feed", retrieved_at=upd["retrieved_at"], sha256=upd["sha256"],
                       span=f"oppdateringsid {ev['update_id']} {ev['timestamp']} {ev['type']}: " + ", ".join(c.get("path", "") for c in ev["changes"]),
                       method="brreg_oppdateringer_includeChanges", rights="NLOD 2.0 (Brønnøysundregistrene open data)")
            col.claim("activity", "registry_change", {"date": ev["date"], "type": ev["type"], "fields": ev["fields"],
                                                      "changes": ev["changes"]}, [e], key=ev["update_id"], effective_date=ev["date"])
        if evs:
            col.section("activity", "available", sources=[upd["url"]])
        else:
            col.section("activity", "not_available", reason="no registry changes since 2025-09 in the update feed", sources=[upd["url"]])
    elif upd:
        col.section("activity", "failed", reason="registry update feed unavailable", sources=[upd.get("url")])
