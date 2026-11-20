# Refresh, snapshots and change detection

* `--state-dir state/` holds `runs/<run_id>/envelopes.jsonl` (immutable; a repeated run id writes `envelopes.attemptN.jsonl` beside it),
  `index.json` (org → run ids) and `raw/<sha256>.gz` (content-addressed raw HTTP bodies for every evidence hash).
* On each run the previous envelope of every company is loaded and diffed by stable claim slots:
  single-valued fields (legal name, address, website, …) by field; multi-valued facts (roles, workplaces, jobs, news) by claim id.
* Typed changes: `new_role`/`ended_role`, `new_location`/`closed_location`, `new_job`/`closed_job`, `new_filing`,
  `changed_description`, `changed_<field>`, `new_activity`; each with materiality (high/medium/low), previous + current value,
  previous evidence and current evidence ids.
* A source that **fails** this run does not erase knowledge: the last supported claims of that section are carried forward,
  flagged `carried_forward: true`, with their original evidence.
* Idempotent: re-running the same inputs against unchanged sources produces the same claim ids and zero changes
  (verified in `reports/smoke-100/refresh-check.json`). Volatile counters are excluded from change detection.
* `first_observed_at` is preserved across runs; `last_observed_at` is the current run.
