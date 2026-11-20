# Sources, rights and outbound-URL policy

Every published claim cites the source below that produced it (URL, retrieval time, sha256 of the raw response,
extraction method and the exact span). Search engines are **not** used. No account-bound credentials are used.

| Source | Access | What we take | Key / match | Rights |
|---|---|---|---|---|
| Brønnøysund Enhetsregisteret API `data.brreg.no/enhetsregisteret/api/enheter/{org}` | open REST | identity, addresses, NACE, employees, founding, capital, purpose, declared website / e-mail / phone, status flags | exact org number | NLOD 2.0 |
| Enhetsregisteret bulk CSV (`--bulk`, frozen snapshot supplied by Builderr) | file | snapshot fallback; name-uniqueness and shared-domain counts used by the identity gates | exact org number | NLOD 2.0 |
| `/enheter/{org}/roller` | open REST | CEO, board, auditor, accountant, partners (names + role + last changed; birth dates are **not** published) | exact org number | NLOD 2.0 |
| `/underenheter?overordnetEnhet={org}` | open REST | registered workplaces (sub-units); their declared website / e-mail domain and trading names are used only as website candidates | exact parent org number | NLOD 2.0 |
| `/konsernstruktur/{org}` (only when `erIKonsern=true`) | open REST | parent, subsidiaries, group top | exact org number | NLOD 2.0 |
| `/oppdateringer/enheter?organisasjonsnummer=…&includeChanges=true` | open REST, batched | dated registry changes (field-level patches) | exact org number | NLOD 2.0 |
| Regnskapsregisteret `data.brreg.no/regnskapsregisteret/regnskap/{org}` | open REST | filed annual accounts (company + group), up to 3 fiscal years, audit flags; link to official PDF copy | exact org number | NLOD 2.0 |
| NAV job-vacancy feed `pam-stilling-feed.nav.no` (public token from `/api/publicToken`) | official API, consumed as documented (feed pages → one `feedentry` per active ad, 16 concurrent, cached by uuid + `sistEndret`) | active job ads indexed by `employer.orgnr`; an ad is published only if that org number equals the company or one of its registered sub-units; employer homepage as a website candidate | exact org number | NAV API terms (arbeidsplassen.nav.no/vilkar-api) |
| arbeidsplassen.nav.no search (`/stillinger/api/search`) | public JSON behind the NAV job site; ≤1 request / 2 s, only for companies with registered employees, auto-disabled on HTTP 429; `--no-nav-search` turns it off | additional ads (incl. FINN-sourced). Non-FINN hits are re-verified through the feed entry org number; FINN hits only when the employer name equals the legal name **and** that name is unique in the registry | org number or unique exact legal name | NAV public data |
| Wikidata SPARQL (batched) | open API | item, official website (P856), social IDs (P2013/P2003/P4264/P2002/P2397/P7085), ISIN | `P2333` (Norwegian org number) — items with two candidates are dropped | CC0 |
| GLEIF LEI API (batched) | open API | LEI + registration status | `entity.registeredAs` = org number | CC0 |
| Company website | public HTML, robots.txt respected, ≤6 pages per site, ≤2.5 MB per page | website, title/description/brand, generic contact mailbox, outbound social links, careers links, JSON-LD JobPosting/Article, RSS/Atom news | only after the site passes the identity gate (see `AGENT.md`) | company-owned; facts only, short spans |

Not used: LinkedIn, Meta, Glassdoor, Indeed, Google/Bing/Brave/DuckDuckGo scraping (their terms prohibit automated queries;
no paid search key is assumed), proff.no, 1881, Gule Sider, w2.brreg.no/kunngjoring (disallowed by robots.txt).
Norid (.no registry) RDAP/whois was evaluated for domain → holder org number and **not used**: the public RDAP response does
not expose the holder, entity search requires authorisation, and Norid's terms prohibit commercial use of the lookup data.
DNS resolution of candidate domains (`getaddrinfo`) is the only registry-side signal used. Social profiles are **linked**, never scraped.

## Outbound URL safety
* Only `http`/`https`, default ports, no credentials in URLs.
* Every hop (including redirects, max 5) is DNS-resolved and refused if it points to a private, loopback, link-local,
  CGNAT, multicast or reserved address (`signalpost/http.py: assert_public_url`).
* Per-host concurrency caps (brreg 6, NAV feed 16, NAV search 3, Wikidata 1, GLEIF 2, any company site 2), bounded body size, timeouts, retries with backoff.
* robots.txt is honoured for company sites (RFC 9309 semantics).

## Secrets
None required. Optional: `SIGNALPOST_LLM_API_KEY` (+ `SIGNALPOST_LLM_BASE_URL`, `SIGNALPOST_LLM_MODEL`) for an
evidence-bounded rewrite of the deterministic brief; read only from the environment, never logged or written to output.
