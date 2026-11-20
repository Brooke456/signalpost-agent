# Research and abstention policy

1. **Anchor**: every run starts from the organisation number. Identity, roles, workplaces, group links, registry changes
   and filed accounts are fetched from Brønnøysund endpoints keyed by that number. These facts are exact by construction.
2. **Candidates** for the public web identity, in order: registry-declared website → NAV employer homepage (from an ad whose
   employer org number matches) → Wikidata P856 (item keyed by P2333) → registry e-mail domain (free-mail providers excluded)
   → name-derived domains (`<name>.no/.com`, only if DNS resolves).
3. **Identity gate** (`signalpost/web.py::_decide`) — a website is published only when:
   * the organisation number is printed on the site (checked on the homepage plus up to 5 contact/about/privacy/careers pages) → `org_number_on_site`, or
   * it was declared by the company to an org-number-keyed register (Brreg `hjemmeside`, NAV employer record) **and** nothing
     contradicts it (no other org number without our name; not parked; if the domain is declared by several registered entities —
     franchise/brand/group sites — the site must name this company) → `registry_declared_domain`, or
   * it is the registry e-mail domain, the domain is not shared by >3 registered entities, and the site names the company → `registry_email_domain_corroborated`, or
   * a guessed domain equal to the company name shows both the legal name and the registry phone number and no other org number.
   Everything else is `ambiguous` (with the reason) or `not_available`.
4. **Company-owned profiles** are only taken from links on a gated site (or Wikidata keyed by org number). If a site links
   several profiles on one platform and exactly one matches the company/domain name, that one is used; otherwise all are
   reported as `ambiguous` candidates and not published.
5. **Jobs** are published only when the NAV ad's employer org number is the company or one of its registered sub-units
   (or, for FINN-sourced ads without an org number, when the employer name equals a registry-unique legal name).
6. **Activity** is dated: registry change events (Brreg update feed), company news from the gated site's RSS/JSON-LD (≤2 years).
7. **Synthesis** is deterministic and cites claim ids; it states unknowns explicitly. An LLM, if configured, may only rephrase,
   and its output is discarded if it contains any number not present in the deterministic brief.
8. Prefer abstention: a missed fact costs recall; a wrong-company fact disqualifies. When in doubt → `ambiguous`.
