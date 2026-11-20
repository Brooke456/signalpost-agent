#!/usr/bin/env python3
"""Independent precision audit: re-derive every published claim for a random sample of companies
directly from the authoritative source (fresh requests, separate code path) and count correct / wrong.

usage: python scripts/audit_precision.py --envelopes out/envelopes.jsonl --sample 30 --seed 7 --output reports/audit.json
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sys
import urllib.request
from pathlib import Path

UA = {"User-Agent": "SignalpostAudit/1.0", "Accept": "application/json"}


def get(url: str, headers: dict | None = None, raw: bool = False):
    req = urllib.request.Request(url, headers={**UA, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            b = r.read()
            return b if raw else json.loads(b)
    except Exception as exc:  # noqa: BLE001
        return {"__error__": str(exc)}


def dig(d, *p):
    for k in p:
        if not isinstance(d, dict):
            return None
        d = d.get(k)
    return d


FIN = {
    "revenue": ("resultatregnskapResultat", "driftsresultat", "driftsinntekter", "sumDriftsinntekter"),
    "sales_revenue": ("resultatregnskapResultat", "driftsresultat", "driftsinntekter", "salgsinntekter"),
    "operating_expenses": ("resultatregnskapResultat", "driftsresultat", "driftskostnad", "sumDriftskostnad"),
    "payroll_expenses": ("resultatregnskapResultat", "driftsresultat", "driftskostnad", "loennskostnad"),
    "operating_result": ("resultatregnskapResultat", "driftsresultat", "driftsresultat"),
    "net_financial_items": ("resultatregnskapResultat", "finansresultat", "nettoFinans"),
    "profit_before_tax": ("resultatregnskapResultat", "ordinaertResultatFoerSkattekostnad"),
    "tax_expense": ("resultatregnskapResultat", "ordinaertResultatSkattekostnad"),
    "annual_result": ("resultatregnskapResultat", "aarsresultat"),
    "total_assets": ("eiendeler", "sumEiendeler"),
    "fixed_assets": ("eiendeler", "anleggsmidler", "sumAnleggsmidler"),
    "current_assets": ("eiendeler", "omloepsmidler", "sumOmloepsmidler"),
    "cash_and_bank": ("eiendeler", "sumBankinnskuddOgKontanter"),
    "equity": ("egenkapitalGjeld", "egenkapital", "sumEgenkapital"),
    "total_debt": ("egenkapitalGjeld", "gjeldOversikt", "sumGjeld"),
    "short_term_debt": ("egenkapitalGjeld", "gjeldOversikt", "kortsiktigGjeld", "sumKortsiktigGjeld"),
    "long_term_debt": ("egenkapitalGjeld", "gjeldOversikt", "langsiktigGjeld", "sumLangsiktigGjeld"),
}
ROLEMAP = {"ceo": "DAGL", "board_chair": "LEDE", "board_deputy_chair": "NEST", "board_member": "MEDL", "board_deputy_member": "VARA",
           "auditor": "REVI", "accountant": "REGN", "proprietor": "INNH", "contact_person": "KONT", "board_observer": "OBS"}


def digits(s):
    return re.sub(r"\D", "", str(s or ""))


def audit_company(env: dict) -> list[dict]:
    org = env["organisation_number"]
    ent = get(f"https://data.brreg.no/enhetsregisteret/api/enheter/{org}")
    roles = get(f"https://data.brreg.no/enhetsregisteret/api/enheter/{org}/roller")
    acc = get(f"https://data.brreg.no/regnskapsregisteret/regnskap/{org}")
    out = []
    page_cache: dict[str, str] = {}

    def page(url: str) -> str:
        if url not in page_cache:
            b = get(url, {"Accept": "text/html", "User-Agent": "Mozilla/5.0 SignalpostAudit"}, raw=True)
            page_cache[url] = b.decode("utf-8", "replace") if isinstance(b, bytes) else ""
        return page_cache[url]

    for c in env["claims"]:
        if c.get("availability") != "available":
            continue
        f, v = c["field"], c["value"]
        verdict, why = "unverifiable", ""
        try:
            if f == "legal_name":
                verdict = "correct" if v == ent.get("navn") else "wrong"
            elif f == "organisation_number":
                verdict = "correct" if v == ent.get("organisasjonsnummer") else "wrong"
            elif f == "legal_form":
                verdict = "correct" if v["code"] == dig(ent, "organisasjonsform", "kode") else "wrong"
            elif f == "municipality":
                verdict = "correct" if v in (dig(ent, "forretningsadresse", "kommune"), dig(ent, "postadresse", "kommune")) else "wrong"
            elif f == "industry_nace":
                verdict = "correct" if v["code"] == dig(ent, "naeringskode1", "kode") else "wrong"
            elif f == "founded_date":
                verdict = "correct" if v == ent.get("stiftelsesdato") else "wrong"
            elif f == "registered_business_address":
                a = ent.get("forretningsadresse") or {}
                verdict = "correct" if all(x in v for x in (a.get("adresse") or [])[:1] + [a.get("postnummer") or ""]) else "wrong"
            elif f == "registered_employees":
                verdict = "correct" if v == ent.get("antallAnsatte") else "wrong"
            elif f == "share_capital":
                verdict = "correct" if abs(float(v["amount"]) - float(dig(ent, "kapital", "belop") or -1)) < 0.01 else "wrong"
            elif f == "registry_status":
                st = "bankrupt" if ent.get("konkurs") else "under_liquidation" if ent.get("underAvvikling") else "under_forced_dissolution" if ent.get("underTvangsavviklingEllerTvangsopplosning") else "active"
                verdict = "correct" if v == st else "wrong"
            elif f in ("registry_declared_website",):
                verdict = "correct" if v == ent.get("hjemmeside") else "wrong"
            elif f in ROLEMAP or f in ("general_partner", "business_manager", "registered_role"):
                found = False
                for g in roles.get("rollegrupper", []) or []:
                    for r in g.get("roller", []) or []:
                        if r.get("avregistrert") or r.get("fratraadt"):
                            continue
                        nm = r.get("person", {}).get("navn") or {}
                        name = " ".join(x for x in [nm.get("fornavn"), nm.get("mellomnavn"), nm.get("etternavn")] if x)
                        en = r.get("enhet") or {}
                        ename = " ".join(en.get("navn") or []) if isinstance(en.get("navn"), list) else en.get("navn")
                        if dig(r, "type", "kode") == v.get("role_code") and (name == v.get("person_name") or ename == v.get("entity_name")):
                            found = True
                verdict = "correct" if found else "wrong"
            elif f.replace("group_", "") in FIN:
                key = f.replace("group_", "")
                typ = "KONSERN" if f.startswith("group_") else "SELSKAP"
                fy = c["reporting_period"]["fiscal_year"]
                recs = [r for r in acc if isinstance(acc, list) and r.get("regnskapstype") == typ and (dig(r, "regnskapsperiode", "tilDato") or "")[:4] == fy]
                if recs:
                    verdict = "correct" if dig(recs[0], *FIN[key]) == v["amount"] else "wrong"
            elif f == "registered_workplace":
                u = get(f"https://data.brreg.no/enhetsregisteret/api/underenheter/{v['organisation_number']}")
                verdict = "correct" if u.get("overordnetEnhet") == org and u.get("navn") == v.get("name") else "wrong"
            elif f == "official_website":
                html = page(v)
                spaced = f"{org[:3]} {org[3:6]} {org[6:]}"
                reg = (ent.get("hjemmeside") or "").lower().replace("www.", "")
                em = (ent.get("epostadresse") or "").lower().split("@")[-1]
                host = re.sub(r"^https?://(www\.)?", "", v).split("/")[0]
                if org in digits(html) and (org in html or spaced in html or org in html.replace("\u00a0", " ").replace(" ", "")):
                    verdict, why = "correct", "org number on site"
                elif reg and host.endswith(reg.split("/")[0]):
                    verdict, why = "correct", "matches registry-declared website"
                elif em and host.endswith(em):
                    verdict, why = "needs_review", "registry e-mail domain (name match)"
                else:
                    verdict, why = "needs_review", "no org number in raw HTML"
            elif f == "social_profile":
                ev = {e["id"]: e for e in env["evidence"]}
                src = ev[c["evidence_ids"][0]]
                if src["source_class"] == "company_owned_website":
                    html = page(src["source_url"])
                    verdict = "correct" if v["handle"].lower() in html.lower() else "needs_review"
                else:
                    verdict, why = "needs_review", src["source_class"]
            elif f == "job_posting":
                verdict, why = "needs_review", c.get("entity_match")
                ev = {e["id"]: e for e in env["evidence"]}
                for i in c["evidence_ids"]:
                    if ev[i]["source_class"] == "nav_job_feed_api":
                        tok = re.search(rb"eyJ[\w\-]+\.[\w\-]+\.[\w\-]+", get("https://pam-stilling-feed.nav.no/api/publicToken", raw=True) or b"")
                        if tok:
                            d = get(ev[i]["source_url"], {"Authorization": "Bearer " + tok.group(0).decode()})
                            o = digits(dig(d, "ad_content", "employer", "orgnr"))
                            subs = get(f"https://data.brreg.no/enhetsregisteret/api/underenheter?overordnetEnhet={org}&size=200")
                            so = {x.get("organisasjonsnummer") for x in dig(subs, "_embedded", "underenheter") or []}
                            verdict = "correct" if o == org or o in so else "wrong"
            else:
                verdict = "unverifiable"
        except Exception as exc:  # noqa: BLE001
            verdict, why = "error", str(exc)[:120]
        out.append({"org": org, "field": f, "verdict": verdict, "why": why, "value": json.dumps(v, ensure_ascii=False)[:160]})
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--envelopes", required=True)
    ap.add_argument("--sample", type=int, default=30)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--output", required=True)
    a = ap.parse_args()
    envs = [json.loads(l) for l in Path(a.envelopes).read_text(encoding="utf-8").splitlines() if l.strip()]
    sample = random.Random(a.seed).sample(envs, min(a.sample, len(envs)))
    rows = []
    for e in sample:
        rows += audit_company(e)
        print(e["organisation_number"], file=sys.stderr, flush=True)
    by_field: dict[str, dict[str, int]] = {}
    for r in rows:
        by_field.setdefault(r["field"], {}).setdefault(r["verdict"], 0)
        by_field[r["field"]][r["verdict"]] += 1
    correct = sum(1 for r in rows if r["verdict"] == "correct")
    wrong = sum(1 for r in rows if r["verdict"] == "wrong")
    report = {"companies": len(sample), "claims_checked": len(rows), "correct": correct, "wrong": wrong,
              "needs_review": sum(1 for r in rows if r["verdict"] == "needs_review"),
              "unverifiable_by_script": sum(1 for r in rows if r["verdict"] in ("unverifiable", "error")),
              "precision_on_machine_checked": round(correct / (correct + wrong), 4) if correct + wrong else None,
              "by_field": by_field, "wrong_rows": [r for r in rows if r["verdict"] == "wrong"],
              "review_rows": [r for r in rows if r["verdict"] == "needs_review"]}
    Path(a.output).parent.mkdir(parents=True, exist_ok=True)
    Path(a.output).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k not in ("wrong_rows", "review_rows", "by_field")}, indent=2))


if __name__ == "__main__":
    main()
