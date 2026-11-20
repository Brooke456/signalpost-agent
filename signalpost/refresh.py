"""Idempotent refresh: immutable per-run snapshots + typed, evidence-backed diffs.

state/
  runs/<run_id>/envelopes.jsonl   immutable once written (a re-run with the same run_id and identical claims is a no-op)
  index.json                      org -> [run_ids...] (latest last)
  raw/                            content-addressed raw HTTP bodies (sha256.gz)
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

SINGLE = {"legal_name", "legal_form", "registered_business_address", "postal_address", "municipality", "industry_nace",
          "registered_employees", "registry_status", "share_capital", "statutory_purpose", "registry_declared_website",
          "registry_phone", "registry_email", "official_website", "website_description", "public_brand_name", "website_title",
          "parent_company", "group_ultimate_parent", "part_of_group", "vat_registered", "active_job_postings_count",
          "fiscal_years_available"}
HIGH = {"legal_name", "registry_status", "ceo", "board_chair", "official_website", "parent_company", "legal_form", "fiscal_years_available",
        "proprietor", "auditor"}
MEDIUM = {"registered_business_address", "industry_nace", "registered_employees", "share_capital", "board_member", "registered_workplace",
          "job_posting", "social_profile", "website_description", "public_brand_name", "subsidiary", "general_partner"}
IGNORE = {"active_job_postings_count", "registry_profile_url", "website_title", "website_language", "accounts_audited"}


def slot(c: dict[str, Any]) -> str:
    if c["field"] in SINGLE:
        return c["field"]
    return c["claim_id"]


def change_type(field: str, kind: str) -> str:
    if field in ("ceo", "board_chair", "board_member", "board_deputy_member", "auditor", "accountant", "proprietor", "general_partner",
                 "board_deputy_chair", "contact_person"):
        return {"added": "new_role", "removed": "ended_role", "changed": "changed_role"}[kind]
    if field == "registered_workplace":
        return {"added": "new_location", "removed": "closed_location", "changed": "changed_location"}[kind]
    if field == "job_posting":
        return {"added": "new_job", "removed": "closed_job", "changed": "changed_job"}[kind]
    if field == "fiscal_years_available" or field == "annual_accounts_copy":
        return "new_filing" if kind != "removed" else "removed_filing"
    if field in ("website_description",):
        return "changed_description"
    if field in ("company_news", "registry_change"):
        return "new_activity" if kind == "added" else "aged_out_activity"
    return f"{kind}_{field}"


class Store:
    def __init__(self, root: Path) -> None:
        self.root = root
        (root / "runs").mkdir(parents=True, exist_ok=True)
        self.index_path = root / "index.json"
        self.index: dict[str, list[str]] = json.loads(self.index_path.read_text()) if self.index_path.exists() else {}
        self._cache: dict[str, dict[str, dict[str, Any]]] = {}

    def previous(self, org: str, run_id: str) -> dict[str, Any] | None:
        runs = [r for r in self.index.get(org, []) if r != run_id]
        if not runs:
            return None
        last = runs[-1]
        if last not in self._cache:
            p = self.root / "runs" / last / "envelopes.jsonl"
            data: dict[str, dict[str, Any]] = {}
            if p.exists():
                for line in p.read_text(encoding="utf-8").splitlines():
                    if line.strip():
                        e = json.loads(line)
                        data[e["organisation_number"]] = e
            self._cache[last] = data
        return self._cache[last].get(org)

    def save_run(self, run_id: str, envelopes: list[dict[str, Any]]) -> dict[str, Any]:
        d = self.root / "runs" / run_id
        d.mkdir(parents=True, exist_ok=True)
        p = d / "envelopes.jsonl"
        if p.exists():
            # never overwrite a frozen snapshot; write a sibling attempt instead
            n = 2
            while (d / f"envelopes.attempt{n}.jsonl").exists():
                n += 1
            p = d / f"envelopes.attempt{n}.jsonl"
        with p.open("w", encoding="utf-8") as fh:
            for e in envelopes:
                fh.write(json.dumps(e, ensure_ascii=False, separators=(",", ":")) + "\n")
        for e in envelopes:
            lst = self.index.setdefault(e["organisation_number"], [])
            if run_id not in lst:
                lst.append(run_id)
        self.index_path.write_text(json.dumps(self.index, indent=0))
        return {"snapshot": str(p)}


def diff(prev: dict[str, Any] | None, claims: list[dict[str, Any]], sections: dict[str, Any], now: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return (changes, carried_forward_claims)."""
    if not prev:
        return [], []
    old = [c for c in prev.get("claims", []) if c.get("availability") == "available"]
    old_by: dict[str, list[dict[str, Any]]] = {}
    for c in old:
        old_by.setdefault(slot(c), []).append(c)
    new_by: dict[str, list[dict[str, Any]]] = {}
    for c in claims:
        if c.get("availability") == "available":
            new_by.setdefault(slot(c), []).append(c)
    changes: list[dict[str, Any]] = []
    carried: list[dict[str, Any]] = []
    prev_ev = {e["id"]: e for e in prev.get("evidence", [])}
    failed_sections = {k for k, v in sections.items() if v.get("availability") in ("failed", "blocked")}
    for s in sorted(set(old_by) | set(new_by)):
        o = old_by.get(s, [])
        n = new_by.get(s, [])
        field = (o or n)[0]["field"]
        section = (o or n)[0]["section"]
        if field in IGNORE:
            continue
        if o and not n:
            if section in failed_sections:
                for c in o:
                    cc = dict(c)
                    cc["carried_forward"] = True
                    cc["note"] = "Source failed this run; last supported value kept from previous snapshot."
                    carried.append(cc)
                continue
            for c in o:
                changes.append(_chg(field, "removed", c, None, prev_ev, now))
        elif n and not o:
            for c in n:
                changes.append(_chg(field, "added", None, c, prev_ev, now))
        else:
            ov = json.dumps([c["value"] for c in o], sort_keys=True, ensure_ascii=False)
            nv = json.dumps([c["value"] for c in n], sort_keys=True, ensure_ascii=False)
            if ov != nv:
                changes.append(_chg(field, "changed", o[0], n[0], prev_ev, now))
    return changes, carried


def _short(v: Any) -> str:
    if isinstance(v, dict):
        for k in ("person_name", "entity_name", "title", "name", "label", "url", "amount"):
            if v.get(k) is not None:
                return str(v[k])[:80]
        return json.dumps(v, ensure_ascii=False)[:80]
    return str(v)[:80]


def _chg(field: str, kind: str, old: dict[str, Any] | None, new: dict[str, Any] | None, prev_ev: dict[str, Any], now: str) -> dict[str, Any]:
    mat = "high" if field in HIGH else "medium" if field in MEDIUM else "low"
    if field in ("registry_change", "company_news"):
        mat = "low"
    label = field.replace("_", " ")
    if kind == "added":
        summary = f"new {label}: {_short(new['value'])}"
    elif kind == "removed":
        summary = f"{label} no longer present: {_short(old['value'])}"
    else:
        summary = f"{label} changed from {_short(old['value'])} to {_short(new['value'])}"
    return {
        "change_type": change_type(field, kind),
        "field": field,
        "materiality": mat,
        "summary": summary,
        "previous_value": old["value"] if old else None,
        "current_value": new["value"] if new else None,
        "previous_claim_id": old["claim_id"] if old else None,
        "claim_id": new["claim_id"] if new else None,
        "previous_evidence": [prev_ev[e] for e in (old or {}).get("evidence_ids", []) if e in prev_ev][:2],
        "current_evidence_ids": (new or {}).get("evidence_ids", []),
        "first_observed_previous": (old or {}).get("first_observed_at"),
        "detected_at": now,
    }
