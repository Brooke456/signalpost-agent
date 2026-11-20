"""Batch runner: one terminal envelope per input organisation number, always."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from . import __version__
from .claims import SECTIONS, Collector
from .external import build_nav_index, gleif_batch, registry_updates_batch, wikidata_batch
from .http import METER, set_snapshot_dir, utc_now
from .refresh import Store, diff
from .registry import Bulk
from .synthesis import build as synthesize

SCHEMA = "signalpost-envelope/1.1"
MODULE_STATE = {"available": "complete", "not_available": "not_found", "not_applicable": "not_applicable",
                "blocked": "blocked_policy", "ambiguous": "complete", "failed": "source_error"}


def read_inputs(path: str) -> list[str]:
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    vals: list[Any]
    if p.suffix == ".json":
        body = json.loads(text)
        vals = body if isinstance(body, list) else body.get("organisation_numbers") or body.get("organisations") or []
    elif p.suffix == ".jsonl":
        vals = [json.loads(l) for l in text.splitlines() if l.strip()]
    else:
        vals = [l.strip() for l in text.replace(",", "\n").splitlines() if l.strip()]
    out: list[str] = []
    for v in vals:
        o = v.get("organisation_number") or v.get("orgnr") or v.get("org") if isinstance(v, dict) else v
        o = "".join(ch for ch in str(o or "") if ch.isdigit())
        if len(o) != 9:
            raise SystemExit(f"Invalid organisation number in input: {v!r}")
        if o not in out:
            out.append(o)
    return out


def envelope(org: str, res: dict[str, Any] | None, *, run_id: str, started: str, err: str | None, store: Store, now: str) -> dict[str, Any]:
    if res is None:
        col = Collector(org)
        for s in SECTIONS:
            col.section(s, "failed", reason=err or "not processed")
        col.error("pipeline", err or "not processed")
        res = {"collector": col, "ident": {"organisation_number": org}, "extra": {}, "runtime_ms": 0}
    col: Collector = res["collector"]
    for s in SECTIONS:
        if s not in col.section_state:
            col.section(s, "not_available", reason="not established in this run")
    prev = store.previous(org, run_id)
    claims = col.claims
    changes, carried = diff(prev, claims, col.section_state, now)
    prev_claims = {c["claim_id"]: c for c in (prev or {}).get("claims", [])}
    for c in claims:
        p = prev_claims.get(c["claim_id"])
        c["first_observed_at"] = (p or {}).get("first_observed_at") or now
        c["last_observed_at"] = now
    evidence = list(col.evidence.values())
    if carried:
        prev_ev = {e["id"]: e for e in prev.get("evidence", [])}
        for c in carried:
            claims.append(c)
            for e in c["evidence_ids"]:
                if e in prev_ev and e not in col.evidence:
                    evidence.append(prev_ev[e])
    ident = res["ident"]
    sections = {k: {**col.section_state[k], "title": SECTIONS[k], "claims": sum(1 for c in claims if c["section"] == k and c.get("availability") == "available")}
                for k in SECTIONS}
    summary = synthesize(claims, {k: v for k, v in sections.items()}, changes, ident)
    available_sections = [k for k, v in sections.items() if v["availability"] == "available"]
    overall = "available" if available_sections else ("failed" if all(v["availability"] == "failed" for v in sections.values()) else "not_available")
    legal = {
        "organisation_number": org,
        "legal_name": ident.get("name"),
        "legal_form": ident.get("legal_form"),
        "public_brand": next((c["value"] for c in claims if c["field"] == "public_brand_name"), None),
        "municipality": ident.get("municipality"),
        "official_website": next((c["value"] for c in claims if c["field"] == "official_website" and c.get("availability") == "available"), None),
    }
    return {
        "schema_version": SCHEMA,
        "organisation_number": org,
        "run_id": run_id,
        "state": "complete" if overall != "failed" else "source_error",
        "availability": overall,
        "started_at": started,
        "completed_at": now,
        "run": {"run_id": run_id, "started_at": started, "completed_at": now, "terminal_status": "completed" if overall != "failed" else "failed",
                "agent_version": __version__, "previous_run_id": (prev or {}).get("run_id")},
        "legal_identity": legal,
        "sections": sections,
        "modules": {k: {"state": MODULE_STATE[v["availability"]], "availability": v["availability"], "final_timestamp": now} for k, v in sections.items()},
        "summary": summary,
        "claims": claims,
        "evidence": evidence,
        "changes": changes,
        "refresh": {"previous_run_id": (prev or {}).get("run_id"), "previous_completed_at": (prev or {}).get("completed_at"),
                    "material_changes": sum(1 for c in changes if c["materiality"] in ("high", "medium")),
                    "carried_forward_claims": len(carried), "is_first_observation": prev is None},
        "errors": col.errors,
        "operations": {"runtime_ms": res.get("runtime_ms", 0), "third_party_cost_usd": 0},
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Signalpost company-research agent (batch).")
    ap.add_argument("--organisations", "--input", dest="organisations", required=True, help="JSON / JSONL / text list of organisation numbers")
    ap.add_argument("--bulk", default=os.environ.get("SIGNALPOST_BULK"), help="Frozen Brreg entity snapshot (CSV/CSV.gz or Signalpost universe JSONL.gz). Optional.")
    ap.add_argument("--output", required=True, help="Terminal envelope JSONL (one line per input)")
    ap.add_argument("--profiles-output", help="Compact profile JSONL for the viewer")
    ap.add_argument("--report", help="Machine-readable run report JSON")
    ap.add_argument("--site", help="Write a static HTML viewer to this path")
    ap.add_argument("--run-id", default=None)
    ap.add_argument("--expected-count", type=int, default=None)
    ap.add_argument("--state-dir", default=os.environ.get("SIGNALPOST_STATE_DIR", "state"))
    ap.add_argument("--workers", type=int, default=int(os.environ.get("SIGNALPOST_WORKERS", "24")))
    ap.add_argument("--time-budget-seconds", type=float, default=float(os.environ.get("SIGNALPOST_TIME_BUDGET_S", "5400")))
    ap.add_argument("--no-guess-domains", action="store_true")
    ap.add_argument("--no-jobs", action="store_true")
    ap.add_argument("--no-nav-search", action="store_true", help="Use only the official NAV feed (skip arbeidsplassen search)")
    ap.add_argument("--no-raw-snapshots", action="store_true")
    a = ap.parse_args(argv)

    t_start = time.monotonic()
    started = utc_now()
    run_id = a.run_id or ("run-" + started.replace(":", "").replace("-", ""))
    orgs = read_inputs(a.organisations)
    if a.expected_count is not None and len(orgs) != a.expected_count:
        print(f"WARNING: expected {a.expected_count} organisations, received {len(orgs)}; processing all received", file=sys.stderr)
    state = Path(a.state_dir)
    store = Store(state)
    if not a.no_raw_snapshots:
        set_snapshot_dir(state / "raw")
    # reserve 8% of the budget (min 60 s) for envelope assembly and writing
    hard_deadline = t_start + a.time_budget_seconds
    deadline = hard_deadline - max(60.0, a.time_budget_seconds * 0.08)

    feed_pool = ThreadPoolExecutor(max_workers=1)
    pre: dict[str, Any] = {}
    if not a.no_jobs:
        pre["nav_feed_future"] = feed_pool.submit(build_nav_index, None, 120, deadline)
    bulk = Bulk(a.bulk, orgs) if a.bulk else Bulk(None, [])
    pre["bulk_loaded_at"] = utc_now()
    since = (date.today() - timedelta(days=420)).isoformat() + "T00:00:00.000Z"
    with ThreadPoolExecutor(max_workers=3) as px:
        f_wd = px.submit(wikidata_batch, orgs)
        f_gl = px.submit(gleif_batch, orgs)
        f_up = px.submit(registry_updates_batch, orgs, since)
        for key, f in (("wikidata", f_wd), ("gleif", f_gl), ("updates", f_up)):
            try:
                pre[key] = f.result(timeout=max(30, deadline - time.monotonic()))
            except Exception as exc:  # noqa: BLE001
                pre[key] = {}
                print(f"prefetch {key} failed: {exc}", file=sys.stderr)

    from .company import research_company
    options = {"guess_domains": not a.no_guess_domains, "jobs": not a.no_jobs, "nav_search": not a.no_nav_search}
    results: dict[str, dict[str, Any]] = {}
    errors: dict[str, str] = {}
    pool = ThreadPoolExecutor(max_workers=a.workers)
    futs = {pool.submit(research_company, o, bulk=bulk, prefetched=pre, deadline=deadline, options=options): o for o in orgs}
    done = 0
    unfinished = False
    try:
        for f in as_completed(futs, timeout=max(1.0, hard_deadline - 30 - time.monotonic())):
            o = futs[f]
            try:
                results[o] = f.result()
            except Exception as exc:  # noqa: BLE001
                errors[o] = f"{type(exc).__name__}: {exc}"
                traceback.print_exc()
            done += 1
            if done % 25 == 0:
                print(f"[{done}/{len(orgs)}] {int(time.monotonic() - t_start)}s requests={METER.requests}", file=sys.stderr, flush=True)
    except Exception:  # noqa: BLE001  (timeout)
        unfinished = True
        print("time budget reached; emitting failed envelopes for unfinished companies", file=sys.stderr)
    # phase 2: hiring for companies that finished before the NAV feed index was ready
    pending = [o for o, r in results.items() if r.get("needs_jobs")]
    if pending and not unfinished:
        from .company import finish_hiring, run_jobs
        feed = None
        try:
            feed = pre["nav_feed_future"].result(timeout=max(1.0, deadline - time.monotonic()))
        except Exception:  # noqa: BLE001
            feed = None

        def _phase2(o: str) -> None:
            r = results[o]
            ex = r["extra"]
            run_jobs(r["collector"], r["ident"], bulk, ex["subunits"], ex["unique"], feed, ex["site"], deadline, options)

        futs2 = [pool.submit(_phase2, o) for o in pending]
        try:
            for f in as_completed(futs2, timeout=max(1.0, hard_deadline - 30 - time.monotonic())):
                try:
                    f.result()
                except Exception:  # noqa: BLE001
                    traceback.print_exc()
        except Exception:  # noqa: BLE001
            unfinished = True
        for o in pending:
            finish_hiring(results[o]["collector"])
    pool.shutdown(wait=False, cancel_futures=True)

    now = utc_now()
    envs = []
    for o in orgs:
        envs.append(envelope(o, results.get(o), run_id=run_id, started=started, now=now, store=store,
                             err=errors.get(o) or (None if o in results else "time budget exhausted before completion")))
    out = Path(a.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        for e in envs:
            fh.write(json.dumps(e, ensure_ascii=False, separators=(",", ":")) + "\n")
    tmp.replace(out)
    snap = store.save_run(run_id, envs)
    if a.profiles_output:
        from .viewer import compact_profile
        pp = Path(a.profiles_output)
        pp.parent.mkdir(parents=True, exist_ok=True)
        with pp.open("w", encoding="utf-8") as fh:
            for e in envs:
                fh.write(json.dumps(compact_profile(e), ensure_ascii=False, separators=(",", ":")) + "\n")
    if a.site:
        from .viewer import write_site
        write_site(envs, Path(a.site))
    report = build_report(envs, orgs, run_id, started, now, t_start, bulk, snap, a)
    nf = pre.get("nav_feed_future")
    if nf is not None and nf.done():
        try:
            idx = nf.result()
            report["nav_feed"] = {"pages": idx.pages, "active_ads_indexed": idx.active, "error": idx.error}
        except Exception as exc:  # noqa: BLE001
            report["nav_feed"] = {"error": str(exc)}
    feed_pool.shutdown(wait=False, cancel_futures=True)
    if a.report:
        Path(a.report).parent.mkdir(parents=True, exist_ok=True)
        Path(a.report).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("run_id", "expected_count", "emitted_envelopes", "validation", "coverage")}, indent=2, ensure_ascii=False))
    code = 0 if report["validation"]["passed"] else 1
    if unfinished:
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(code)
    return code


def build_report(envs, orgs, run_id, started, now, t_start, bulk, snap, a) -> dict[str, Any]:
    n = len(envs)
    cov: dict[str, Any] = {}
    for s in SECTIONS:
        states: dict[str, int] = {}
        for e in envs:
            st = e["sections"][s]["availability"]
            states[st] = states.get(st, 0) + 1
        cov[s] = {"available_companies": states.get("available", 0), "states": states,
                  "claims": sum(e["sections"][s]["claims"] for e in envs)}
    fields: dict[str, dict[str, int]] = {}
    for e in envs:
        seen = set()
        for c in e["claims"]:
            if c.get("availability") != "available":
                continue
            f = fields.setdefault(c["field"], {"claims": 0, "companies": 0})
            f["claims"] += 1
            if c["field"] not in seen:
                f["companies"] += 1
                seen.add(c["field"])
    lat = sorted(e["operations"]["runtime_ms"] for e in envs)
    req_lat = sorted(METER.latencies)
    ids = [e["organisation_number"] for e in envs]
    unsupported = sum(1 for e in envs for c in e["claims"] if not c.get("evidence_ids"))
    ev_ok = all(all(x in {ev["id"] for ev in e["evidence"]} for c in e["claims"] for x in c["evidence_ids"]) for e in envs)
    checks = {
        "exact_expected_count": n == (a.expected_count if a.expected_count is not None else len(orgs)),
        "one_envelope_per_input": ids == orgs,
        "unique_organisation_numbers": len(set(ids)) == len(ids),
        "all_states_terminal": all(e["availability"] in ("available", "not_available", "blocked", "not_applicable", "ambiguous", "failed") for e in envs),
        "every_claim_has_evidence": unsupported == 0,
        "evidence_references_resolve": ev_ok,
    }
    return {
        "run_id": run_id, "agent_version": __import__("signalpost").__version__, "started_at": started, "completed_at": now,
        "wall_clock_s": round(time.monotonic() - t_start, 1),
        "expected_count": a.expected_count if a.expected_count is not None else len(orgs), "emitted_envelopes": n,
        "terminal_availability": {k: sum(1 for e in envs if e["availability"] == k) for k in ("available", "not_available", "failed")},
        "coverage": cov, "fields": dict(sorted(fields.items())),
        "operations": {"requests": METER.requests, "bytes": METER.bytes, "requests_by_host_top": dict(sorted(METER.by_host.items(), key=lambda kv: -kv[1])[:15]),
                       "request_p50_ms": req_lat[len(req_lat) // 2] if req_lat else None,
                       "request_p95_ms": req_lat[int(len(req_lat) * 0.95)] if req_lat else None,
                       "company_p50_ms": lat[len(lat) // 2] if lat else None, "company_p95_ms": lat[min(len(lat) - 1, int(len(lat) * 0.95))] if lat else None,
                       "third_party_cost_usd": 0.0, "llm_used": bool(os.environ.get("SIGNALPOST_LLM_API_KEY"))},
        "registry_snapshot": bulk.metadata(),
        "refresh": {"snapshot": snap["snapshot"], "companies_with_previous": sum(1 for e in envs if e["refresh"]["previous_run_id"]),
                    "changes": sum(len(e["changes"]) for e in envs), "material_changes": sum(e["refresh"]["material_changes"] for e in envs)},
        "validation": {"passed": all(checks.values()), "checks": checks, "claims_without_evidence": unsupported},
    }


if __name__ == "__main__":
    raise SystemExit(main())
