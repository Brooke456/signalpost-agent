"""Static, dependency-free profile viewer (search, profile, compare two, verify every source). Desktop + mobile."""
from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any

FIELD_LABELS = {
    "organisation_number": "Organisation number", "legal_name": "Legal name", "legal_form": "Legal form", "public_brand_name": "Public brand",
    "registered_business_address": "Business address", "postal_address": "Postal address", "municipality": "Municipality",
    "industry_nace": "Primary industry (NACE)", "secondary_industry_nace": "Other industry (NACE)", "registered_employees": "Registered employees",
    "founded_date": "Founded", "registered_date": "Registered", "registry_status": "Registry status", "share_capital": "Share capital",
    "statutory_purpose": "Statutory purpose", "registered_activity": "Registered activity", "vat_registered": "VAT registered",
    "in_business_register": "In Foretaksregisteret", "part_of_group": "Part of a group", "former_name": "Former name",
    "registry_declared_website": "Website (declared to registry)", "registry_phone": "Phone (registry)", "registry_email": "E-mail (registry)",
    "registry_profile_url": "Brreg profile", "parent_company": "Parent company", "subsidiary": "Subsidiary", "group_ultimate_parent": "Group top",
    "lei": "LEI", "wikidata_item": "Wikidata", "isin": "ISIN",
    "revenue": "Revenue", "sales_revenue": "Sales revenue", "operating_expenses": "Operating expenses", "payroll_expenses": "Payroll expenses",
    "operating_result": "Operating result", "net_financial_items": "Net financial items", "profit_before_tax": "Profit before tax",
    "tax_expense": "Tax", "annual_result": "Net result", "total_assets": "Total assets", "fixed_assets": "Fixed assets",
    "current_assets": "Current assets", "cash_and_bank": "Cash and bank", "equity": "Equity", "total_debt": "Total debt",
    "short_term_debt": "Short-term debt", "long_term_debt": "Long-term debt", "accounts_audited": "Audited",
    "annual_accounts_copy": "Filed accounts (PDF)", "fiscal_years_available": "Fiscal years available",
    "ceo": "CEO / daily manager", "board_chair": "Board chair", "board_deputy_chair": "Deputy chair", "board_member": "Board member",
    "board_deputy_member": "Deputy board member", "auditor": "Auditor", "accountant": "Accountant", "proprietor": "Proprietor",
    "general_partner": "Partner", "contact_person": "Contact person", "registered_role": "Role", "board_observer": "Observer",
    "registered_workplace": "Registered workplace", "official_website": "Official website", "website_domain": "Domain",
    "website_title": "Website title", "website_description": "Self-description", "website_language": "Website language",
    "website_contact_email": "Contact e-mail (site)", "social_profile": "Company profile", "social_profile_candidates": "Unattributed profiles",
    "job_posting": "Job posting", "active_job_postings_count": "Active postings", "careers_page": "Careers page",
    "company_news": "Website news", "registry_change": "Registry change",
}
SECTION_ORDER = ["identity", "financials", "leadership", "workplaces", "web_presence", "hiring", "activity"]
SECTION_TITLES = {"identity": "Identity & brand", "financials": "Annual accounts", "leadership": "Leadership", "workplaces": "Workplaces",
                  "web_presence": "Website & profiles", "hiring": "Hiring", "activity": "Dated activity"}


def _money(v: dict[str, Any]) -> str:
    a = v.get("amount")
    if a is None:
        return "—"
    return f"{a:,.0f} {v.get('currency') or ''}".replace(",", " ")


def display(c: dict[str, Any]) -> str:
    v = c["value"]
    f = c["field"]
    if isinstance(v, dict):
        if "amount" in v and "currency" in v:
            return _money(v)
        if f in ("legal_form", "industry_nace", "secondary_industry_nace"):
            return f"{v.get('code')} — {v.get('label')}"
        if "person_name" in v or "entity_name" in v:
            nm = v.get("person_name") or v.get("entity_name")
            extra = f" ({v.get('entity_organisation_number')})" if v.get("entity_organisation_number") else ""
            return f"{nm}{extra} · {v.get('role')}"
        if f == "registered_workplace":
            e = f" · {v['registered_employees']} employees" if v.get("registered_employees") is not None else ""
            return f"{v.get('name')} — {v.get('address') or ''}{e}"
        if f == "job_posting":
            loc = ", ".join(v.get("locations") or [])
            return f"{v.get('title')} ({v.get('published') or '?'}{' → ' + v['expires'] if v.get('expires') else ''}){' · ' + loc if loc else ''}"
        if f == "social_profile":
            return f"{v.get('platform')}: {v.get('url')}"
        if f == "company_news":
            return f"{v.get('date')} — {v.get('title')}"
        if f == "registry_change":
            return f"{v.get('date')} — {v.get('type')}: {', '.join(v.get('fields') or [])}"
        if f in ("parent_company", "subsidiary", "group_ultimate_parent"):
            return f"{v.get('name')} ({v.get('organisation_number')}){' · ' + v['relation'] if v.get('relation') else ''}{' ' + v['share'] if v.get('share') else ''}"
        if f == "share_capital":
            return f"{v.get('amount'):,.2f} {v.get('currency')} ({v.get('shares')} shares)".replace(",", " ")
        if f == "lei":
            return f"{v.get('lei')} ({v.get('registration_status')})"
        if f == "annual_accounts_copy":
            return f"{v.get('year')}: {v.get('url')}"
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, list):
        return ", ".join(str(x) if not isinstance(x, dict) else (x.get("url") or json.dumps(x)) for x in v)
    if isinstance(v, bool):
        return "yes" if v else "no"
    return str(v)


def _link(c: dict[str, Any]) -> str | None:
    v = c["value"]
    if isinstance(v, dict):
        return v.get("url")
    if isinstance(v, str) and v.startswith("http"):
        return v
    return None


def compact_profile(e: dict[str, Any]) -> dict[str, Any]:
    ev = {x["id"]: x for x in e["evidence"]}
    claims = []
    for c in e["claims"]:
        claims.append({
            "id": c["claim_id"], "s": c["section"], "f": c["field"], "l": FIELD_LABELS.get(c["field"], c["field"].replace("_", " ")),
            "v": display(c), "u": _link(c), "a": c.get("availability"), "cf": c.get("confidence"), "m": c.get("entity_match"),
            "p": (c.get("reporting_period") or {}).get("fiscal_year"), "pt": (c.get("reporting_period") or {}).get("account_type"),
            "d": c.get("effective_date"), "n": c.get("note"), "cfw": c.get("carried_forward", False),
            "raw": c["value"] if c["field"] in ("revenue", "annual_result", "operating_result", "equity", "total_assets", "registered_employees") else None,
            "ev": [{"u": ev[i]["source_url"], "c": ev[i]["source_class"], "t": ev[i]["retrieved_at"], "s": ev[i]["claim_span"],
                    "h": (ev[i].get("content_sha256") or "")[:16], "x": ev[i].get("extraction_method")} for i in c["evidence_ids"] if i in ev],
        })
    li = e["legal_identity"]
    return {"org": e["organisation_number"], "name": li.get("legal_name"), "brand": li.get("public_brand"), "form": li.get("legal_form"),
            "muni": li.get("municipality"), "web": li.get("official_website"), "av": e["availability"],
            "sec": {k: {"a": v["availability"], "r": v.get("reason"), "n": v.get("claims", 0), "src": v.get("sources_checked", [])} for k, v in e["sections"].items()},
            "sum": e["summary"], "claims": claims, "changes": e["changes"], "run": e["run"], "errors": e["errors"]}


def write_site(envs: list[dict[str, Any]], path: Path) -> None:
    data = [compact_profile(e) for e in envs]
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(TEMPLATE.replace("__DATA__", payload).replace("__RUN__", html.escape(envs[0]["run_id"] if envs else "")), encoding="utf-8")


TEMPLATE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Signalpost — verified Norwegian company profiles</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--ink:#14171c;--mute:#5d6673;--line:#e3e6eb;--acc:#1d5bd8;--ok:#13804a;--warn:#b26a00;--bad:#b42318;--amb:#7a4fd6}
*{box-sizing:border-box}body{margin:0;font:15px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--ink)}
header{position:sticky;top:0;z-index:5;background:#0f1724;color:#fff;padding:10px 16px;display:flex;gap:12px;align-items:center;flex-wrap:wrap}
header b{font-size:17px}header small{color:#aab4c3}
#q{flex:1;min-width:180px;padding:9px 12px;border-radius:8px;border:0;font-size:15px}
.wrap{display:grid;grid-template-columns:340px 1fr;height:calc(100vh - 56px)}
#list{overflow:auto;border-right:1px solid var(--line);background:var(--card)}
.item{padding:10px 14px;border-bottom:1px solid var(--line);cursor:pointer}.item:hover,.item.sel{background:#eef3ff}
.item .n{font-weight:600}.item .m{color:var(--mute);font-size:12.5px}
.dots{display:flex;gap:3px;margin-top:4px}.dot{width:9px;height:9px;border-radius:50%;background:#d5d9e0}.dot.available{background:var(--ok)}.dot.ambiguous{background:var(--amb)}.dot.failed,.dot.blocked{background:var(--bad)}
#main{overflow:auto;padding:18px 22px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px;margin-bottom:14px}
h1{font-size:22px;margin:0 0 4px}h2{font-size:16px;margin:0 0 8px;display:flex;justify-content:space-between;align-items:center}
.sub{color:var(--mute)}.badge{font-size:11.5px;padding:2px 8px;border-radius:99px;border:1px solid currentColor;white-space:nowrap}
.b-available{color:var(--ok)}.b-not_available{color:var(--mute)}.b-ambiguous{color:var(--amb)}.b-failed,.b-blocked{color:var(--bad)}.b-not_applicable{color:var(--mute)}
table{width:100%;border-collapse:collapse}td{padding:6px 4px;border-top:1px solid var(--line);vertical-align:top}td.k{color:var(--mute);width:34%}
.src{font-size:12px;color:var(--acc);cursor:pointer;white-space:nowrap}.ev{display:none;font-size:12px;background:#f3f5f8;border-radius:8px;padding:8px;margin-top:6px;word-break:break-word}
.ev.open{display:block}.ev a{color:var(--acc)}code{font-size:11.5px}
.flag{padding:6px 10px;border-radius:8px;margin:4px 0;font-size:13.5px}.flag.high{background:#fdecea}.flag.medium{background:#fff4e0}.flag.info{background:#eef3ff}
.sum p{margin:6px 0}.cite{font-size:11px;color:var(--mute)}
.toolbar{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px}button,select{font:inherit;padding:6px 10px;border-radius:8px;border:1px solid var(--line);background:#fff;cursor:pointer}
button.on{background:var(--acc);color:#fff;border-color:var(--acc)}
.cmp td{width:auto}.cmp th{text-align:left;padding:6px 4px;font-size:13px}
.back{display:none}
@media (max-width:800px){.wrap{grid-template-columns:1fr;height:auto}#list{max-height:none}#main{padding:12px}
 body.detail #list{display:none}body:not(.detail) #main{display:none}.back{display:inline-block}td.k{width:40%}}
</style></head><body>
<header><b>Signalpost</b><small>run __RUN__ · every fact links to its source</small>
<input id="q" placeholder="Search name, org.nr, municipality, industry…" autocomplete="off">
<select id="flt"><option value="">All companies</option><option value="web_presence">With verified website</option><option value="hiring">Hiring signals</option><option value="activity">Dated activity</option><option value="financials">Accounts available</option></select>
<button id="cmpBtn" title="Compare two companies">Compare</button></header>
<div class="wrap"><div id="list"></div><div id="main"><div class="card sub">Select a company. Use <b>Compare</b> to pick two companies side by side.</div></div></div>
<script>
const DATA=__DATA__;const SEC=["identity","financials","leadership","workplaces","web_presence","hiring","activity"];
const ST={identity:"Identity & brand",financials:"Annual accounts",leadership:"Leadership",workplaces:"Workplaces",web_presence:"Website & profiles",hiring:"Hiring",activity:"Dated activity"};
let cmp=false,picked=[];const $=s=>document.querySelector(s);
const esc=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
function score(p){return SEC.reduce((a,s)=>a+(p.sec[s].a==="available"?1:0),0)}
DATA.sort((a,b)=>score(b)-score(a)||String(a.name).localeCompare(b.name));
function hay(p){return (p.name+" "+p.org+" "+(p.brand||"")+" "+(p.muni||"")+" "+(p.web||"")+" "+p.claims.filter(c=>c.f==="industry_nace").map(c=>c.v).join(" ")).toLowerCase()}
function renderList(){const q=$("#q").value.trim().toLowerCase(),f=$("#flt").value;
 const rows=DATA.filter(p=>(!q||hay(p).includes(q))&&(!f||p.sec[f].a==="available"));
 $("#list").innerHTML=rows.map(p=>`<div class="item ${picked.includes(p.org)?'sel':''}" data-o="${p.org}"><div class="n">${esc(p.brand&&p.brand.toLowerCase()!==String(p.name).toLowerCase()?p.brand+' · ':'')}${esc(p.name)}</div><div class="m">${p.org} · ${esc(p.form||"")} · ${esc(p.muni||"")}</div><div class="dots">${SEC.map(s=>`<span class="dot ${p.sec[s].a}" title="${ST[s]}: ${p.sec[s].a}"></span>`).join("")}</div></div>`).join("")||'<div class="item sub">No match</div>';
 document.querySelectorAll(".item[data-o]").forEach(el=>el.onclick=()=>pick(el.dataset.o));}
function pick(o){if(cmp){picked=picked.includes(o)?picked.filter(x=>x!==o):[...picked,o].slice(-2);renderList();if(picked.length===2)renderCompare();return}
 location.hash=o;}
function evHtml(c){return c.ev.map(e=>`<div><a href="${esc(e.u)}" target="_blank" rel="noopener nofollow">${esc(e.u)}</a><br>${esc(e.c)} · retrieved ${esc(e.t)} · sha256 <code>${esc(e.h)}…</code> · ${esc(e.x||"")}<br>“${esc(e.s)}”</div>`).join("<hr>")}
function row(c){const v=c.u?`<a href="${esc(c.u)}" target="_blank" rel="noopener nofollow">${esc(c.v)}</a>`:esc(c.v);
 const meta=[c.p?`FY ${c.p}${c.pt&&c.pt!=="SELSKAP"?" ("+c.pt.toLowerCase()+")":""}`:"",c.d&&!c.p?`as of ${c.d}`:"",c.a!=="available"?c.a:"",c.cfw?"carried forward":""].filter(Boolean).join(" · ");
 return `<tr><td class="k">${esc(c.l)}</td><td>${v}${meta?` <span class="cite">${esc(meta)}</span>`:""}${c.n?`<div class="cite">${esc(c.n)}</div>`:""}<div><span class="src" onclick="this.parentNode.nextElementSibling.classList.toggle('open')">source ▸ ${esc(c.ev[0]?c.ev[0].c:"")} · match: ${esc(c.m||"")} · conf ${c.cf}</span></div><div class="ev">${evHtml(c)}</div></td></tr>`}
function finTable(p){const fs=["revenue","operating_result","annual_result","equity","total_assets","total_debt","cash_and_bank","payroll_expenses"];
 const cs=p.claims.filter(c=>c.s==="financials"&&fs.includes(c.f));const ys=[...new Set(cs.map(c=>c.p))].sort();if(!ys.length)return"";
 let h=`<table><tr><td class="k">NOK (company accounts)</td>${ys.map(y=>`<td><b>${y}</b></td>`).join("")}</tr>`;
 for(const f of fs){const r=cs.filter(c=>c.f===f);if(!r.length)continue;h+=`<tr><td class="k">${esc(r[0].l)}</td>${ys.map(y=>{const c=r.find(x=>x.p===y);return `<td>${c?`<span title="${esc(c.ev[0]?c.ev[0].s:"")}">${esc(c.v)}</span>`:"—"}</td>`}).join("")}</tr>`}
 return h+"</table>"}
function renderProfile(o){const p=DATA.find(x=>x.org===o);if(!p)return;document.body.classList.add("detail");
 const s=p.sum;let h=`<button class="back" onclick="history.back()">← Back</button><div class="card"><h1>${esc(p.brand&&p.brand.toLowerCase()!==String(p.name).toLowerCase()?p.brand:p.name)}</h1><div class="sub">${esc(p.name)} · org.nr ${p.org} · ${esc(p.form||"")} · ${esc(p.muni||"")}${p.web?` · <a href="${esc(p.web)}" target="_blank" rel="noopener nofollow">${esc(p.web)}</a>`:""}</div>
 <div class="dots" style="margin-top:8px">${SEC.map(x=>`<span class="badge b-${p.sec[x].a}">${ST[x]}: ${p.sec[x].a.replace("_"," ")}</span>`).join(" ")}</div></div>`;
 h+=`<div class="card sum"><h2>Brief <span class="cite">${esc(s.method)}</span></h2>${s.llm_brief?`<p><b>${esc(s.llm_brief.text)}</b></p>`:""}${s.paragraphs.map(x=>`<p>${esc(x.text)} ${x.claim_ids.length?`<span class="cite">[${x.claim_ids.length} cited facts]</span>`:""}</p>`).join("")}
 ${s.flags.map(f=>`<div class="flag ${f.severity}">${esc(f.flag)}</div>`).join("")}</div>`;
 if(p.changes.length)h+=`<div class="card"><h2>Changes since previous run</h2><table>${p.changes.map(c=>`<tr><td class="k">${esc(c.change_type)} <span class="cite">${esc(c.materiality)}</span></td><td>${esc(c.summary)}</td></tr>`).join("")}</table></div>`;
 for(const x of SEC){const cs=p.claims.filter(c=>c.s===x);const st=p.sec[x];
  h+=`<div class="card"><h2>${ST[x]} <span class="badge b-${st.a}">${st.a.replace("_"," ")}</span></h2>`;
  if(x==="financials")h+=finTable(p);
  const show=x==="financials"?cs.filter(c=>!["revenue","operating_result","annual_result","equity","total_assets","total_debt","cash_and_bank","payroll_expenses"].includes(c.f)||c.pt!=="SELSKAP"):cs;
  const lim=x==="financials"?60:200;
  if(show.length)h+=`<details ${x!=="financials"?"open":""}><summary class="cite">${show.length} fact(s) with sources</summary><table>${show.slice(0,lim).map(row).join("")}</table></details>`;
  if(!cs.length)h+=`<div class="sub">${esc(st.r||"Not found in checked sources.")}</div>`;
  if(st.src&&st.src.length)h+=`<div class="cite">Sources checked: ${st.src.map(u=>`<a href="${esc(u)}" target="_blank" rel="noopener nofollow">${esc(u.replace(/^https?:\/\//,"").slice(0,60))}</a>`).join(", ")}</div>`;
  h+=`</div>`}
 h+=`<div class="card cite">Run ${esc(p.run.run_id)} · completed ${esc(p.run.completed_at)}${p.run.previous_run_id?" · previous run "+esc(p.run.previous_run_id):""}${p.errors.length?" · errors: "+esc(p.errors.map(e=>e.module+": "+e.message).join("; ")):""}</div>`;
 $("#main").innerHTML=h;$("#main").scrollTop=0;window.scrollTo(0,0)}
function val(p,f){const cs=p.claims.filter(c=>c.f===f&&c.pt!=="KONSERN").sort((a,b)=>String(b.p).localeCompare(String(a.p)));return cs[0]?cs[0].v+(cs[0].p?` (${cs[0].p})`:""):"—"}
function renderCompare(){document.body.classList.add("detail");const [a,b]=picked.map(o=>DATA.find(x=>x.org===o));
 const rows=[["Org.nr",p=>p.org],["Legal form",p=>p.form||"—"],["Municipality",p=>p.muni||"—"],["Industry",p=>val(p,"industry_nace")],["Founded",p=>val(p,"founded_date")],["Employees (registry)",p=>val(p,"registered_employees")],
 ["Revenue",p=>val(p,"revenue")],["Operating result",p=>val(p,"operating_result")],["Net result",p=>val(p,"annual_result")],["Equity",p=>val(p,"equity")],["Total assets",p=>val(p,"total_assets")],
 ["CEO",p=>val(p,"ceo")],["Board chair",p=>val(p,"board_chair")],["Workplaces",p=>p.claims.filter(c=>c.f==="registered_workplace").length],["Website",p=>p.web||"—"],
 ["Profiles",p=>p.claims.filter(c=>c.f==="social_profile").map(c=>c.v.split(":")[0]).join(", ")||"—"],["Active job postings",p=>p.claims.filter(c=>c.f==="job_posting").length],["Flags",p=>p.sum.flags.map(f=>f.flag).join("; ")||"—"]];
 $("#main").innerHTML=`<button class="back" onclick="history.back()">← Back</button><div class="card"><h2>Compare</h2><table class="cmp"><tr><th></th><th>${esc(a.name)}</th><th>${esc(b.name)}</th></tr>${rows.map(([k,f])=>`<tr><td class="k">${k}</td><td>${esc(f(a))}</td><td>${esc(f(b))}</td></tr>`).join("")}</table><p class="cite">Open either profile to verify each value against its source.</p><button onclick="location.hash='${a.org}'">Open ${esc(a.name)}</button> <button onclick="location.hash='${b.org}'">Open ${esc(b.name)}</button></div>`}
$("#q").oninput=renderList;$("#flt").onchange=renderList;
$("#cmpBtn").onclick=()=>{cmp=!cmp;picked=[];$("#cmpBtn").classList.toggle("on",cmp);$("#cmpBtn").textContent=cmp?"Pick two…":"Compare";document.body.classList.remove("detail");renderList()};
window.onhashchange=()=>{const o=location.hash.slice(1);if(/^\d{9}$/.test(o)){cmp=false;$("#cmpBtn").classList.remove("on");$("#cmpBtn").textContent="Compare";renderProfile(o)}else{document.body.classList.remove("detail")}};
renderList();if(location.hash)window.onhashchange();
</script></body></html>"""
