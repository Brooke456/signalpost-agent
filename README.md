# Signalpost agent — exact-entity, evidence-first Norwegian company profiles

Give it Norwegian organisation numbers; it returns **exactly one terminal envelope per company** with a
decision-useful profile (identity & brand, latest filed accounts with history, leadership, registered workplaces,
verified website & company-owned profiles, hiring, dated activity), where **every fact carries its source URL,
retrieval time, sha256 of the raw response, extraction span and reporting period**. Missing information is an explicit
state (`not_available`, `blocked`, `ambiguous`, `failed`), never a zero.

Design goal: **precision first**. Every source is keyed by the organisation number (Brønnøysund APIs, NAV job ads by
employer org number, Wikidata P2333, GLEIF `registeredAs`), and a website is only published after an identity gate
(org number printed on the site, or a domain the company itself declared to an org-number-keyed register with no
contradiction). Shared, franchise, accountant and employer domains are reported `ambiguous` instead of guessed.

No API keys, no LLM and no third-party spend are required (cost per official run: **USD 0**).

## One command

```bash
uv sync --frozen && uv run python scripts/run_competition_batch.py \
  --organisations batch.jsonl --bulk brreg-enheter.csv \
  --output out/envelopes.jsonl --profiles-output out/profiles.jsonl --report out/run-report.json \
  --site out/index.html --run-id daily-001 --expected-count 1000
```

* Same flags as the Builderr starter kit's `run_competition_batch.py` (extra starter flags such as `--modules`/`--resume` are accepted and ignored).
* `--organisations`: JSON list, `{"organisation_numbers": [...]}`, JSONL (`{"organisation_number": "..."}` per line) or plain text.
* `--bulk` (optional): the frozen Brreg entity snapshot (CSV, CSV.gz, or the Signalpost universe JSONL.gz). Used as identity fallback and for the name-uniqueness / shared-domain identity gates. Without it the agent uses the live API only.
* `--state-dir` (default `state/`): immutable run snapshots, raw response bodies by sha256, refresh index. Re-run with a new `--run-id` to refresh; changes since the previous run appear in each envelope's `changes`.
* `--time-budget-seconds` (default 5400, env `SIGNALPOST_TIME_BUDGET_S`): soft deadline; every company still gets an envelope (remaining steps are marked `blocked`/`failed`).
* `--workers` (default 24). Equivalent without the wrapper: `uv run python -m signalpost ...`.
* pip alternative: `python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt` (same pinned versions as `uv.lock`).

Exit code 0 when the batch contract validates (count, uniqueness, terminal states, every claim has resolvable evidence).

## 100-company smoke test (committed)

`reports/smoke-100/` — run on the 100 companies selected by the starter kit's `select_entry_batch.py` (seed 20260823)
from the public 411,160-company universe:

* `envelopes.jsonl` — the 100 terminal envelopes
* `run-report.json` — coverage per section and field, request counts, p50/p95 latency, validation
* `precision-audit.json` — independent re-derivation of every claim for 35 random companies (see below)
* `web-audit-all.json` — re-audit of every website and social claim in the run
* `refresh-check.json` — idempotent re-run (second run against the first run's state)
* `index.html` — the viewer for these 100 profiles (search, profile, compare two, expand any fact to its source)

Browse the smoke-test viewer: https://raw.githack.com/Brooke456/signalpost-agent/main/reports/smoke-100/index.html
(GitHub Pages copy: `docs/smoke-100/index.html`). The v2 smoke test was run from a clean clone at commit `8bb5c31`;
later commits only add reports and documentation (no code change). See `EVAL.md` for the numbers and the v1 → v2 comparison
(`reports/v1-vs-v2.json`).

**v2.0.0** raises website / social / hiring recall without loosening the identity gate: more name-derived and sub-unit
website candidates (still published only on org number, or legal name + registry phone/address/e-mail), directory and
chain-page guards, and an exact org-number index over every active NAV feed entry (main unit + sub-units).

## Viewer (UX)

`--site out/index.html` writes a single self-contained, mobile-responsive page: search by name / org number /
municipality / industry, filter by evidence type, per-section availability badges, a cited brief with risk flags,
a multi-year accounts table, "source ▸" on every fact (URL, source class, retrieval time, sha256 prefix, exact span),
changes since the previous run, and side-by-side comparison of two companies.

## Docs
* `docs/AGENT.md` — research, identity-gate and abstention policy
* `docs/SOURCES.md` — every source, access method, match key, licence; outbound URL safety; secrets
* `docs/DATA_SCHEMA.md` — envelope, claim and evidence schema
* `docs/REFRESH.md` — snapshots, typed diffs, carry-forward on failure, idempotency
* `EVAL.md` — smoke-test coverage, precision audit, runtime and cost
* `docs/LIMITATIONS.md`

## Models / APIs / licences
* Models: none by default. Optional `SIGNALPOST_LLM_API_KEY` (+`SIGNALPOST_LLM_BASE_URL`, `SIGNALPOST_LLM_MODEL`, OpenAI-compatible) only rephrases the deterministic brief and is rejected if it introduces any number not in the facts.
* APIs: Brønnøysund Enhetsregisteret + Regnskapsregisteret (NLOD 2.0), NAV job-vacancy feed (public token, NAV API terms) and arbeidsplassen search, Wikidata SPARQL (CC0), GLEIF LEI API (CC0), company-owned websites (robots.txt respected).
* Code: MIT. Dependencies: beautifulsoup4 (MIT), lxml (BSD), soupsieve (MIT), typing-extensions (PSF).

## Tests
`uv run python -m unittest discover -s tests` — identity-gate regressions (shared e-mail domain, franchise domain,
contradicting org number, unverified guesses), social-link normalisation, refresh idempotency and typed role changes,
terminal envelope for a crashed company.
