"""Offline checks for cross-paper concepts, reference IDs, and field records."""
from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from test_paperread import FakeClient, paperread


PAPER = "2610.27436v1"


class FieldReadingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.store = paperread.Store(self.home, client=FakeClient())
        self.path = self.home / "papers" / PAPER
        paperread.write_json(self.path / "paper.json", {})

    def cli(self, *args, record=None):
        output = StringIO()
        with mock.patch.object(paperread, "Store", return_value=self.store), \
                mock.patch.object(paperread.sys, "stdin", StringIO(json.dumps(record))), \
                redirect_stdout(output):
            code = paperread.main(list(args))
        return code, json.loads(output.getvalue())

    def profile(self):
        data = {"background": "Synthetic background", "areas": {}, "concepts": {
            "winding": {"status": "explained", "paper": PAPER, "where": "S2.p1", "updated": "2026-11-04"},
            "old version": {"status": "known", "paper": "2610.27436v2", "where": "S2", "source": "Synthetic explicit confirmation"},
            "unresolved": {"status": "unsure", "paper": "1002.3895v2", "where": "S1.p1"},
            "old ID": {"status": "explained", "paper": "cond-mat_0506581v1", "where": "S1"},
            "no provenance": {"status": "known", "source": "Synthetic prior knowledge"},
        }}
        paperread.write_json(self.home / "profile.json", data)
        return data

    def test_concept_status_filter_retains_provenance_and_does_not_rewrite_profile(self):
        self.profile()
        original = (self.home / "profile.json").read_bytes()
        code, result = self.cli("profile", "concepts", "--status", "explained")
        self.assertEqual(code, 0)
        self.assertEqual([r["concept"] for r in result], ["winding", "old ID"])
        self.assertEqual(result[0]["paper"], PAPER)
        self.assertEqual(result[0]["where"], "S2.p1")
        self.assertEqual((self.home / "profile.json").read_bytes(), original)

    def test_excluding_arxiv_paper_excludes_versions_and_accepts_urls(self):
        self.profile()
        for value in (PAPER, "2610.27436", "https://arxiv.org/abs/2610.27436v3"):
            with self.subTest(value=value):
                code, result = self.cli("profile", "concepts", "--exclude-paper", value)
                self.assertEqual(code, 0)
                self.assertEqual([r["concept"] for r in result], ["unresolved", "old ID", "no provenance"])
                self.assertIsNone(result[-1]["paper"])
                self.assertIsNone(result[-1]["where"])

    def test_concepts_combine_filters_and_normalize_old_arxiv_cache_names(self):
        self.profile()
        code, result = self.cli("profile", "concepts", "--status", "explained", "--exclude-paper", "cond-mat/0506581v4")
        self.assertEqual(code, 0)
        self.assertEqual([r["concept"] for r in result], ["winding"])

    def test_local_paper_exclusion_is_exact(self):
        paperread.write_json(self.home / "profile.json", {"background": "", "areas": {}, "concepts": {
            "local": {"status": "explained", "paper": "notes-12345678"},
            "another": {"status": "unsure", "paper": "notes-abcdefgh"},
        }})
        self.assertEqual([r["concept"] for r in self.store.profile_concepts(exclude_paper="notes-12345678")], ["another"])

    def test_empty_profile_and_invalid_filters_are_safe(self):
        self.assertEqual(self.cli("profile", "concepts"), (0, []))
        self.assertFalse((self.home / "profile.json").exists())
        for args in (("--status", "mastered"), ("--exclude-paper", "../../profile.json")):
            code, result = self.cli("profile", "concepts", *args)
            self.assertEqual(code, 1)
            self.assertIn("error", result)

    def item(self, **changes):
        item = {"label": "[26]", "title": "Synthetic foundation", "role": "foundational",
                "checked": "abstract", "link": "https://doi.org/10.0000/example"}
        item.update(changes)
        return item

    def test_field_position_and_next_roundtrip_without_changing_profile(self):
        self.profile()
        original = (self.home / "profile.json").read_bytes()
        for record in (
            {"type": "field", "kind": "position", "where": None, "items": [self.item()]},
            {"type": "field", "kind": "next", "items": [self.item(label=None, role="notes", checked="fulltext", missing=["band theory"])]},
        ):
            code, result = self.cli("log", "add", PAPER, "--json", "-", record=record)
            self.assertEqual(code, 0)
            self.assertIsNone(result["where"])
        code, records = self.cli("log", "show", PAPER, "--type", "field", "--last", "0")
        self.assertEqual(code, 0)
        self.assertEqual([r["kind"] for r in records], ["position", "next"])
        self.assertEqual(records[1]["items"][0]["missing"], ["band theory"])
        self.assertEqual((self.home / "profile.json").read_bytes(), original)

    def test_invalid_field_record_does_not_append(self):
        good = {"type": "field", "kind": "position", "items": [self.item()]}
        self.store.log_add(PAPER, good)
        path = self.path / "log.jsonl"
        original = path.read_bytes()
        bad = [dict(good, kind="other"), dict(good, where="S1"), dict(good, segment=None),
               dict(good, items=None), dict(good, items={}), dict(good, items=["not an item"]),
               dict(good, items=[]), dict(good, kind="next", items=[])]
        for record in bad:
            with self.subTest(record=record), self.assertRaises(paperread.PaperError):
                self.store.log_add(PAPER, record)
        self.assertEqual(path.read_bytes(), original)

    def test_field_required_item_fields_and_kind_specific_roles(self):
        invalid = [self.item(title=""), self.item(title=None), self.item(label=""),
                   self.item(label=None), self.item(role="notes"), self.item(role="recent"),
                   self.item(checked="title"), self.item(checked=None),
                   self.item(link="javascript:alert(1)"), self.item(link="not a URL"),
                   self.item(link="https://["), self.item(link="https://bad host.example")]
        for key in ("label", "title", "role", "checked", "link"):
            item = self.item()
            del item[key]
            invalid.append(item)
        for item in invalid:
            with self.subTest(item=item), self.assertRaises(paperread.PaperError):
                self.store.log_add(PAPER, {"type": "field", "kind": "position", "items": [item]})
        self.assertFalse((self.path / "log.jsonl").exists())

    def test_next_requires_missing_array_and_does_not_accept_position_only_roles(self):
        for item in (self.item(), self.item(missing=None), self.item(missing="QM"),
                     self.item(missing=[1]), self.item(role="contrast", missing=[]),
                     self.item(role="predecessor", missing=[])):
            with self.subTest(item=item), self.assertRaises(paperread.PaperError):
                self.store.log_add(PAPER, {"type": "field", "kind": "next", "items": [item]})
        for role in ("notes", "foundational", "recent"):
            record = self.store.log_add(PAPER, {"type": "field", "kind": "next",
                                               "items": [self.item(label="", role=role, missing=[])]})
            self.assertEqual(record["items"][0]["missing"], [])


class ReferenceIdentifierTests(unittest.TestCase):
    def ref(self, text="", links=None):
        item = {"id": "bib.bib19", "label": "[26]", "text": text, "links": links or []}
        result = paperread.reference({"references": [item]}, "26")
        self.assertNotIn("doi", item)  # Enrichment does not rewrite cached records.
        return result

    def test_exact_links_supply_doi_and_versioned_arxiv(self):
        result = self.ref(links=["https://dx.doi.org/10.1103/PhysRevLett.113.046802",
                                "https://arxiv.org/pdf/1311.5233v2.pdf"])
        self.assertEqual(result["doi"], "10.1103/physrevlett.113.046802")
        self.assertEqual(result["arxiv_id"], "1311.5233v2")

    def test_text_ids_handle_citation_punctuation_and_old_arxiv_ids(self):
        result = self.ref("A source (doi:10.1000/example(foo)). arXiv:cond-mat/0506581v2.")
        self.assertEqual(result["doi"], "10.1000/example(foo)")
        self.assertEqual(result["arxiv_id"], "cond-mat/0506581v2")

    def test_link_doi_precedes_prose_and_encoded_suffix_is_preserved(self):
        result = self.ref("doi:10.1000/wrong", ["https://doi.org/10.1000/a%28b%29"])
        self.assertEqual(result["doi"], "10.1000/a(b)")

    def test_prose_sici_doi_preserves_internal_angle_brackets(self):
        doi = "10.1002/(SICI)1099-0380(199910)6:10<747::AID-PC580>3.0.CO;2-C"
        self.assertEqual(self.ref("doi:" + doi + ".")["doi"], doi.lower())
        self.assertEqual(self.ref("<" + doi + ">.")["doi"], doi.lower())

    def test_arxiv_doi_and_plain_arxiv_url_are_usable(self):
        result = self.ref(links=["https://doi.org/10.48550/arXiv.1509.02295"])
        self.assertEqual(result["arxiv_id"], "1509.02295")
        self.assertEqual(self.ref("See https://arxiv.org/abs/1002.3895v2.")["arxiv_id"], "1002.3895v2")

    def test_no_identifier_is_null_not_a_guessed_lookup(self):
        result = self.ref("A. Author, Journal 10, 20 (2000).")
        self.assertIsNone(result["doi"])
        self.assertIsNone(result["arxiv_id"])


if __name__ == "__main__":
    unittest.main()
