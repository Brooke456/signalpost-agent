# Envelope schema (`signalpost-envelope/1.1`)

One JSON object per input organisation number, one per line, in input order. Always emitted (a crash or timeout
for one company yields an envelope with `availability: failed` and an error entry — never a missing row).

```jsonc
{
  "schema_version": "signalpost-envelope/1.1",
  "organisation_number": "938702675",
  "run_id": "daily-001",
  "state": "complete",                 // starter-kit compatible terminal state (complete | source_error)
  "availability": "available",         // available | not_available | blocked | not_applicable | ambiguous | failed
  "run": {"run_id": "...", "started_at": "...Z", "completed_at": "...Z", "terminal_status": "completed", "agent_version": "1.0.0", "previous_run_id": null},
  "legal_identity": {"organisation_number": "...", "legal_name": "...", "legal_form": "AS", "public_brand": "...", "municipality": "...", "official_website": "https://..."},
  "sections": {                        // the seven required sections, each with an explicit state
    "identity":     {"availability": "available", "title": "1. Legal identity and public brand", "claims": 23, "sources_checked": ["..."]},
    "financials":   {...}, "leadership": {...}, "workplaces": {...},
    "web_presence": {"availability": "ambiguous", "reason": "formue.no: e-mail domain used by 18 registered entities ...", "sources_checked": [...]},
    "hiring": {...}, "activity": {...}
  },
  "modules": {"identity": {"state": "complete", "availability": "available", "final_timestamp": "..."}},   // starter-kit style
  "summary": {"method": "deterministic_template_v1", "headline": "...", "paragraphs": [{"topic": "latest_numbers", "text": "...", "claim_ids": ["revenue:..."]}], "flags": [{"flag": "negative equity in 2025 accounts", "severity": "high", "claim_ids": [...]}]},
  "claims": [{
     "claim_id": "revenue:3f2a…",       // stable across runs: hash(org, field, stable key)
     "section": "financials", "field": "revenue",
     "value": {"amount": 914000000.0, "currency": "NOK"},
     "availability": "available", "confidence": 0.99,
     "entity_match": "exact_org_number", // or org_number_on_site | registry_declared_domain | nav_feed_employer_orgnr | linked_from_verified_site | ...
     "reporting_period": {"from": "2025-01-01", "to": "2025-12-31", "fiscal_year": "2025", "account_type": "SELSKAP"},
     "effective_date": null, "evidence_ids": ["ev-…"],
     "first_observed_at": "...Z", "last_observed_at": "...Z"
  }],
  "evidence": [{
     "id": "ev-…", "source_url": "https://data.brreg.no/regnskapsregisteret/regnskap/938702675",
     "source_class": "official_annual_accounts_api", "retrieved_at": "...Z", "http_status": 200,
     "content_sha256": "…",             // sha256 of the raw response body; body stored under state/raw/<sha>.gz
     "claim_span": "[regnskapstype=SELSKAP, regnskapsperiode 2025-01-01..2025-12-31] resultatregnskapResultat/driftsresultat/driftsinntekter/sumDriftsinntekter = 914000000.0 NOK",
     "extraction_method": "brreg_regnskap_json_path", "source_rights": "NLOD 2.0 (...)"
  }],
  "changes": [{"change_type": "new_role", "field": "ceo", "materiality": "high", "summary": "...", "previous_value": ..., "current_value": ..., "previous_evidence": [...], "current_evidence_ids": [...], "detected_at": "..."}],
  "refresh": {"previous_run_id": "...", "material_changes": 1, "carried_forward_claims": 0, "is_first_observation": false},
  "errors": [{"module": "website", "message": "..."}],
  "operations": {"runtime_ms": 8123, "third_party_cost_usd": 0}
}
```

Rules
* Missing information is a section state (`not_available`, `blocked`, `ambiguous`, `failed`) — never a zero value.
* Money values are exactly as filed (NOK unless the filing currency differs); no estimation, no unit conversion.
* `ambiguous` claims (e.g. several social profiles on one platform) are kept for review with `availability: "ambiguous"` and are not counted as published facts.
