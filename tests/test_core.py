"""Offline regression tests (no network). Run: python -m unittest discover -s tests"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from signalpost import web  # noqa: E402
from signalpost.claims import Collector  # noqa: E402
from signalpost.refresh import Store, diff  # noqa: E402
from signalpost.run import envelope  # noqa: E402


def _site(origin, **kw):
    r = web.SiteResult(candidate="https://x.no/", origin=origin, domain=kw.pop("domain", "x.no"))
    for k, v in kw.items():
        setattr(r, k, v)
    return r


IDENT = {"organisation_number": "986308970", "name": "GURIÅSEN AS", "phone": "22 00 00 00"}


class OrgNumbers(unittest.TestCase):
    def test_mod11(self):
        self.assertTrue(web.mod11_ok("938702675"))
        self.assertFalse(web.mod11_ok("938702676"))

    def test_find_with_context(self):
        allv, ctx = web.find_org_numbers("Kontakt oss. Org.nr: 938 702 675 MVA. Tlf 123 456 789")
        self.assertIn("938702675", ctx)
        self.assertEqual(web.org_span("Org.nr. 938 702 675", "938702675")[:7], "Org.nr.")


class IdentityGate(unittest.TestCase):
    def test_shared_email_domain_is_ambiguous(self):
        r = _site("registry_email_domain", domain="formue.no", phone_match=True, shared=18)
        web._decide(r, IDENT)
        self.assertEqual(r.verdict, "ambiguous")

    def test_email_domain_phone_only_is_not_enough(self):
        r = _site("registry_email_domain", domain="formue.no", phone_match=True, shared=1)
        web._decide(r, IDENT)
        self.assertNotEqual(r.verdict, "accepted_registry")

    def test_org_number_on_site_wins(self):
        r = _site("name_guess", org_found=True)
        web._decide(r, IDENT)
        self.assertEqual(r.verdict, "verified")

    def test_franchise_domain_needs_name(self):
        r = _site("registry_declared", domain="7-eleven.no", shared=120)
        web._decide(r, {"organisation_number": "935354293", "name": "BUTIKKDRIFT UMAZABAL GUERRA AS"})
        self.assertEqual(r.verdict, "ambiguous")

    def test_registry_site_contradicted(self):
        r = _site("registry_declared", domain="studiox.no", other_orgs={"920990029"})
        web._decide(r, {"organisation_number": "914561302", "name": "APPELX AS"})
        self.assertEqual(r.verdict, "ambiguous")

    def test_guess_without_proof_rejected(self):
        r = _site("name_guess", domain="guriasen.no", name_match=True)
        web._decide(r, IDENT)
        self.assertEqual(r.verdict, "rejected")


class Social(unittest.TestCase):
    def test_share_links_dropped(self):
        self.assertIsNone(web.normalize_social("https://www.facebook.com/sharer/sharer.php?u=x"))
        self.assertIsNone(web.normalize_social("https://www.linkedin.com/in/some-person"))
        self.assertEqual(web.normalize_social("https://no.linkedin.com/company/af-gruppen/")["handle"], "af-gruppen")
        self.assertEqual(web.normalize_social("https://www.instagram.com/af_gruppen/")["platform"], "instagram")


class RefreshAndEnvelope(unittest.TestCase):
    def _col(self, name="A AS", ceo="Kari Nordmann"):
        c = Collector("123456785")
        e = c.ev(url="https://data.brreg.no/x", source_class="t", retrieved_at="2026-01-01T00:00:00Z", sha256="h", span="s", method="m")
        c.claim("identity", "legal_name", name, [e])
        c.claim("leadership", "ceo", {"person_name": ceo, "role_code": "DAGL"}, [e], key=("DAGL", ceo))
        c.section("identity", "available")
        c.section("leadership", "available")
        return c

    def test_idempotent_and_typed_changes(self):
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d))
            res = {"collector": self._col(), "ident": {"organisation_number": "123456785", "name": "A AS"}, "extra": {}, "runtime_ms": 1}
            e1 = envelope("123456785", res, run_id="r1", started="t", err=None, store=store, now="2026-01-01T00:00:00Z")
            store.save_run("r1", [e1])
            res2 = {"collector": self._col(), "ident": {"organisation_number": "123456785", "name": "A AS"}, "extra": {}, "runtime_ms": 1}
            e2 = envelope("123456785", res2, run_id="r2", started="t", err=None, store=store, now="2026-01-02T00:00:00Z")
            self.assertEqual(e2["changes"], [])
            self.assertEqual([c["claim_id"] for c in e1["claims"]], [c["claim_id"] for c in e2["claims"]])
            store.save_run("r2", [e2])
            res3 = {"collector": self._col(ceo="Ola Nordmann"), "ident": {"organisation_number": "123456785", "name": "A AS"}, "extra": {}, "runtime_ms": 1}
            e3 = envelope("123456785", res3, run_id="r3", started="t", err=None, store=store, now="2026-01-03T00:00:00Z")
            types = sorted(c["change_type"] for c in e3["changes"])
            self.assertEqual(types, ["ended_role", "new_role"])

    def test_failed_company_still_gets_terminal_envelope(self):
        with tempfile.TemporaryDirectory() as d:
            e = envelope("123456785", None, run_id="r1", started="t", err="boom", store=Store(Path(d)), now="n")
            self.assertEqual(e["availability"], "failed")
            self.assertEqual(len(e["sections"]), 7)
            json.dumps(e)


if __name__ == "__main__":
    unittest.main()
