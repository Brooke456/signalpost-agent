# Known limitations

* No search engine: websites are only discovered from org-number-keyed records (registry `hjemmeside`/`epostadresse`, NAV employer
  record, Wikidata P2333) or from the company name, and are published only after the identity gate. Companies whose site is not
  discoverable this way are reported `not_available` (absence of evidence, not evidence of absence).
* JavaScript-only sites: static HTML only; org numbers rendered client-side are missed (the site may still be accepted through a registry declaration).
* NAV's open feed excludes FINN-sourced ads; those are only found through the arbeidsplassen search with the strict unique-legal-name rule.
* Brreg's update feed exposes field-level changes only from September 2025 onward.
* Registry e-mail domains used by many entities (accountants, employers, groups) are never accepted without the org number on the site.
* No social-platform scraping, so follower counts and posts are out of scope by design.
