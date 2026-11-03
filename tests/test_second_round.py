"""Offline regression checks for implementation.md section 14.

The HTML is hand-authored; no paper downloads or model calls are needed.
"""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills" / "paper-foundations" / "scripts" / "paperread.py"
SPEC = importlib.util.spec_from_file_location("paperread_second_round", SCRIPT)
paperread = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = paperread
SPEC.loader.exec_module(paperread)
HTML = (Path(__file__).parent / "fixtures" / "second_round_latexml.html").read_text(encoding="utf-8")
PAPER = "2610.27436v1"


class NoNetworkClient:
    def get(self, url):
        raise AssertionError("Unexpected network request: " + url)


class SecondRoundParserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = paperread.parse_html(HTML)

    def test_references_keep_source_order_labels_text_and_links(self):
        references = self.data["references"]
        self.assertEqual([r["id"] for r in references], ["bib.bib19", "bib.bib20"])
        self.assertEqual([r["label"] for r in references], ["[26]", "[27]"])
        self.assertIn("A. Example, BIBLIOGRAPHY_ONLY: A toy source.", references[0]["text"])
        self.assertIn("Synthetic Journal 1, 2 (2026).", references[0]["text"])
        self.assertEqual(references[0]["links"], [
            "https://doi.org/10.0000/synthetic.26",
            "https://arxiv.org/abs/0000.00026",
        ])
        self.assertEqual(references[1]["links"], [])
        self.assertNotIn("\n", references[0]["text"])

    def test_bibliography_is_not_a_body_paragraph_or_search_hit(self):
        body = " ".join(p["text"] for p in self.data["paragraphs"])
        self.assertNotIn("BIBLIOGRAPHY_ONLY", body)
        self.assertEqual(paperread.find(self.data, "BIBLIOGRAPHY_ONLY"), [])

    def test_parent_caption_excludes_all_nested_captions(self):
        self.assertEqual([f["id"] for f in self.data["figures"]], ["S2.F1", "S2.T1"])
        figure = self.data["figures"][0]
        self.assertEqual(figure["caption"], "Figure 1: Parent comparison.")
        self.assertEqual(self.data["figures"][1]["caption"], "Table 1: Summary of the model.")

    def test_nested_subcaptions_follow_caption_source_order_without_duplication(self):
        subcaptions = self.data["figures"][0]["subcaptions"]
        self.assertEqual([(s["id"], s["caption"]) for s in subcaptions], [
            ("S2.F1.a.i", "(i) Nested inset is magenta."),
            ("S2.F1.a", "(a) Panel A has a turquoise loop."),
            ("S2.F1.b", "(b) Panel B has an orange path."),
        ])
        self.assertEqual([s["order"] for s in subcaptions], sorted(s["order"] for s in subcaptions))
        self.assertEqual(len({s["order"] for s in subcaptions}), 3)
        self.assertEqual(self.data["figures"][1].get("subcaptions", []), [])

    def test_find_retains_subcaption_search_and_after_boundary(self):
        for term, identifier in (("MAGENTA", "S2.F1.a.i"), ("turquoise", "S2.F1.a"), ("orange", "S2.F1.b")):
            with self.subTest(term=term):
                matches = paperread.find(self.data, term, after="S2.p1")
                self.assertTrue(matches)
                self.assertTrue(any(term.casefold() in m["snippet"].casefold() for m in matches))
                self.assertTrue(all(m["section"] == "S2" for m in matches))
                self.assertEqual(matches[0]["id"], identifier)
                self.assertEqual(matches[0]["parent"], "S2.F1")
                self.assertEqual(matches[0]["type"], "figure")
                self.assertEqual(paperread.find(self.data, term, after="S2.p2"), [])

    def test_latex_search_finds_reuse_that_omits_the_concept_name(self):
        words = paperread.find(self.data, "winding", after="S1.p1")
        symbols = paperread.find(self.data, r"\omega", after="S1.p1")
        self.assertEqual(words, [])
        self.assertEqual([hit["id"] for hit in symbols], ["S2.E1", "S2.p2"])


class SecondRoundCliTests(unittest.TestCase):
    def setUp(self):
        # Keep the entire isolated reader store inside the repository.
        self.temp = tempfile.TemporaryDirectory(prefix=".round2-test-", dir=ROOT)
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "private"
        self.paper_path = self.home / "papers" / PAPER
        self.paper_path.mkdir(parents=True)
        self.data = paperread.parse_html(HTML)
        self.data.update(
            arxiv_id="2610.27436", version="v1", source="arxiv_html",
            source_url="https://arxiv.org/html/" + PAPER,
            fetched_at="2026-11-03T00:00:00Z", authors=[],
        )
        self.save_paper()

    def save_paper(self):
        (self.paper_path / "paper.json").write_text(json.dumps(self.data), encoding="utf-8")

    def cli(self, *args, record=None):
        env = dict(os.environ, PAPERREAD_HOME=str(self.home), HOME=self.temp.name,
                   PYTHONDONTWRITEBYTECODE="1")
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            input=None if record is None else json.dumps(record),
            capture_output=True, text=True, env=env, timeout=10,
        )

    def success(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stderr, "")
        return result.stdout

    def error(self, result):
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("Traceback", result.stderr)
        error = json.loads(result.stdout)["error"]
        self.assertIsInstance(error, str)
        self.assertTrue(error)
        return error

    def test_text_is_plain_text_and_preserves_internal_and_document_order(self):
        output = self.success(self.cli("text", PAPER))
        with self.assertRaises(json.JSONDecodeError):
            json.loads(output)
        expected_order = [
            "ABSTRACT_ONLY", "1 Motivation", "S1.p1", "INTRO_ONLY",
            "2 Ordered argument", "S2.p1", "BEFORE_DISPLAY",
            r"\omega = n_1 - n_2", "AFTER_DISPLAY",
            "Figure 1: Parent comparison.", "S2.p2", "BETWEEN_VISUALS",
            "Table 1: Summary of the model.", "Q = 7",
            "2.1 Nested consequence", "S2.SS1.p1", "SUBSECTION_ONLY",
            "20 Unrelated section", "S20.p1", "S20_ONLY",
        ]
        positions = [output.index(fragment) for fragment in expected_order]
        self.assertEqual(positions, sorted(positions))
        for identifier in ("S2.E1", "S2.E2"):
            self.assertIn(identifier, output)
        for fragment in (r"\omega = n_1 - n_2", "Q = 7", "Figure 1: Parent comparison."):
            self.assertEqual(output.count(fragment), 1)
        self.assertNotIn("BIBLIOGRAPHY_ONLY", output)
        self.assertNotIn("MATHML_SENTINEL", output)

    def test_text_section_contains_descendants_but_not_prefix_neighbor(self):
        output = self.success(self.cli("text", PAPER, "--section", "S2"))
        for fragment in ("2 Ordered argument", "S2.p1", "S2.E1", "S2.F1",
                         "S2.T1", "S2.E2", "2.1 Nested consequence", "SUBSECTION_ONLY"):
            self.assertIn(fragment, output)
        for fragment in ("INTRO_ONLY", "ABSTRACT_ONLY", "S20_ONLY", "20 Unrelated section"):
            self.assertNotIn(fragment, output)
        subsection = self.success(self.cli("text", PAPER, "--section", "S2.SS1"))
        self.assertIn("SUBSECTION_ONLY", subsection)
        self.assertNotIn("BEFORE_DISPLAY", subsection)

    def test_unknown_text_section_returns_json_error(self):
        self.assertIn("S404", self.error(self.cli("text", PAPER, "--section", "S404")))

    def test_ref_resolves_number_printed_label_and_source_id(self):
        expected = self.data["references"][0]
        for identifier in ("26", "[26]", "bib.bib19"):
            with self.subTest(identifier=identifier):
                actual = json.loads(self.success(self.cli("ref", PAPER, identifier)))
                self.assertEqual(actual, expected)
        actual = json.loads(self.success(self.cli("ref", PAPER, "27")))
        self.assertEqual(actual, self.data["references"][1])

    def test_unknown_reference_returns_json_error(self):
        self.assertIn("999", self.error(self.cli("ref", PAPER, "999")))

    def test_pdf_only_text_and_ref_report_friendly_json_errors(self):
        self.data["source"] = "pdf_only"
        for key in ("sections", "paragraphs", "equations", "figures", "references"):
            self.data[key] = []
        self.save_paper()
        for args in (("text", PAPER), ("ref", PAPER, "26")):
            with self.subTest(args=args):
                self.assertRegex(self.error(self.cli(*args)), "PDF|pdf")

    def test_new_sentence_log_type_is_defaulted_and_persisted(self):
        entry = json.loads(self.success(self.cli(
            "log", "add", PAPER, "--json", "-",
            record={"where": "S2.p1.s1", "explained": ["invariant"]},
        )))
        self.assertEqual(entry["type"], "sentence")
        saved = json.loads((self.paper_path / "log.jsonl").read_text(encoding="utf-8"))
        self.assertEqual(saved["type"], "sentence")
        shown = json.loads(self.success(self.cli("log", "show", PAPER)))
        self.assertEqual(shown, [entry])

    def test_overview_log_has_null_location_and_keeps_explained_concepts(self):
        entry = json.loads(self.success(self.cli(
            "log", "add", PAPER, "--json", "-",
            record={"type": "overview", "explained": ["winding", "invariant"]},
        )))
        self.assertEqual(entry["type"], "overview")
        self.assertIsNone(entry["where"])
        self.assertEqual(entry["explained"], ["winding", "invariant"])
        shown = json.loads(self.success(self.cli("log", "show", PAPER)))
        self.assertEqual(shown, [entry])

    def test_old_log_is_read_as_sentence_without_rewriting_it(self):
        path = self.paper_path / "log.jsonl"
        original = json.dumps({"where": "S1.p1.s1", "explained": ["legacy concept"]}) + "\n"
        path.write_text(original, encoding="utf-8")
        entries = json.loads(self.success(self.cli("log", "show", PAPER)))
        self.assertEqual(entries[0]["type"], "sentence")
        self.assertEqual(entries[0]["explained"], ["legacy concept"])
        self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_last_zero_reads_all_logs_including_legacy_entries(self):
        path = self.paper_path / "log.jsonl"
        records = [{"where": "S1.p1.s1", "feedback": "feedback %d" % number} for number in range(12)]
        original = "".join(json.dumps(record) + "\n" for record in records)
        path.write_text(original, encoding="utf-8")
        default = json.loads(self.success(self.cli("log", "show", PAPER)))
        self.assertEqual(len(default), 10)
        all_entries = json.loads(self.success(self.cli("log", "show", PAPER, "--last", "0")))
        self.assertEqual([entry["feedback"] for entry in all_entries], [record["feedback"] for record in records])
        self.assertTrue(all(entry["type"] == "sentence" for entry in all_entries))
        self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_invalid_log_type_does_not_append_a_record(self):
        self.error(self.cli("log", "add", PAPER, "--json", "-", record={"type": "typo"}))
        path = self.paper_path / "log.jsonl"
        self.assertFalse(path.exists() and path.read_text(encoding="utf-8"))

    def test_overview_with_sentence_location_is_rejected_without_append(self):
        self.error(self.cli(
            "log", "add", PAPER, "--json", "-",
            record={"type": "overview", "where": "S2.p1.s1"},
        ))
        path = self.paper_path / "log.jsonl"
        self.assertFalse(path.exists() and path.read_text(encoding="utf-8"))

    def test_old_html_cache_is_upgraded_offline_without_changing_reader_files(self):
        self.data.pop("parser_version", None)
        self.data.pop("references", None)
        self.data["figures"][0]["caption"] = "OLD_MERGED_CAPTION"
        self.save_paper()
        (self.paper_path / "source.html").write_text(HTML, encoding="utf-8")
        protected = {
            self.home / "profile.json": '{"background": "Reader background", "areas": {}, "concepts": {}}\n',
            self.paper_path / "outline.md": "# Reader outline\nPersonal note.\n",
            self.paper_path / "log.jsonl": '{"where": "S1.p1.s1", "feedback": "Keep this."}\n',
        }
        for path, original in protected.items():
            path.write_text(original, encoding="utf-8")
        store = paperread.Store(self.home, client=NoNetworkClient())
        _, upgraded = store.load(PAPER, require_html=True)
        self.assertEqual(upgraded["parser_version"], 2)
        self.assertEqual(upgraded["references"][0]["label"], "[26]")
        self.assertEqual(upgraded["figures"][0]["caption"], "Figure 1: Parent comparison.")
        for key in ("arxiv_id", "version", "source", "source_url", "fetched_at", "authors"):
            self.assertEqual(upgraded[key], self.data[key])
        saved = json.loads((self.paper_path / "paper.json").read_text(encoding="utf-8"))
        self.assertEqual(saved, upgraded)
        for path, original in protected.items():
            self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_old_html_cache_without_source_requests_explicit_refresh(self):
        self.data.pop("parser_version", None)
        self.save_paper()
        store = paperread.Store(self.home, client=NoNetworkClient())
        with self.assertRaisesRegex(paperread.PaperError, r"fetch.*--refresh"):
            store.load(PAPER, require_html=True)


if __name__ == "__main__":
    unittest.main()
