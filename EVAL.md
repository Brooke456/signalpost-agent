# Evaluation (v2.0.0)

All numbers below come from files committed in `reports/`. Both runs were produced from a **clean clone** of this
repository at the v2 code commit `8bb5c313df2f27a6741d21efce9f7b93556a8ef3` (`uv sync --frozen`, then the one command in the README).
v1 numbers are from commit `34ac4ec` (same batch files, same commands); the side-by-side is in `reports/v1-vs-v2.json`.

## What changed in v2 (recall without loosening the identity gate)
* **More website candidates, same strict gate.** Name-derived `.no`/`.com` domains are now built from the legal name, the
  company's distinct registered sub-unit (trading) names and its two latest earlier legal names, with both Norwegian
  transliterations (ø→o/oe, å→a/aa), generic-word stripping, first-two-words, compound stems (`altaskifer` for
  "Alta Skiferprodukter") and the distinctive first word. Websites / e-mail domains declared on the company's own
  sub-units are new registry candidates. A guessed domain is still only published when the site prints the organisation
  number, or the legal name together with the registry phone, the registered street address + postcode, or the registry
  e-mail on that domain (new), and no other organisation number.
* **New precision guards**: guessed pages listing ≥3 other org numbers (directories, group listings) are rejected; a store
  page on a chain/franchise domain is published as that page only (no chain socials/news/careers attributed); social
  links, careers pages and news on a company section of a shared site are not attributed to the company.
* **Hiring**: every active NAV feed entry is read once per run (cached by uuid + modification time) and indexed by
  employer org number, so ads are matched exactly via the main unit or any sub-unit, even when the ad's business name
  differs from the legal name (v1 only looked at ads whose business name matched).
* **Crawl**: the identity check now visits one page per category first (contact, about, terms/privacy, careers, news)
  and up to 5 pages for guessed domains; malformed markup no longer aborts a company (lxml → html.parser fallback).

Not used, on purpose: search-engine scraping (Google/Bing/DuckDuckGo terms prohibit automated queries; no paid key is
assumed) and Norid RDAP/whois (holder not exposed publicly, entity search needs authorisation, commercial use prohibited).

## 100-company smoke test — `reports/smoke-100/`

Batch: `tests/smoke-companies.jsonl` = the starter kit's `select_entry_batch.py --count 100` (seed 20260823) over the public
411,160-company universe (91 AS, 3 BRL, 2 ESEK, 2 STI, 1 SA, 1 ANS; only 7 declare a website in the registry, 10 have registered employees).

* Envelopes: **100/100**, validation passed: True (exact_expected_count, one_envelope_per_input, unique_organisation_numbers, all_states_terminal, every_claim_has_evidence, evidence_references_resolve).
* Wall clock 455.6 s with 24 workers (includes loading the 1.18 M-row registry snapshot and reading the NAV feed: 228 pages,
  9,804 active ads, every feed entry read once → 6,035 employer org numbers indexed). Requests: 11,567 (≈9.9 k of them NAV
  feed entries, a fixed per-run cost; ≈1.7 k for the companies). Per-company p50 16.1 s, p95 76.3 s. Third-party cost: USD 0.
* Published claims: 7,299, every one with resolvable evidence.

| Section | companies available | claims | states |
|---|---|---|---|
| identity | 100 | 1757 | available 100 |
| financials | 100 | 4812 | available 100 |
| leadership | 100 | 385 | available 100 |
| workplaces | 74 | 77 | available 74, not_available 26 |
| web_presence | 12 | 59 | available 12, not_available 81, ambiguous 6, failed 1 |
| hiring | 0 | 0 | not_available 100 |
| activity | 100 | 209 | available 100 |

| v1 → v2 (smoke 100) | v1 | v2 |
|---|---|---|
| verified official website | 10 | **12** |
| companies with company-owned social profiles (claims) | 3 (7) | 3 (7) |
| companies with verified job ads / careers page | 0 / 0 | 0 / 0 |
| website `ambiguous` (withheld) | 5 | 6 |

Selected fields (claims / companies): official website 12/12, social profiles 7/3, website news 32/4, LEI 21/21, CEO 64/64,
board chair 99/99, registered workplaces 77/74, revenue (company, up to 3 years) 200/75, dated registry changes 177/100.
No job ads exist in the NAV feed for any of these 100 org numbers or their sub-units (90 have no registered employees).
Website `ambiguous` cases are shared e-mail/employer/group domains deliberately withheld.

### Precision audit — `reports/smoke-100/precision-audit.json`, `reports/smoke-100/web-audit-all.json`
`scripts/audit_precision.py` re-derives every published claim of 35 randomly sampled companies (seed 7) with fresh requests
and separate code: 2,544 claims; **1,952 correct, 0 wrong** on machine-checkable claims (identity, every financial value per
fiscal year, roles, workplaces, shares, jobs, websites, social links); 1 website needed manual review and was confirmed
correct (legal name, registered address and registry phone printed in the site footer). 591 descriptive claims (statutory
purpose, flags, registry-change events, PDF links) are copied verbatim from the same registry responses and not re-checked.
In addition, **every** website and social claim in the run (12 companies, 19 claims) was re-audited
(`--web-only-all`): 18 correct by script, 1 confirmed manually, **0 wrong**. The audit now re-fetches every company-site
evidence page, not only the homepage.
**Measured precision: 100 % (0 wrong-company publications).**

### Refresh — `reports/smoke-100/refresh-check.json`
Second run (`smoke-100-v2-r2`) against the first run's state: 100/100 companies matched to their previous snapshot,
**0 changes** (no false changes), 100/100 identical claim-id sets; the first snapshot is preserved. With the warm cache the
second run read 0 NAV feed entries (9,804 from cache): 1,759 requests, 250 s. Typed changes are covered by unit tests.

## Larger companies — `reports/dev-employers-40/`
40 random companies with ≥10 registered employees (seed 11), same batch as v1:

| Section | companies available | claims | states |
|---|---|---|---|
| identity | 40 | 821 | available 40 |
| financials | 40 | 2320 | available 40 |
| leadership | 40 | 263 | available 40 |
| workplaces | 40 | 156 | available 40 |
| web_presence | 25 | 169 | available 25, not_available 13, ambiguous 1, blocked 1 |
| hiring | 10 | 22 | available 10, not_available 30 |
| activity | 40 | 411 | available 40 |

| v1 → v2 (40 employers) | v1 | v2 |
|---|---|---|
| web presence available | 22 | **25** |
| verified official website | 21 | **24** |
| companies with company-owned social profiles (claims) | 17 (37) | **19 (38)** |
| hiring available (job ads / careers page) | 10 (1 / 10) | 10 (1 / 10) |

New in v2: molaanlegg.com (name + registered address), marthes.no (org number), fus.no/ringstabekk (org number, declared on
the company's sub-unit; the chain's social/careers links are not attributed). One FINN ad found by v1's name search had
expired by the v2 run; the kindergarten chain's careers pages are no longer attributed to the single kindergarten.

Audit of all 40 (fresh requests): 4,162 claims, **2,921 correct, 0 wrong**; 10 web/social claims needed manual review,
all correct (notes in the file).

## Budget behaviour
`--time-budget-seconds` (default 5400 s) is a soft deadline: the NAV feed-entry index, website and hiring steps stop early,
and every company still receives a terminal envelope; a hard stop 30 s before the budget emits `failed` envelopes for
anything unfinished. If the feed-entry index is cut off, hiring falls back to v1's name-matched candidates (still verified by org number).
Expected for a 1,000-company batch: ~20–35 min, ~25–30 k requests on a cold cache (~10 k of them the one-off NAV feed
entries; refresh runs reuse the cache), USD 0.
