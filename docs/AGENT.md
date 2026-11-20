# Research and abstention policy

1. **Anchor**: every run starts from the organisation number. Identity, roles, workplaces, group links, registry changes
   and filed accounts are fetched from Brønnøysund endpoints keyed by that number. These facts are exact by construction.
2. **Candidates** for the public web identity, in order: registry-declared website → NAV employer homepage (from an ad whose
   employer org number matches) → Wikidata P856 (item keyed by P2333) → registry e-mail domain (free-mail providers excluded)
   → website / e-mail domain the company declared for its own registered sub-units → name-derived domains (v2: built from the
   legal name, distinct sub-unit trading names and the two latest earlier legal names; both Norwegian transliterations
   ø→o/oe, å→a/aa; full name, name without generic words, first two words, compound stem such as `altaskifer` for
   "Alta Skiferprodukter", distinctive first word; `.no` then `.com`; up to 12 labels, only DNS-resolving domains are fetched).
   Guessed domains are hypotheses only: they never pass on name similarity.
3. **Identity gate** (`signalpost/web.py::_decide`) — a website is published only when:
   * the organisation number is printed on the site (checked on the homepage plus up to 5 contact/about/privacy/careers pages) → `org_number_on_site`, or
   * it was declared by the company to an org-number-keyed register (Brreg `hjemmeside`, NAV employer record) **and** nothing
     contradicts it (no other org number without our name; not parked; if the domain is declared by several registered entities —
     franchise/brand/group sites — the site must name this company) → `registry_declared_domain`, or
   * it is the registry e-mail domain, the domain is not shared by >3 registered entities, and the site names the company → `registry_email_domain_corroborated`, or
   * a guessed domain shows the legal name **and** the registry phone number, business street address + postcode, or the
     registry e-mail address on that same domain, and no other organisation number.
   Extra guards (v2): a guessed page that lists ≥3 other organisation numbers and does not name the company (directory,
   group or client listing) is rejected; when the org number is only found on a sub-page of a domain that does not carry
   the company's name (a store page on a chain/franchise site), only that page is published as the website and the
   domain's own social profiles, news, careers and description are **not** attributed to the company. The same applies to
   social links found on a company section of a shared site (e.g. one kindergarten on a chain domain).
   Everything else is `ambiguous` (with the reason) or `not_available`.
4. **Company-owned profiles** are only taken from links on a gated site (or Wikidata keyed by org number). If a site links
   several profiles on one platform and exactly one matches the company/domain name, that one is used; otherwise all are
   reported as `ambiguous` candidates and not published.
5. **Jobs** are published only when the NAV ad's employer org number is the company or one of its registered sub-units
   (or, for FINN-sourced ads without an org number, when the employer name equals a registry-unique legal name).
   v2 reads the feed entry of **every** active ad once per run (cached on disk by uuid + modification time, so refresh runs
   only re-read changed ads) and indexes ads by employer org number, so ads published under a brand or workplace name that
   differs from the legal name are matched exactly through the main unit or any sub-unit org number.
6. **Activity** is dated: registry change events (Brreg update feed), company news from the gated site's RSS/JSON-LD (≤2 years).
7. **Synthesis** is deterministic and cites claim ids; it states unknowns explicitly. An LLM, if configured, may only rephrase,
   and its output is discarded if it contains any number not present in the deterministic brief.
8. Prefer abstention: a missed fact costs recall; a wrong-company fact disqualifies. When in doubt → `ambiguous`.
