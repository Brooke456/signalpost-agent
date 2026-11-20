"""Claim + evidence builders. Every published claim points at evidence with source URL,
retrieval time, content hash, extraction method and a claim span."""
from __future__ import annotations

import hashlib
import json
from typing import Any

STATES = ("available", "not_available", "blocked", "not_applicable", "ambiguous", "failed")

SECTIONS = {
    "identity": "1. Legal identity and public brand",
    "financials": "2. Latest annual accounts and available history",
    "leadership": "3a. Leadership and governance (official roles)",
    "workplaces": "3b. Registered workplaces (sub-units)",
    "web_presence": "4. Verified official website and company-owned profiles",
    "hiring": "5a. Hiring (job postings)",
    "activity": "5b. Dated public activity",
}


def _h(*parts: Any, n: int = 16) -> str:
    raw = "|".join(json.dumps(p, sort_keys=True, ensure_ascii=False, default=str) for p in parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:n]


class Collector:
    """Accumulates claims and evidence for one company."""

    def __init__(self, org: str) -> None:
        self.org = org
        self.claims: list[dict[str, Any]] = []
        self.evidence: dict[str, dict[str, Any]] = {}
        self.section_state: dict[str, dict[str, Any]] = {}
        self.errors: list[dict[str, Any]] = []
        self._keys: set[str] = set()

    # ------------------------------------------------------------------ evidence
    def ev(self, *, url: str, source_class: str, retrieved_at: str, sha256: str | None, span: str,
           method: str, final_url: str | None = None, http_status: int | None = None,
           rights: str | None = None) -> str:
        eid = "ev-" + _h(url, sha256, span, n=14)
        if eid not in self.evidence:
            rec = {
                "id": eid,
                "source_url": url,
                "source_class": source_class,
                "retrieved_at": retrieved_at,
                "content_sha256": sha256,
                "claim_span": span[:600],
                "extraction_method": method,
            }
            if final_url and final_url != url:
                rec["final_url"] = final_url
            if http_status is not None:
                rec["http_status"] = http_status
            if rights:
                rec["source_rights"] = rights
            self.evidence[eid] = rec
        return eid

    # ------------------------------------------------------------------ claims
    def claim(self, section: str, field: str, value: Any, evidence_ids: list[str], *, key: Any = None,
              confidence: float = 0.99, availability: str = "available", reporting_period: Any = None,
              effective_date: str | None = None, entity_match: str = "exact_org_number", note: str | None = None,
              label: str | None = None) -> dict[str, Any] | None:
        if value is None or value == "" or value == []:
            return None
        stable = key if key is not None else value
        cid = f"{field}:" + _h(self.org, field, stable, n=12)
        if cid in self._keys:
            # merge evidence into the existing claim
            for c in self.claims:
                if c["claim_id"] == cid:
                    for e in evidence_ids:
                        if e not in c["evidence_ids"]:
                            c["evidence_ids"].append(e)
            return None
        self._keys.add(cid)
        rec: dict[str, Any] = {
            "claim_id": cid,
            "section": section,
            "field": field,
            "value": value,
            "availability": availability,
            "confidence": round(confidence, 3),
            "entity_match": entity_match,
            "evidence_ids": list(evidence_ids),
        }
        if label:
            rec["label"] = label
        if reporting_period:
            rec["reporting_period"] = reporting_period
        if effective_date:
            rec["effective_date"] = effective_date
        if note:
            rec["note"] = note
        self.claims.append(rec)
        return rec

    # ------------------------------------------------------------------ sections
    def section(self, name: str, state: str, *, reason: str | None = None, sources: list[str] | None = None,
                override: bool = False) -> None:
        assert state in STATES, state
        cur = self.section_state.get(name)
        rank = {"available": 5, "ambiguous": 4, "blocked": 3, "failed": 2, "not_available": 1, "not_applicable": 0}
        if cur and not override and rank[cur["availability"]] > rank[state]:
            if sources:
                cur.setdefault("sources_checked", [])
                cur["sources_checked"] = sorted(set(cur["sources_checked"]) | set(sources))
            return
        rec = {"availability": state}
        if reason:
            rec["reason"] = reason
        srcs = sorted(set((cur or {}).get("sources_checked", [])) | set(sources or []))
        if srcs:
            rec["sources_checked"] = srcs
        self.section_state[name] = rec

    def error(self, module: str, message: str, url: str | None = None) -> None:
        self.errors.append({"module": module, "message": message[:300], **({"url": url} if url else {})})
