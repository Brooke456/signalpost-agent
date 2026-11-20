# Known limitations

* No search engine: websites are only discovered from org-number-keyed records (registry `hjemmeside`/`epostadresse`, NAV employer
  record, Wikidata P2333, sub-unit declarations) or from domains derived from the company's registered names, and are published
  only after the identity gate. Companies whose site is not
  discoverable this way are reported `not_available` (absence of evidence, not evidence of absence).
* JavaScript-only sites: static HTML only; org numbers rendered client-side are missed (the site may still be accepted through a registry declaration).
* NAV's open feed excludes FINN-sourced ads; those are only found through the arbeidsplassen search with the strict unique-legal-name rule.
* Brreg's update feed exposes field-level changes only from September 2025 onward.
* Registry e-mail domains used by many entities (accountants, employers, groups) are never accepted without the org number on the site.
* No social-platform scraping, so follower counts and posts are out of scope by design.
* Group or chain sites that never print the subsidiary's own org number, legal name + phone/address are not attributed
  (e.g. a dealer group site for one of its legal entities); these stay `not_available`/`ambiguous` by design.
* Reading every active NAV feed entry costs ~10k requests and ~5 min per run on a cold cache (≈1 min warm); it runs in the
  background, is cut off by the time budget, and falls back to name-matched candidates if incomplete.
