"""Offline regression checks for implementation.md section 17.

All reader data is synthetic and isolated in temporary storage. These checks
exercise the real CLI parser, Store operations, and on-disk append behavior.
"""
from contextlib import redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills" / "paper-foundations" / "scripts" / "paperread.py"
SPEC = importlib.util.spec_from_file_location("paperread_recap_reading", SCRIPT)
paperread = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = paperread
SPEC.loader.exec_module(paperread)
PAPER = "2610.27436v1"


class NoNetworkClient:
    def get(self, url):
        raise AssertionError("Unexpected network request: " + url)


class RecapReadingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="paperread-recap-test-")
        self.addCleanup(self.temp.cleanup)
        self.home = (Path(self.temp.name) / "private").resolve()
        self.store = paperread.Store(self.home, client=NoNetworkClient())
        self.paper_path = self.seed_paper(PAPER)
        self.log_path = self.paper_path / "log.jsonl"
        guard = mock.patch.object(paperread, "urlopen", side_effect=AssertionError("Unexpected network access"))
        guard.start()
        self.addCleanup(guard.stop)

    def seed_paper(self, name, source="arxiv_html", **changes):
        path = self.home / "papers" / name
        data = {"title": "Synthetic recap fixture", "source": source,
                "parser_version": paperread.PARSER_VERSION,
                "paragraphs": [], "equations": [], "figures": [], "references": []}
        data.update(changes)
        paperread.write_json(path / "paper.json", data)
        return path

    def cli(self, *args, record=None):
        output = io.StringIO()
        stdin = io.StringIO("" if record is None else json.dumps(record))
        with mock.patch.object(paperread, "Store", return_value=self.store), \
                mock.patch.object(sys, "stdin", stdin), redirect_stdout(output):
            status = paperread.main(list(args))
        return status, json.loads(output.getvalue())

    def seed_logs(self, records):
        # Keep unusual whitespace: reads and failed writes must preserve bytes.
        text = "\n".join(json.dumps(record, ensure_ascii=False, separators=(", ", " : "))
                         for record in records) + "\n"
        self.log_path.write_text(text, encoding="utf-8")
        return self.log_path.read_bytes()

    def seed_profile(self):
        data = {"background": "Synthetic physics background", "areas": {}, "concepts": {
            "winding": {"status": "explained", "paper": PAPER, "where": "S2.p1"},
            "same paper confirmed": {"status": "known", "paper": "2610.27436v3",
                                     "where": "S3.p1", "source": "读顺了"},
            "URL provenance": {"status": "unsure", "paper": "https://arxiv.org/pdf/2610.27436v2.pdf",
                               "where": "p3"},
            "another paper": {"status": "known", "paper": "1002.3895v2", "where": "S1"},
            "old cache ID": {"status": "explained", "paper": "cond-mat_0506581v1", "where": "S1"},
            "old URL ID": {"status": "known", "paper": "https://arxiv.org/abs/cond-mat/0506581v2"},
            "local paper": {"status": "explained", "paper": "notes-12345678", "where": "p2"},
            "other local paper": {"status": "unsure", "paper": "notes-abcdefgh", "where": "p3"},
            "missing provenance": {"status": "known", "source": "Prior knowledge"},
            "null provenance": {"status": "explained", "paper": None, "where": None},
        }}
        path = self.home / "profile.json"
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return path.read_bytes()

    def test_glossary_path_reports_absence_without_creating_the_file(self):
        cache_before = (self.paper_path / "paper.json").read_bytes()
        status, result = self.cli("glossary-path", PAPER)
        self.assertEqual(status, 0, result)
        self.assertEqual(result, {"path": str(self.paper_path / "glossary.md"), "exists": False})
        self.assertFalse((self.paper_path / "glossary.md").exists())
        self.assertEqual((self.paper_path / "paper.json").read_bytes(), cache_before)

    def test_glossary_path_preserves_existing_host_authored_content(self):
        path = self.paper_path / "glossary.md"
        content = "# 术语表\n\n| 术语 | 本文中的意思 |\n| Γ | 对称性算符 |\n"
        path.write_text(content, encoding="utf-8")
        status, result = self.cli("glossary-path", "https://arxiv.org/abs/" + PAPER)
        self.assertEqual((status, result), (0, {"path": str(path), "exists": True}))
        self.assertEqual(path.read_text(encoding="utf-8"), content)

    def test_glossary_path_supports_pdf_and_local_cache_names(self):
        for name, source in (("1002.3895v2", "pdf_only"), ("notes-12345678", "local_pdf")):
            with self.subTest(source=source):
                path = self.seed_paper(name, source=source)
                (path / "paper.pdf").write_bytes(b"%PDF-1.4\nSynthetic offline fixture\n")
                status, result = self.cli("glossary-path", name)
                self.assertEqual((status, result), (0, {"path": str(path / "glossary.md"), "exists": False}))
                self.assertFalse((path / "glossary.md").exists())

    def test_glossary_path_upgrades_old_html_locally_and_preserves_reader_files(self):
        html = (ROOT / "tests" / "fixtures" / "second_round_latexml.html").read_text(encoding="utf-8")
        profile_before = self.seed_profile()
        history = self.seed_logs([{"where": "S1.p1.s1", "feedback": "Existing history"}])
        glossary = self.paper_path / "glossary.md"
        glossary.write_text("# Existing host-authored glossary\n", encoding="utf-8")
        glossary_before = glossary.read_bytes()
        for source in ("arxiv_html", "ar5iv"):
            with self.subTest(source=source):
                path = self.seed_paper(PAPER, source=source, parser_version=0)
                (path / "source.html").write_text(html, encoding="utf-8")
                status, result = self.cli("glossary-path", PAPER)
                self.assertEqual((status, result), (0, {"path": str(glossary), "exists": True}))
                upgraded = json.loads((path / "paper.json").read_text(encoding="utf-8"))
                self.assertEqual(upgraded["parser_version"], paperread.PARSER_VERSION)
                self.assertTrue(upgraded["paragraphs"])
                self.assertEqual(glossary.read_bytes(), glossary_before)
                self.assertEqual(self.log_path.read_bytes(), history)
                self.assertEqual((self.home / "profile.json").read_bytes(), profile_before)

    def test_glossary_path_old_html_without_source_matches_outline_refresh_error(self):
        self.seed_paper(PAPER, parser_version=0)
        before = (self.paper_path / "paper.json").read_bytes()
        for command in ("glossary-path", "outline-path"):
            with self.subTest(command=command):
                status, error = self.cli(command, PAPER)
                self.assertEqual(status, 1)
                self.assertIn("refresh", error["error"])
                self.assertEqual((self.paper_path / "paper.json").read_bytes(), before)
        self.assertFalse((self.paper_path / "glossary.md").exists())

    def test_glossary_path_uses_latest_cached_version_without_network(self):
        newer = self.seed_paper("2610.27436v12")
        self.seed_paper("2610.27436v2")
        self.assertEqual(self.cli("glossary-path", "2610.27436"),
                         (0, {"path": str(newer / "glossary.md"), "exists": False}))

    def test_glossary_path_unknown_and_invalid_papers_return_json_errors(self):
        for paper in ("9999.12345v1", "missing-local-cache", "../../profile.json"):
            with self.subTest(paper=paper):
                status, result = self.cli("glossary-path", paper)
                self.assertEqual(status, 1)
                self.assertIsInstance(result.get("error"), str)
                self.assertTrue(result["error"])
        self.assertEqual([p.name for p in self.store.papers.iterdir()], [PAPER])

    def test_profile_paper_filter_unifies_versions_and_urls_and_preserves_profile(self):
        before = self.seed_profile()
        for value in (PAPER, "2610.27436", "2610.27436v99", "arXiv:2610.27436v3",
                      "https://arxiv.org/abs/2610.27436v8", "https://arxiv.org/pdf/2610.27436v2.pdf",
                      "https://arxiv.org/html/2610.27436v1"):
            with self.subTest(value=value):
                status, records = self.cli("profile", "concepts", "--paper", value)
                self.assertEqual(status, 0, records)
                self.assertEqual([r["concept"] for r in records],
                                 ["winding", "same paper confirmed", "URL provenance"])
                self.assertEqual(records[0]["paper"], PAPER)
                self.assertEqual(records[0]["where"], "S2.p1")
                self.assertEqual(records[1]["source"], "读顺了")
        self.assertEqual((self.home / "profile.json").read_bytes(), before)

    def test_profile_paper_filter_combines_with_each_status_in_store_and_cli(self):
        self.seed_profile()
        expected = {"known": "same paper confirmed", "explained": "winding", "unsure": "URL provenance"}
        for status, concept in expected.items():
            with self.subTest(status=status):
                direct = self.store.profile_concepts(status=status, paper="2610.27436v4")
                code, records = self.cli("profile", "concepts", "--paper", PAPER, "--status", status)
                self.assertEqual((code, records), (0, direct))
                self.assertEqual([r["concept"] for r in records], [concept])

    def test_profile_paper_filter_normalizes_old_identifiers_without_a_cache(self):
        self.seed_profile()
        for value in ("cond-mat/0506581", "cond-mat/0506581v3", "cond-mat_0506581v7",
                      "https://arxiv.org/pdf/cond-mat/0506581v2.pdf"):
            with self.subTest(value=value):
                records = self.store.profile_concepts(paper=value)
                self.assertEqual([r["concept"] for r in records], ["old cache ID", "old URL ID"])
        self.assertFalse((self.store.papers / "cond-mat_0506581v1").exists())

    def test_profile_paper_filter_matches_local_cache_name_exactly(self):
        self.seed_profile()
        for paper, expected in (("notes-12345678", ["local paper"]),
                                ("notes-abcdefgh", ["other local paper"]), ("notes", [])):
            with self.subTest(paper=paper):
                status, records = self.cli("profile", "concepts", "--paper", paper)
                self.assertEqual(status, 0, records)
                self.assertEqual([r["concept"] for r in records], expected)

    def test_profile_paper_filter_omits_missing_or_null_provenance(self):
        self.seed_profile()
        records = self.store.profile_concepts(paper=PAPER)
        self.assertTrue(all(r["paper"] is not None for r in records))
        self.assertNotIn("missing provenance", [r["concept"] for r in records])
        self.assertNotIn("null provenance", [r["concept"] for r in records])
        all_records = self.store.profile_concepts()
        self.assertEqual(len(all_records), 10)
        self.assertIsNone(all_records[-2]["paper"])
        self.assertIsNone(all_records[-2]["where"])

    def test_profile_mutually_exclusive_filters_fail_in_store_and_cli(self):
        before = self.seed_profile()
        for excluded in (PAPER, "1002.3895v2"):
            with self.subTest(excluded=excluded):
                with self.assertRaises(paperread.PaperError):
                    self.store.profile_concepts(exclude_paper=excluded, paper=PAPER)
                status, error = self.cli("profile", "concepts", "--paper", PAPER, "--exclude-paper", excluded)
                self.assertEqual(status, 1)
                self.assertIsInstance(error.get("error"), str)
        self.assertEqual((self.home / "profile.json").read_bytes(), before)

    def test_profile_empty_and_unmatched_filters_do_not_create_reader_data(self):
        for paper in (PAPER, "9999.12345v1", "notes-12345678"):
            with self.subTest(paper=paper):
                self.assertEqual(self.store.profile_concepts(paper=paper), [])
                self.assertEqual(self.cli("profile", "concepts", "--paper", paper), (0, []))
        self.assertFalse((self.home / "profile.json").exists())

    def test_profile_invalid_paper_or_status_returns_json_error(self):
        for options in (("--paper", "../../profile.json"), ("--paper", "https://example.org/paper"),
                        ("--paper", PAPER, "--status", "mastered")):
            with self.subTest(options=options):
                status, error = self.cli("profile", "concepts", *options)
                self.assertEqual(status, 1)
                self.assertIsInstance(error.get("error"), str)

    def test_recap_roundtrip_preserves_concepts_terms_and_feedback(self):
        record = {"type": "recap", "where": None, "covered": ["绕数", "守恒律"],
                  "terms": ["Γ", "Bloch 哈密顿量"], "feedback": "绕数还不太确定"}
        status, entry = self.cli("log", "add", PAPER, "--json", "-", record=record)
        self.assertEqual(status, 0, entry)
        for key, value in record.items():
            self.assertEqual(entry[key], value)
        self.assertTrue(entry["time"])
        self.assertNotIn("segment", entry)
        self.assertEqual(self.cli("log", "show", PAPER, "--type", "recap"), (0, [entry]))
        self.assertEqual(json.loads(self.log_path.read_text(encoding="utf-8")), entry)

    def test_recap_missing_arrays_default_independently_to_empty_lists(self):
        for extra in ({}, {"covered": []}, {"terms": []}, {"covered": ["winding"]}, {"terms": ["Γ"]}):
            with self.subTest(extra=extra):
                entry = self.store.log_add(PAPER, {"type": "recap", **extra})
                self.assertIsNone(entry["where"])
                self.assertEqual(entry["covered"], extra.get("covered", []))
                self.assertEqual(entry["terms"], extra.get("terms", []))

    def test_recap_arrays_reject_wrong_types_and_members_without_appending(self):
        before = self.seed_logs([{"where": "S1.p1.s1", "feedback": "Existing sentence"}])
        for field in ("covered", "terms"):
            for value in (None, "winding", 1, 1.5, True, False, {}, ["winding", 1], [None], [False], [[]], [{}]):
                with self.subTest(field=field, value=value):
                    with self.assertRaisesRegex(paperread.PaperError, field):
                        self.store.log_add(PAPER, {"type": "recap", field: value})
                    self.assertEqual(self.log_path.read_bytes(), before)

    def test_recap_where_must_be_null_and_segment_must_be_absent(self):
        before = self.seed_logs([{"type": "overview", "where": None}])
        invalid = [{"where": value} for value in ("S1", "", 0, False, [], {})]
        invalid += [{"segment": value} for value in (None, 1, True, "1")]
        for fields in invalid:
            with self.subTest(fields=fields):
                with self.assertRaises(paperread.PaperError):
                    self.store.log_add(PAPER, {"type": "recap", **fields})
                self.assertEqual(self.log_path.read_bytes(), before)

    def test_recap_and_reader_reply_append_without_changing_profile_or_history(self):
        profile_before = self.seed_profile()
        history = self.seed_logs([{"where": "S1.p1.s1", "explained": ["winding"]}])
        first = self.store.log_add(PAPER, {"type": "recap", "covered": ["winding"], "terms": ["Γ"],
                                          "explained": ["A recap must not mark this concept"]})
        after_first = self.log_path.read_bytes()
        reply = self.store.log_add(PAPER, {"type": "recap", "feedback": "这里还有点虚"})
        self.assertTrue(after_first.startswith(history))
        self.assertTrue(self.log_path.read_bytes().startswith(after_first))
        self.assertEqual(self.store.log_show(PAPER, record_type="recap", last=0), [first, reply])
        self.assertEqual((self.home / "profile.json").read_bytes(), profile_before)

    def test_recap_append_does_not_create_a_profile(self):
        self.store.log_add(PAPER, {"type": "recap", "covered": ["winding"], "terms": ["Γ"]})
        self.assertFalse((self.home / "profile.json").exists())

    def test_recap_show_filters_before_last_and_preserves_legacy_log_bytes(self):
        records = []
        for number in range(12):
            records.extend([{"type": "recap", "where": None, "feedback": "recap %d" % number},
                            {"where": "S1.p1.s1", "feedback": "legacy sentence %d" % number},
                            {"type": "deep", "segment": number + 1, "feedback": "deep %d" % number}])
        before = self.seed_logs(records)
        expected = [record for record in records if record.get("type") == "recap"]
        for last in (None, 1, 3, 0):
            with self.subTest(last=last):
                args = () if last is None else ("--last", str(last))
                status, shown = self.cli("log", "show", PAPER, "--type", "recap", *args)
                self.assertEqual(status, 0, shown)
                self.assertEqual(shown, expected if last == 0 else expected[-(10 if last is None else last):])
                self.assertEqual(self.log_path.read_bytes(), before)
        status, sentences = self.cli("log", "show", PAPER, "--type", "sentence", "--last", "0")
        self.assertEqual(status, 0, sentences)
        self.assertEqual(len(sentences), 12)
        self.assertTrue(all(r["type"] == "sentence" for r in sentences))
        self.assertEqual(self.log_path.read_bytes(), before)

    def test_recap_show_empty_results_do_not_create_or_modify_history(self):
        self.assertEqual(self.cli("log", "show", PAPER, "--type", "recap"), (0, []))
        self.assertFalse(self.log_path.exists())
        before = self.seed_logs([{"where": "S1.p1.s1"}])
        self.assertEqual(self.cli("log", "show", PAPER, "--type", "recap", "--last", "0"), (0, []))
        self.assertEqual(self.log_path.read_bytes(), before)

    def test_invalid_recap_cli_input_returns_json_error_and_preserves_log(self):
        before = self.seed_logs([{"where": "S1.p1.s1"}])
        for fields in ({"where": "S1"}, {"segment": None}, {"covered": None}, {"terms": [1]}):
            with self.subTest(fields=fields):
                status, error = self.cli("log", "add", PAPER, "--json", "-", record={"type": "recap", **fields})
                self.assertEqual(status, 1)
                self.assertIsInstance(error.get("error"), str)
                self.assertTrue(error["error"])
                self.assertEqual(self.log_path.read_bytes(), before)

    def test_real_cli_process_exit_codes_and_json_are_usable(self):
        env = dict(os.environ, PAPERREAD_HOME=str(self.home), PYTHONDONTWRITEBYTECODE="1")
        cases = [(("glossary-path", PAPER), 0),
                 (("log", "add", PAPER, "--json", '{"type":"recap","terms":["Γ"]}'), 0),
                 (("log", "show", PAPER, "--type", "recap"), 0),
                 (("profile", "concepts", "--paper", PAPER, "--exclude-paper", PAPER), 1),
                 (("log", "add", PAPER, "--json", '{"type":"recap","terms":null}'), 1),
                 (("glossary-path", "9999.12345v1"), 1)]
        for args, expected_code in cases:
            with self.subTest(args=args):
                result = subprocess.run([sys.executable, str(SCRIPT), *args], cwd=ROOT,
                                        env=env, text=True, capture_output=True, timeout=15)
                self.assertEqual(result.returncode, expected_code, result.stdout + result.stderr)
                value = json.loads(result.stdout)
                if expected_code:
                    self.assertIsInstance(value.get("error"), str)
                self.assertNotIn("Traceback", result.stderr)
        self.assertFalse((self.home / "profile.json").exists())


if __name__ == "__main__":
    unittest.main()
