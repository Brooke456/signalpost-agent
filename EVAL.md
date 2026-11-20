# Evaluation

All numbers below come from files committed in `reports/`. The smoke test was produced from a **clean clone** of this
repository at the submitted code (`uv sync --frozen`, then the one command in the README).

## 100-company smoke test — `reports/smoke-100/`

Batch: `tests/smoke-companies.jsonl` = the starter kit's `select_entry_batch.py --count 100` (seed 20260823) over the public
411,160-company universe (91 AS, 3 BRL, 2 ESEK, 2 STI, 1 SA, 1 ANS; only 7 declare a website in the registry, 10 have registered employees).

* Envelopes: **100/100**, validation passed: True (exact_expected_count, one_envelope_per_input, unique_organisation_numbers, all_states_terminal, every_claim_has_evidence, evidence_references_resolve).
* Wall clock 217.7 s with 24 workers (includes loading the 1.18 M-row registry snapshot and reading the NAV feed: 228 pages, 9805 active ads). Requests: 1253; per-company p50 11.5 s, p95 49.2 s. Third-party cost: USD 0.
* Published claims: 7286, every one with resolvable evidence.

| Section | companies available | claims | states |
|---|---|---|---|
| identity | 100 | 1757 | available 100 |
| financials | 100 | 4812 | available 100 |
| leadership | 100 | 385 | available 100 |
| workplaces | 74 | 77 | available 74, not_available 26 |
| web_presence | 10 | 54 | available 10, not_available 82, ambiguous 5, failed 3 |
| hiring | 0 | 0 | not_available 100 |
| activity | 100 | 201 | available 100 |

Selected fields (claims / companies): official website 10/10, social profiles 7/3, website news 24/3, LEI 21/21, CEO 64/64, board chair 99/99, registered workplaces 77/74, revenue (company, up to 3 years) 200/75, dated registry changes 177/100.
No job ads were verified for these 100 (90 have no registered employees). Website `ambiguous` cases are shared e-mail/employer/group domains deliberately withheld (e.g. a holding company using its owner's employer domain).

### Precision audit — `reports/smoke-100/precision-audit.json`
`scripts/audit_precision.py` re-derives every published claim of 35 randomly sampled companies (seed 7) with fresh requests
and separate code: 2544 claims; **1952 correct, 0 wrong** on machine-checkable claims
(identity, every financial value per fiscal year, roles, workplaces, shares, jobs, websites, social links); 1 website needed
manual review and was confirmed correct (registered address printed on the site). 591 descriptive claims (statutory purpose,
flags, registry-change events, PDF links) are copied verbatim from the same registry responses and not re-checked by the script.
**Measured precision: 100 % (0 wrong-company publications).**

### Refresh — `reports/smoke-100/refresh-check.json`
Second run against the first run's state: 100/100 companies matched to their previous snapshot, **0 changes** (no false changes),
100/100 identical claim-id sets; the first snapshot is preserved. Typed changes are covered by unit tests.

## Development check on employers — `reports/dev-employers-40/`
40 random companies with ≥10 registered employees (seed 11), produced during development:

| Section | companies available | claims | states |
|---|---|---|---|
| identity | 40 | 819 | available 40 |
| financials | 40 | 2320 | available 40 |
| leadership | 40 | 263 | available 40 |
| workplaces | 40 | 156 | available 40 |
| web_presence | 22 | 150 | not_available 15, available 22, failed 1, ambiguous 1, blocked 1 |
| hiring | 10 | 22 | not_available 30, available 10 |
| activity | 40 | 401 | available 40 |

Audit of all 40: 4131 claims, **2917 correct, 0 wrong**; 10 web claims reviewed manually, all correct (notes in the file).

## Budget behaviour
`--time-budget-seconds` (default 5400 s) is a soft deadline: website and hiring steps stop early, and every company still
receives a terminal envelope; a hard stop 30 s before the budget emits `failed` envelopes for anything unfinished.
Expected for a 1,000-company batch: ~15–25 min, ~12–13 k requests, USD 0.
