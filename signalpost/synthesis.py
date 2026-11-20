"""Decision-useful, evidence-bounded synthesis.

Deterministic by default: every sentence is generated from published claims and cites their claim ids.
Optional LLM polish (SIGNALPOST_LLM_API_KEY): the model only rephrases the deterministic brief; any output
containing a number or date not present in the cited claims is rejected and the deterministic text is kept.
"""
from __future__ import annotations

import json
import os
import re
import urllib.request
from typing import Any


def _nok(v: float | int | None, cur: str = "NOK") -> str:
    if v is None:
        return "n/a"
    a = abs(float(v))
    sign = "-" if float(v) < 0 else ""
    if a >= 1e9:
        s = f"{a / 1e9:.2f} bn"
    elif a >= 1e6:
        s = f"{a / 1e6:.1f} m"
    elif a >= 1e3:
        s = f"{a / 1e3:.0f} k"
    else:
        s = f"{a:.0f}"
    return f"{sign}{cur} {s}"


def _pct(a: float, b: float) -> str | None:
    if b in (0, None) or a is None:
        return None
    ch = (a - b) / abs(b) * 100
    return f"{ch:+.0f}%"


def build(claims: list[dict[str, Any]], sections: dict[str, Any], changes: list[dict[str, Any]], ident: dict[str, Any]) -> dict[str, Any]:
    by: dict[str, list[dict[str, Any]]] = {}
    for c in claims:
        if c.get("availability") == "available":
            by.setdefault(c["field"], []).append(c)

    def one(f: str) -> dict[str, Any] | None:
        v = by.get(f)
        return v[0] if v else None

    paras: list[dict[str, Any]] = []
    flags: list[dict[str, Any]] = []

    # --- what it is
    name = (one("legal_name") or {}).get("value") or ident.get("name")
    brand = one("public_brand_name")
    form = one("legal_form")
    ind = one("industry_nace")
    muni = one("municipality")
    founded = one("founded_date")
    desc = one("website_description")
    status = one("registry_status")
    cites = [x["claim_id"] for x in (brand, form, ind, muni, founded) if x]
    s = f"{name}"
    if brand and brand["value"] and brand["value"].lower() not in (name or "").lower():
        s += f" (public brand: {brand['value']})"
    s += f" is a {form['value']['label'] if form and form['value'].get('label') else 'registered entity'}"
    if muni:
        s += f" based in {muni['value'].title()}"
    if founded:
        s += f", founded {founded['value'][:4]}"
    if ind:
        s += f". Its registered primary activity is “{ind['value']['label']}” (NACE {ind['value']['code']})"
    s += "."
    if desc:
        s += f" Its own website describes it as: “{desc['value'][:220]}”."
        cites.append(desc["claim_id"])
    elif one("statutory_purpose"):
        p = one("statutory_purpose")
        s += f" Statutory purpose: “{p['value'][:200]}”."
        cites.append(p["claim_id"])
    paras.append({"topic": "what_it_does", "text": s, "claim_ids": cites})
    if status and status["value"] != "active":
        flags.append({"flag": f"registry status: {status['value']}", "severity": "high", "claim_ids": [status["claim_id"]]})

    # --- leadership
    ceo = by.get("ceo", []) + by.get("proprietor", []) + by.get("general_partner", [])
    chair = one("board_chair")
    board = by.get("board_member", [])
    aud = one("auditor")
    parts = []
    cites = []
    if ceo:
        parts.append(f"day-to-day leadership: {', '.join(c['value'].get('person_name') or c['value'].get('entity_name') for c in ceo[:2])} ({ceo[0].get('label') or 'CEO'})")
        cites += [c["claim_id"] for c in ceo[:2]]
    if chair:
        parts.append(f"board chair {chair['value'].get('person_name') or chair['value'].get('entity_name')}")
        cites.append(chair["claim_id"])
    if board:
        parts.append(f"{len(board)} other board member(s)")
        cites += [c["claim_id"] for c in board[:5]]
    if aud:
        parts.append(f"auditor {aud['value'].get('entity_name') or aud['value'].get('person_name')}")
        cites.append(aud["claim_id"])
    if parts:
        paras.append({"topic": "who_leads_it", "text": "Registered roles — " + "; ".join(parts) + ".", "claim_ids": cites})
    if not aud and (form or {}).get("value", {}).get("code") in ("AS",):
        flags.append({"flag": "no auditor registered (small-company audit opt-out is common for AS)", "severity": "info", "claim_ids": []})

    # --- where
    addr = one("registered_business_address")
    wps = by.get("registered_workplace", [])
    emp = one("registered_employees")
    t = []
    cites = []
    if addr:
        t.append(f"registered business address {addr['value']}")
        cites.append(addr["claim_id"])
    if wps:
        munis = sorted({w["value"].get("municipality") or "" for w in wps if w["value"].get("municipality")})
        t.append(f"{len(wps)} registered workplace(s)" + (f" in {', '.join(m.title() for m in munis[:5])}" if munis else ""))
        cites += [w["claim_id"] for w in wps[:5]]
    if emp:
        t.append(f"{emp['value']} employees registered in the employer register")
        cites.append(emp["claim_id"])
    if t:
        paras.append({"topic": "where_it_operates", "text": "Operations: " + "; ".join(t) + ".", "claim_ids": cites})

    # --- financials (company accounts)
    fin_years: dict[str, dict[str, Any]] = {}
    for f in ("revenue", "operating_result", "annual_result", "equity", "total_assets", "total_debt", "cash_and_bank"):
        for c in by.get(f, []):
            y = (c.get("reporting_period") or {}).get("fiscal_year")
            fin_years.setdefault(y, {})[f] = c
    if fin_years:
        ys = sorted(fin_years)
        y = ys[-1]
        cur = fin_years[y]
        prev = fin_years.get(ys[-2]) if len(ys) > 1 else None
        currency = next(iter(cur.values()))["value"].get("currency") or "NOK"
        bits = []
        cites = []
        def val(c: dict[str, Any] | None) -> float | None:
            return None if c is None else c["value"].get("amount")
        if "revenue" in cur:
            r = val(cur["revenue"])
            b = f"revenue {_nok(r, currency)}"
            if prev and "revenue" in prev:
                p = _pct(r, val(prev["revenue"]))
                if p:
                    b += f" ({p} vs {ys[-2]})"
                    cites.append(prev["revenue"]["claim_id"])
            bits.append(b)
            cites.append(cur["revenue"]["claim_id"])
        if "operating_result" in cur:
            bits.append(f"operating result {_nok(val(cur['operating_result']), currency)}")
            cites.append(cur["operating_result"]["claim_id"])
        if "annual_result" in cur:
            bits.append(f"net result {_nok(val(cur['annual_result']), currency)}")
            cites.append(cur["annual_result"]["claim_id"])
        if "equity" in cur and "total_assets" in cur and val(cur["total_assets"]):
            ratio = val(cur["equity"]) / val(cur["total_assets"]) * 100
            bits.append(f"equity {_nok(val(cur['equity']), currency)} ({ratio:.0f}% of total assets {_nok(val(cur['total_assets']), currency)})")
            cites += [cur["equity"]["claim_id"], cur["total_assets"]["claim_id"]]
            if val(cur["equity"]) < 0:
                flags.append({"flag": f"negative equity in {y} accounts", "severity": "high", "claim_ids": [cur["equity"]["claim_id"]]})
            elif ratio < 10:
                flags.append({"flag": f"thin equity ratio ({ratio:.0f}%) in {y}", "severity": "medium", "claim_ids": [cur["equity"]["claim_id"]]})
        if "annual_result" in cur and val(cur["annual_result"]) is not None and val(cur["annual_result"]) < 0:
            if prev and "annual_result" in prev and (val(prev["annual_result"]) or 0) < 0:
                flags.append({"flag": f"net loss in both {ys[-2]} and {y}", "severity": "medium",
                              "claim_ids": [cur["annual_result"]["claim_id"], prev["annual_result"]["claim_id"]]})
        if "revenue" in cur and val(cur["revenue"]) == 0:
            flags.append({"flag": f"no operating revenue reported for {y} (often a holding or dormant company)", "severity": "info",
                          "claim_ids": [cur["revenue"]["claim_id"]]})
        paras.append({"topic": "latest_numbers", "text": f"Latest filed company accounts ({y}): " + "; ".join(bits) + ".", "claim_ids": cites})
    aud_fin = one("accounts_audited")
    if aud_fin and aud_fin["value"] is False:
        flags.append({"flag": "latest accounts were not audited (audit opt-out)", "severity": "info", "claim_ids": [aud_fin["claim_id"]]})

    # --- hiring
    jobs = by.get("job_posting", [])
    careers = by.get("careers_page", [])
    if jobs or careers:
        t = []
        cites = []
        if jobs:
            titles = ", ".join(f"“{j['value'].get('title')}”" for j in jobs[:3])
            t.append(f"{len(jobs)} active job posting(s) verified to this organisation (e.g. {titles})")
            cites += [j["claim_id"] for j in jobs[:5]]
        if careers:
            t.append(f"careers page at {careers[0]['value']}")
            cites.append(careers[0]["claim_id"])
        paras.append({"topic": "hiring", "text": "Hiring signals: " + "; ".join(t) + ".", "claim_ids": cites})
    else:
        paras.append({"topic": "hiring", "text": "No verified job postings or careers page were found in the checked sources (NAV arbeidsplassen, company website). This is absence of evidence, not evidence of no hiring.", "claim_ids": []})

    # --- web + activity
    site = one("official_website")
    socials = by.get("social_profile", [])
    news = sorted(by.get("company_news", []), key=lambda c: c["value"]["date"], reverse=True)
    regc = sorted(by.get("registry_change", []), key=lambda c: c["value"]["date"], reverse=True)
    t = []
    cites = []
    if site:
        t.append(f"verified website {site['value']} ({site.get('entity_match').replace('_', ' ')})")
        cites.append(site["claim_id"])
    if socials:
        t.append("profiles on " + ", ".join(sorted({c['value']['platform'] for c in socials})))
        cites += [c["claim_id"] for c in socials]
    if news:
        t.append(f"latest website news {news[0]['value']['date']}: “{news[0]['value']['title'][:90]}”")
        cites.append(news[0]["claim_id"])
    if regc:
        t.append(f"latest registry change {regc[0]['value']['date']} ({', '.join(regc[0]['value']['fields'][:3])})")
        cites.append(regc[0]["claim_id"])
    if t:
        paras.append({"topic": "public_activity", "text": "Public footprint: " + "; ".join(t) + ".", "claim_ids": cites})

    # --- changes
    mat = [c for c in changes if c.get("materiality") in ("high", "medium")]
    if mat:
        paras.append({"topic": "changes_since_last_run", "text": "Since the previous run: " + "; ".join(c["summary"] for c in mat[:5]) + ".",
                      "claim_ids": [c.get("claim_id") for c in mat[:5] if c.get("claim_id")]})

    unknowns = [{"section": k, "availability": v["availability"], "reason": v.get("reason")} for k, v in sections.items()
                if v["availability"] != "available"]
    if unknowns:
        paras.append({"topic": "unknowns", "text": "Not established: " + "; ".join(
            f"{u['section'].replace('_', ' ')} ({u['availability'].replace('_', ' ')})" for u in unknowns) + ".", "claim_ids": []})
    headline = paras[0]["text"].split(". ")[0] + "."
    out = {"method": "deterministic_template_v1", "headline": headline, "paragraphs": paras, "flags": flags}
    llm = _llm_polish(out, claims)
    if llm:
        out["llm_brief"] = llm
    return out


def _numbers(text: str) -> set[str]:
    return set(re.findall(r"\d[\d.,]*", text))


def _llm_polish(summary: dict[str, Any], claims: list[dict[str, Any]]) -> dict[str, Any] | None:
    key = os.environ.get("SIGNALPOST_LLM_API_KEY")
    if not key:
        return None
    base = os.environ.get("SIGNALPOST_LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    model = os.environ.get("SIGNALPOST_LLM_MODEL", "gpt-4o-mini")
    src = "\n".join(p["text"] for p in summary["paragraphs"])
    prompt = ("Rewrite the following verified company facts as a concise 3-4 sentence briefing for someone deciding whether to apply, "
              "sell, partner or invest. Use ONLY these facts; do not add numbers, dates, names or judgements that are not present. "
              "Keep every number exactly as written.\n\n" + src)
    body = json.dumps({"model": model, "messages": [{"role": "user", "content": prompt}], "temperature": 0}).encode()
    try:
        req = urllib.request.Request(base + "/chat/completions", data=body, headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            text = json.load(r)["choices"][0]["message"]["content"].strip()
    except Exception:  # noqa: BLE001
        return None
    if not _numbers(text) <= _numbers(src):
        return None
    return {"model": model, "text": text, "validated": "numbers_subset_of_deterministic_brief"}
