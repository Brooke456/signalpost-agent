"""Brønnøysund (Enhetsregisteret + Regnskapsregisteret) — the identity anchor.

All data here is keyed by the exact organisation number. Licence: NLOD 2.0 (data.brreg.no).
"""
from __future__ import annotations

import csv
import gzip
import hashlib
import io
import re
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

from .claims import Collector
from .http import fetch_json, utc_now

NLOD = "NLOD 2.0 (Brønnøysundregistrene open data)"
BULK_URL = "https://data.brreg.no/enhetsregisteret/api/enheter/lastned/csv"
ENTITY = "https://data.brreg.no/enhetsregisteret/api/enheter/{org}"
ROLES = "https://data.brreg.no/enhetsregisteret/api/enheter/{org}/roller"
SUBUNITS = "https://data.brreg.no/enhetsregisteret/api/underenheter?overordnetEnhet={org}&size=200"
GROUP = "https://data.brreg.no/enhetsregisteret/api/konsernstruktur/{org}"
ACCOUNTS = "https://data.brreg.no/regnskapsregisteret/regnskap/{org}"
ACCOUNT_COPY = "https://data.brreg.no/regnskapsregisteret/regnskap/aarsregnskap/kopi/{org}/{year}"
PUBLIC_ENTITY_PAGE = "https://virksomhet.brreg.no/nb/oppslag/enheter/{org}"

LEGAL_SUFFIX = re.compile(r"\b(AS|ASA|A/S|ANS|DA|SA|BA|KS|NUF|ENK|IKS|SE|STI|STIFTELSEN|BRL|BORETTSLAG|SAMEIE|ESEK|AKSJESELSKAP|IN LIQUIDATION|UNDER AVVIKLING|KONKURSBO)\b\.?", re.I)


def fold(text: str) -> str:
    text = (text or "").lower().replace("æ", "ae").replace("ø", "o").replace("å", "a")
    text = unicodedata.normalize("NFKD", text)
    return "".join(c for c in text if not unicodedata.combining(c))


def norm_name(name: str) -> str:
    n = fold(name)
    n = re.sub(r"[^a-z0-9& ]+", " ", n)
    return re.sub(r"\s+", " ", n).strip()


def core_name(name: str) -> str:
    n = LEGAL_SUFFIX.sub(" ", name or "")
    return norm_name(n)


# ---------------------------------------------------------------- bulk snapshot
class Bulk:
    """Frozen registry snapshot (Brreg CSV/CSV.gz or the Signalpost universe JSONL.gz)."""

    def __init__(self, path: str | None, wanted: Iterable[str]):
        self.path = path
        self.rows: dict[str, dict[str, str]] = {}
        self.name_counts: Counter[str] = Counter()
        self.email_domain_counts: Counter[str] = Counter()
        self.web_domain_counts: Counter[str] = Counter()
        self.sha256: str | None = None
        self.rows_scanned = 0
        if not path:
            return
        wanted = set(wanted)
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        self.sha256 = h.hexdigest()
        opener = gzip.open if str(path).endswith(".gz") else open
        with opener(path, "rt", encoding="utf-8", newline="") as fh:
            first = fh.readline()
            fh.seek(0)
            if first.lstrip().startswith("{"):
                import json
                for line in fh:
                    if not line.strip():
                        continue
                    r = json.loads(line)
                    self._ingest({"organisasjonsnummer": r.get("organisation_number", ""), "navn": r.get("name", ""),
                                  "organisasjonsform.kode": r.get("legal_form", ""), "hjemmeside": r.get("website", ""),
                                  "naeringskode1.kode": r.get("industry_code", ""), "naeringskode1.beskrivelse": r.get("industry_label", ""),
                                  "forretningsadresse.kommune": r.get("municipality", ""),
                                  "antallAnsatte": "" if r.get("employees") is None else str(r.get("employees")),
                                  "sisteInnsendteAarsregnskap": r.get("latest_submitted_accounts", "")}, wanted)
            else:
                for row in csv.DictReader(fh):
                    self._ingest(row, wanted)

    def _ingest(self, row: dict[str, str], wanted: set[str]) -> None:
        self.rows_scanned += 1
        org = row.get("organisasjonsnummer") or row.get("Organisasjonsnummer") or ""
        name = row.get("navn") or row.get("Navn") or ""
        if name:
            self.name_counts[norm_name(name)] += 1
        email = (row.get("epostadresse") or "").strip().lower()
        if "@" in email:
            self.email_domain_counts[email.rsplit("@", 1)[1]] += 1
        web = (row.get("hjemmeside") or "").strip().lower()
        if web:
            self.web_domain_counts[_domain_of(web)] += 1
        if org in wanted:
            self.rows[org] = row

    def metadata(self) -> dict[str, Any]:
        return {"path": str(self.path) if self.path else None, "sha256": self.sha256, "rows_scanned": self.rows_scanned,
                "selected": len(self.rows), "source": BULK_URL}


def _domain_of(url: str) -> str:
    u = url.strip().lower()
    u = re.sub(r"^[a-z]+://", "", u)
    u = u.split("/")[0].split("?")[0].split("#")[0].split(":")[0]
    if u.startswith("www."):
        u = u[4:]
    return u


# ---------------------------------------------------------------- helpers
def _addr(a: dict[str, Any] | None) -> str | None:
    if not a:
        return None
    lines = [x for x in (a.get("adresse") or []) if x]
    tail = " ".join(x for x in [a.get("postnummer"), a.get("poststed")] if x)
    parts = lines + ([tail] if tail else [])
    if a.get("landkode") and a.get("landkode") != "NO":
        parts.append(a.get("land") or a.get("landkode"))
    return ", ".join(parts) or None


def _bulk_addr(row: dict[str, str], prefix: str) -> str | None:
    adr = row.get(f"{prefix}.adresse") or ""
    tail = " ".join(x for x in [row.get(f"{prefix}.postnummer"), row.get(f"{prefix}.poststed")] if x)
    parts = [x for x in [adr.strip(), tail] if x]
    return ", ".join(parts) or None


def _g(d: Any, *path: str) -> Any:
    for p in path:
        if not isinstance(d, dict):
            return None
        d = d.get(p)
    return d


# ---------------------------------------------------------------- entity
def collect_entity(col: Collector, org: str, bulk_row: dict[str, str] | None, bulk_meta: dict[str, Any]) -> dict[str, Any]:
    """Returns a normalized identity dict (used by later modules) and records identity claims."""
    url = ENTITY.format(org=org)
    resp, body = fetch_json(url)
    ident: dict[str, Any] = {"organisation_number": org}
    sources = []
    if resp.status == 410 or (resp.status == 404 and not bulk_row):
        col.section("identity", "not_available", reason=f"entity API HTTP {resp.status} (deleted or unknown)", sources=[url])
    if resp.ok and isinstance(body, dict):
        sources.append(url)
        def ev(span: str) -> str:
            return col.ev(url=url, source_class="official_registry_api", retrieved_at=resp.retrieved_at, sha256=resp.sha256,
                          span=span, method="brreg_entity_json", http_status=resp.status, rights=NLOD)
        ident.update({
            "name": body.get("navn"),
            "legal_form": _g(body, "organisasjonsform", "kode"),
            "legal_form_label": _g(body, "organisasjonsform", "beskrivelse"),
            "website": body.get("hjemmeside"),
            "email": body.get("epostadresse"),
            "phone": body.get("telefon") or body.get("mobil"),
            "municipality": _g(body, "forretningsadresse", "kommune") or _g(body, "postadresse", "kommune"),
            "business_address": body.get("forretningsadresse"),
            "postal_address": body.get("postadresse"),
            "industry_code": _g(body, "naeringskode1", "kode"),
            "industry": _g(body, "naeringskode1", "beskrivelse"),
            "employees": body.get("antallAnsatte"),
            "has_registered_employees": body.get("harRegistrertAntallAnsatte"),
            "founded": body.get("stiftelsesdato"),
            "registered": body.get("registreringsdatoEnhetsregisteret"),
            "latest_accounts": body.get("sisteInnsendteAarsregnskap"),
            "bankrupt": body.get("konkurs"), "liquidating": body.get("underAvvikling"),
            "forced_dissolution": body.get("underTvangsavviklingEllerTvangsopplosning"),
            "in_group": body.get("erIKonsern"),
            "purpose": body.get("vedtektsfestetFormaal"),
            "activity": body.get("aktivitet"),
            "historical_names": body.get("historiskeNavn") or [],
            "capital": body.get("kapital"),
            "articles_date": body.get("vedtektsdato"),
            "vat": body.get("registrertIMvaregisteret"),
            "parent_unit": body.get("overordnetEnhet"),
        })
        c = col.claim
        c("identity", "organisation_number", org, [ev(f"organisasjonsnummer: {body.get('organisasjonsnummer')}")])
        c("identity", "legal_name", ident["name"], [ev(f"navn: {ident['name']}")])
        if ident["legal_form"]:
            c("identity", "legal_form", {"code": ident["legal_form"], "label": ident["legal_form_label"]},
              [ev(f"organisasjonsform: {ident['legal_form']} ({ident['legal_form_label']})")], key=ident["legal_form"])
        if body.get("forretningsadresse"):
            a = _addr(body["forretningsadresse"])
            c("identity", "registered_business_address", a, [ev(f"forretningsadresse: {a}")])
        if body.get("postadresse"):
            a = _addr(body["postadresse"])
            c("identity", "postal_address", a, [ev(f"postadresse: {a}")])
        if ident["municipality"]:
            c("identity", "municipality", ident["municipality"], [ev(f"kommune: {ident['municipality']}")])
        for i, key in enumerate(["naeringskode1", "naeringskode2", "naeringskode3"]):
            n = body.get(key)
            if n:
                c("identity", "industry_nace" if i == 0 else "secondary_industry_nace", {"code": n.get("kode"), "label": n.get("beskrivelse")},
                  [ev(f"{key}: {n.get('kode')} {n.get('beskrivelse')}")], key=n.get("kode"))
        if body.get("harRegistrertAntallAnsatte") and body.get("antallAnsatte") is not None:
            c("identity", "registered_employees", body["antallAnsatte"], [ev(f"antallAnsatte: {body['antallAnsatte']}")],
              effective_date=body.get("registreringsdatoAntallAnsatteNAVAaregisteret") or body.get("registreringsdatoAntallAnsatteEnhetsregisteret"),
              note="Employee count registered in Enhetsregisteret/Aa-registeret (not an FTE measure).")
        if ident["founded"]:
            c("identity", "founded_date", ident["founded"], [ev(f"stiftelsesdato: {ident['founded']}")])
        if ident["registered"]:
            c("identity", "registered_date", ident["registered"], [ev(f"registreringsdatoEnhetsregisteret: {ident['registered']}")])
        status = "active"
        if ident["bankrupt"]:
            status = "bankrupt"
        elif ident["liquidating"]:
            status = "under_liquidation"
        elif ident["forced_dissolution"]:
            status = "under_forced_dissolution"
        c("identity", "registry_status", status,
          [ev(f"konkurs: {ident['bankrupt']}; underAvvikling: {ident['liquidating']}; underTvangsavviklingEllerTvangsopplosning: {ident['forced_dissolution']}")])
        if ident["capital"] and ident["capital"].get("belop") is not None:
            cap = ident["capital"]
            c("identity", "share_capital", {"amount": cap.get("belop"), "currency": cap.get("valuta"), "type": cap.get("type"), "shares": cap.get("antallAksjer")},
              [ev(f"kapital.belop: {cap.get('belop')} {cap.get('valuta')}; antallAksjer: {cap.get('antallAksjer')}")],
              effective_date=cap.get("innfortDato"), key=cap.get("belop"))
        if ident["purpose"]:
            p = " ".join(ident["purpose"]) if isinstance(ident["purpose"], list) else str(ident["purpose"])
            c("identity", "statutory_purpose", p, [ev(f"vedtektsfestetFormaal: {p}")], effective_date=ident["articles_date"])
        if ident["activity"]:
            p = " ".join(ident["activity"]) if isinstance(ident["activity"], list) else str(ident["activity"])
            c("identity", "registered_activity", p, [ev(f"aktivitet: {p}")])
        if ident["vat"] is not None:
            c("identity", "vat_registered", bool(ident["vat"]), [ev(f"registrertIMvaregisteret: {ident['vat']}")],
              effective_date=body.get("registreringsdatoMerverdiavgiftsregisteret"))
        if body.get("registrertIForetaksregisteret") is not None:
            c("identity", "in_business_register", bool(body.get("registrertIForetaksregisteret")),
              [ev(f"registrertIForetaksregisteret: {body.get('registrertIForetaksregisteret')}")])
        if ident["in_group"] is not None:
            c("identity", "part_of_group", bool(ident["in_group"]), [ev(f"erIKonsern: {ident['in_group']}")])
        for hn in ident["historical_names"]:
            nm = hn.get("navn") if isinstance(hn, dict) else hn
            if nm:
                c("identity", "former_name", nm, [ev(f"historiskeNavn: {nm}")])
        if ident["website"]:
            c("identity", "registry_declared_website", ident["website"], [ev(f"hjemmeside: {ident['website']}")],
              note="Website as declared by the company to Enhetsregisteret.")
        if ident["phone"]:
            c("identity", "registry_phone", ident["phone"], [ev(f"telefon: {ident['phone']}")])
        if ident["email"]:
            c("identity", "registry_email", ident["email"], [ev(f"epostadresse: {ident['email']}")])
        c("identity", "registry_profile_url", PUBLIC_ENTITY_PAGE.format(org=org), [ev(f"organisasjonsnummer: {org}")], key="page")
        col.section("identity", "available", sources=[url])
    elif bulk_row:
        # Fall back to the frozen snapshot only.
        snap_ev = col.ev(url=BULK_URL, source_class="official_registry_bulk", retrieved_at=bulk_meta.get("retrieved_at") or utc_now(),
                         sha256=bulk_meta.get("sha256"), span=f"row organisasjonsnummer={org}", method="brreg_bulk_csv_row", rights=NLOD)
        ident.update({
            "name": bulk_row.get("navn"), "legal_form": bulk_row.get("organisasjonsform.kode"),
            "website": bulk_row.get("hjemmeside"), "email": bulk_row.get("epostadresse"),
            "phone": bulk_row.get("telefon") or bulk_row.get("mobil"),
            "municipality": bulk_row.get("forretningsadresse.kommune"), "industry": bulk_row.get("naeringskode1.beskrivelse"),
            "industry_code": bulk_row.get("naeringskode1.kode"), "founded": bulk_row.get("stiftelsesdato"),
            "latest_accounts": bulk_row.get("sisteInnsendteAarsregnskap"),
        })
        col.claim("identity", "organisation_number", org, [snap_ev])
        col.claim("identity", "legal_name", ident["name"], [snap_ev])
        col.claim("identity", "legal_form", {"code": ident["legal_form"], "label": bulk_row.get("organisasjonsform.beskrivelse")}, [snap_ev], key=ident["legal_form"])
        a = _bulk_addr(bulk_row, "forretningsadresse")
        col.claim("identity", "registered_business_address", a, [snap_ev])
        col.claim("identity", "municipality", ident["municipality"], [snap_ev])
        if ident["industry_code"]:
            col.claim("identity", "industry_nace", {"code": ident["industry_code"], "label": ident["industry"]}, [snap_ev], key=ident["industry_code"])
        col.claim("identity", "founded_date", ident["founded"], [snap_ev])
        if ident["website"]:
            col.claim("identity", "registry_declared_website", ident["website"], [snap_ev])
        col.error("registry_live", f"entity API unavailable (HTTP {resp.status} {resp.error}); used frozen snapshot", url)
        col.section("identity", "available", reason="frozen registry snapshot only", sources=[BULK_URL])
    else:
        col.error("registry_live", f"entity API HTTP {resp.status} {resp.error}", url)
        col.section("identity", "failed", reason="entity API unavailable and organisation not in snapshot", sources=[url])
    # bulk email is authoritative when API lacks it
    if bulk_row and not ident.get("email") and bulk_row.get("epostadresse"):
        ident["email"] = bulk_row.get("epostadresse")
    return ident


# ---------------------------------------------------------------- roles
ROLE_ORDER = {"DAGL": 0, "LEDE": 1, "NEST": 2, "MEDL": 3, "VARA": 4, "OBS": 5, "KONT": 6, "FFØR": 7, "INNH": 1, "DTPR": 2,
              "DTSO": 2, "KOMP": 2, "REVI": 8, "REGN": 9, "SAM": 3, "BEST": 3}


def collect_roles(col: Collector, org: str) -> list[dict[str, Any]]:
    url = ROLES.format(org=org)
    resp, body = fetch_json(url)
    out: list[dict[str, Any]] = []
    if not resp.ok or not isinstance(body, dict):
        if resp.status == 404:
            col.section("leadership", "not_available", reason="no roles registered", sources=[url])
        else:
            col.error("roles", f"HTTP {resp.status} {resp.error}", url)
            col.section("leadership", "failed", reason="roles API error", sources=[url])
        return out
    for group in body.get("rollegrupper", []) or []:
        changed = group.get("sistEndret")
        gcode = _g(group, "type", "kode")
        glabel = _g(group, "type", "beskrivelse")
        for item in group.get("roller", []) or []:
            if item.get("avregistrert") or item.get("fratraadt"):
                continue
            person = item.get("person") or {}
            if person.get("erDoed"):
                continue
            nm = person.get("navn") or {}
            entity = item.get("enhet") or {}
            name = " ".join(x for x in [nm.get("fornavn"), nm.get("mellomnavn"), nm.get("etternavn")] if x)
            ename = " ".join(entity.get("navn") or []) if isinstance(entity.get("navn"), list) else entity.get("navn")
            rcode = _g(item, "type", "kode")
            rlabel = _g(item, "type", "beskrivelse")
            rec = {"role_code": rcode, "role": rlabel, "group": glabel, "last_changed": changed}
            if name:
                rec["person_name"] = name
            if ename:
                rec["entity_name"] = ename
                rec["entity_organisation_number"] = entity.get("organisasjonsnummer")
            if not (name or ename):
                continue
            out.append(rec)
            who = name or ename
            e = col.ev(url=url, source_class="official_roles_api", retrieved_at=resp.retrieved_at, sha256=resp.sha256,
                       span=f"rollegruppe {gcode} ({glabel}), sistEndret {changed}: {rlabel} = {who}", method="brreg_roles_json",
                       http_status=resp.status, rights=NLOD)
            field = {"DAGL": "ceo", "LEDE": "board_chair", "NEST": "board_deputy_chair", "MEDL": "board_member", "VARA": "board_deputy_member",
                     "REVI": "auditor", "REGN": "accountant", "INNH": "proprietor", "DTPR": "general_partner", "DTSO": "general_partner",
                     "KONT": "contact_person", "OBS": "board_observer", "FFØR": "business_manager", "KOMP": "general_partner"}.get(rcode, "registered_role")
            col.claim("leadership", field, rec, [e], key=(rcode, who), effective_date=changed, label=rlabel)
    if out:
        col.section("leadership", "available", sources=[url])
    else:
        col.section("leadership", "not_available", reason="roles endpoint returned no active roles", sources=[url])
    out.sort(key=lambda r: ROLE_ORDER.get(r["role_code"], 10))
    return out


# ---------------------------------------------------------------- subunits
def collect_subunits(col: Collector, org: str) -> list[dict[str, Any]]:
    url = SUBUNITS.format(org=org)
    resp, body = fetch_json(url)
    out: list[dict[str, Any]] = []
    if not resp.ok or not isinstance(body, dict):
        col.error("locations", f"HTTP {resp.status} {resp.error}", url)
        col.section("workplaces", "failed", reason="sub-unit API error", sources=[url])
        return out
    rows = _g(body, "_embedded", "underenheter") or []
    total = _g(body, "page", "totalElements")
    for u in rows:
        if u.get("nedleggelsesdato") or u.get("slettedato"):
            continue
        a = _addr(u.get("beliggenhetsadresse") or u.get("postadresse"))
        rec = {"organisation_number": u.get("organisasjonsnummer"), "name": u.get("navn"), "address": a,
               "municipality": _g(u, "beliggenhetsadresse", "kommune"),
               "industry": _g(u, "naeringskode1", "beskrivelse"), "started": u.get("oppstartsdato")}
        if u.get("harRegistrertAntallAnsatte") and u.get("antallAnsatte") is not None:
            rec["registered_employees"] = u.get("antallAnsatte")
        out.append(rec)
        e = col.ev(url=url, source_class="official_subunits_api", retrieved_at=resp.retrieved_at, sha256=resp.sha256,
                   span=f"underenhet {rec['organisation_number']} {rec['name']}: {a}", method="brreg_subunits_json",
                   http_status=resp.status, rights=NLOD)
        col.claim("workplaces", "registered_workplace", rec, [e], key=rec["organisation_number"],
                  effective_date=rec.get("started"))
    if out:
        col.section("workplaces", "available", sources=[url])
        if total and total > len(rows):
            col.error("locations", f"{total} sub-units; first {len(rows)} retrieved", url)
    else:
        col.section("workplaces", "not_available", reason="no active registered sub-units", sources=[url])
    return out


# ---------------------------------------------------------------- group
def collect_group(col: Collector, org: str, ident: dict[str, Any]) -> dict[str, Any]:
    """Parent/child links from konsernstruktur (only fetched when erIKonsern is true)."""
    if not ident.get("in_group"):
        return {}
    url = GROUP.format(org=org)
    resp, body = fetch_json(url)
    if not resp.ok or not isinstance(body, dict):
        return {}
    info: dict[str, Any] = {"children": []}

    def ev(span: str) -> str:
        return col.ev(url=url, source_class="official_group_structure_api", retrieved_at=resp.retrieved_at, sha256=resp.sha256,
                      span=span, method="brreg_konsernstruktur_json", http_status=resp.status, rights=NLOD)

    def walk(node: dict[str, Any], depth: int = 0) -> None:
        if depth > 6 or not isinstance(node, dict):
            return
        if node.get("organisasjonsnummer") == org:
            if node.get("parentOrganisasjonsnummer"):
                p = {"organisation_number": node["parentOrganisasjonsnummer"], "name": node.get("parentNavn"),
                     "relation": _g(node, "knytningsform", "beskrivelse"), "share": node.get("grunnlag")}
                info["parent"] = p
                col.claim("identity", "parent_company", p, [ev(f"{node.get('navn')} parent: {p['name']} ({p['organisation_number']}) {p['relation']} {p['share']}")],
                          key=p["organisation_number"], effective_date=node.get("dato"))
            for ch in node.get("children") or []:
                if ch.get("organisasjonsnummer") and len(info["children"]) < 50:
                    rec = {"organisation_number": ch["organisasjonsnummer"], "name": ch.get("navn"),
                           "relation": _g(ch, "knytningsform", "beskrivelse"), "share": ch.get("grunnlag")}
                    info["children"].append(rec)
                    col.claim("identity", "subsidiary", rec, [ev(f"child of {org}: {rec['name']} ({rec['organisation_number']}) {rec['relation']} {rec['share']}")],
                              key=rec["organisation_number"], effective_date=ch.get("dato"))
        for ch in node.get("children") or []:
            walk(ch, depth + 1)
        if node.get("parent"):
            walk(node["parent"], depth + 1)

    walk(body)
    # top-level ultimate parent
    top = body
    if top.get("organisasjonsnummer") and top.get("organisasjonsnummer") != org:
        rec = {"organisation_number": top.get("organisasjonsnummer"), "name": top.get("navn")}
        info["group_top"] = rec
        col.claim("identity", "group_ultimate_parent", rec, [ev(f"konsern top: {rec['name']} ({rec['organisation_number']})")],
                  key=rec["organisation_number"])
    return info


# ---------------------------------------------------------------- annual accounts
FIN_FIELDS = [
    ("revenue", ("resultatregnskapResultat", "driftsresultat", "driftsinntekter", "sumDriftsinntekter")),
    ("sales_revenue", ("resultatregnskapResultat", "driftsresultat", "driftsinntekter", "salgsinntekter")),
    ("operating_expenses", ("resultatregnskapResultat", "driftsresultat", "driftskostnad", "sumDriftskostnad")),
    ("payroll_expenses", ("resultatregnskapResultat", "driftsresultat", "driftskostnad", "loennskostnad")),
    ("operating_result", ("resultatregnskapResultat", "driftsresultat", "driftsresultat")),
    ("net_financial_items", ("resultatregnskapResultat", "finansresultat", "nettoFinans")),
    ("profit_before_tax", ("resultatregnskapResultat", "ordinaertResultatFoerSkattekostnad")),
    ("tax_expense", ("resultatregnskapResultat", "ordinaertResultatSkattekostnad")),
    ("annual_result", ("resultatregnskapResultat", "aarsresultat")),
    ("total_assets", ("eiendeler", "sumEiendeler")),
    ("fixed_assets", ("eiendeler", "anleggsmidler", "sumAnleggsmidler")),
    ("current_assets", ("eiendeler", "omloepsmidler", "sumOmloepsmidler")),
    ("cash_and_bank", ("eiendeler", "sumBankinnskuddOgKontanter")),
    ("equity", ("egenkapitalGjeld", "egenkapital", "sumEgenkapital")),
    ("total_debt", ("egenkapitalGjeld", "gjeldOversikt", "sumGjeld")),
    ("short_term_debt", ("egenkapitalGjeld", "gjeldOversikt", "kortsiktigGjeld", "sumKortsiktigGjeld")),
    ("long_term_debt", ("egenkapitalGjeld", "gjeldOversikt", "langsiktigGjeld", "sumLangsiktigGjeld")),
]


def collect_accounts(col: Collector, org: str) -> list[dict[str, Any]]:
    url = ACCOUNTS.format(org=org)
    resp, body = fetch_json(url)
    periods: list[dict[str, Any]] = []
    if resp.status == 404:
        col.section("financials", "not_available", reason="no machine-readable accounts in Regnskapsregisteret API", sources=[url])
        return periods
    if not resp.ok or not isinstance(body, list):
        col.error("financials", f"HTTP {resp.status} {resp.error}", url)
        col.section("financials", "failed", reason="accounts API error", sources=[url])
        return periods
    for rec in body:
        period = rec.get("regnskapsperiode") or {}
        to = period.get("tilDato")
        year = (to or "")[:4]
        typ = rec.get("regnskapstype") or "SELSKAP"
        values: dict[str, Any] = {}
        for key, path in FIN_FIELDS:
            v = _g(rec, *path)
            if v is not None:
                values[key] = v
        item = {"year": year, "type": typ, "period": {"from": period.get("fraDato"), "to": to}, "currency": rec.get("valuta"),
                "values": values, "audited": not _g(rec, "revisjon", "ikkeRevidertAarsregnskap"),
                "audit_opt_out": _g(rec, "revisjon", "fravalgRevisjon"), "small_company_rules": _g(rec, "regnkapsprinsipper", "smaaForetak"),
                "accounting_rules": _g(rec, "regnkapsprinsipper", "regnskapsregler"), "plan": rec.get("oppstillingsplan"),
                "liquidation_accounts": rec.get("avviklingsregnskap"), "journal": rec.get("journalnr")}
        periods.append(item)
    periods.sort(key=lambda p: (p["type"] != "SELSKAP", p["year"]), reverse=False)
    if not periods:
        col.section("financials", "not_available", reason="accounts API returned no periods", sources=[url])
        return periods
    years = sorted({p["year"] for p in periods})
    for p in periods:
        rp = {"from": p["period"]["from"], "to": p["period"]["to"], "fiscal_year": p["year"], "account_type": p["type"]}
        prefix = "" if p["type"] == "SELSKAP" else "group_"
        for key, v in p["values"].items():
            path = dict(FIN_FIELDS)[key]
            e = col.ev(url=url, source_class="official_annual_accounts_api", retrieved_at=resp.retrieved_at, sha256=resp.sha256,
                       span=f"[regnskapstype={p['type']}, regnskapsperiode {p['period']['from']}..{p['period']['to']}] {'/'.join(path)} = {v} {p['currency']}",
                       method="brreg_regnskap_json_path", http_status=resp.status, rights=NLOD)
            col.claim("financials", prefix + key, {"amount": v, "currency": p["currency"]}, [e], key=(p["type"], p["year"]),
                      reporting_period=rp)
        meta_e = col.ev(url=url, source_class="official_annual_accounts_api", retrieved_at=resp.retrieved_at, sha256=resp.sha256,
                        span=f"[regnskapstype={p['type']}, {p['year']}] revisjon.ikkeRevidertAarsregnskap={not p['audited']}, fravalgRevisjon={p['audit_opt_out']}, regnskapsregler={p['accounting_rules']}",
                        method="brreg_regnskap_json_path", http_status=resp.status, rights=NLOD)
        col.claim("financials", prefix + "accounts_audited", bool(p["audited"]), [meta_e], key=(p["type"], p["year"]), reporting_period=rp)
        copy = ACCOUNT_COPY.format(org=org, year=p["year"])
        if p["type"] == "SELSKAP":
            col.claim("financials", "annual_accounts_copy", {"year": p["year"], "url": copy}, [meta_e], key=p["year"], reporting_period=rp,
                      note="Official copy of the filed annual accounts (PDF) at Regnskapsregisteret.")
    col.claim("financials", "fiscal_years_available", years, [col.ev(url=url, source_class="official_annual_accounts_api", retrieved_at=resp.retrieved_at,
              sha256=resp.sha256, span=f"regnskapsperiode years: {', '.join(years)}", method="brreg_regnskap_json_path", http_status=resp.status, rights=NLOD)],
              key="years")
    col.section("financials", "available", sources=[url])
    return periods
