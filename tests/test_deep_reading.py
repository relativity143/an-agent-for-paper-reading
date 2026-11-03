"""Offline regression checks for implementation.md section 15.

The paper cache uses the existing hand-authored HTML fixture. CLI checks invoke
the real parser and main entry point with a store that rejects all network use.
"""
from contextlib import redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills" / "paper-foundations" / "scripts" / "paperread.py"
SPEC = importlib.util.spec_from_file_location("paperread_deep_reading", SCRIPT)
paperread = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = paperread
SPEC.loader.exec_module(paperread)
HTML = (Path(__file__).parent / "fixtures" / "second_round_latexml.html").read_text(encoding="utf-8")
PAPER = "2610.27436v1"


class NoNetworkClient:
    def get(self, url):
        raise AssertionError("Unexpected network request: " + url)


class DeepReadingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix=".deep-test-", dir=ROOT)
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "private"
        self.paper_path = self.home / "papers" / PAPER
        self.paper_path.mkdir(parents=True)
        self.log_path = self.paper_path / "log.jsonl"
        data = paperread.parse_html(HTML)
        data.update(
            arxiv_id="2610.27436", version="v1", source="arxiv_html",
            source_url="https://arxiv.org/html/" + PAPER,
            fetched_at="2026-11-03T00:00:00Z", authors=[],
        )
        (self.paper_path / "paper.json").write_text(json.dumps(data), encoding="utf-8")
        self.store = paperread.Store(self.home, client=NoNetworkClient())

    def seed_logs(self, records):
        # Preserve noncanonical whitespace to detect even an innocent rewrite.
        text = "\n".join(json.dumps(record, ensure_ascii=False, indent=None) for record in records) + "\n"
        self.log_path.write_text(text, encoding="utf-8")
        return self.log_path.read_bytes()

    def cli(self, *args, record=None):
        output = io.StringIO()
        stdin = io.StringIO("" if record is None else json.dumps(record))
        with mock.patch.object(paperread, "Store", return_value=self.store), \
                mock.patch.object(sys, "stdin", stdin), redirect_stdout(output):
            status = paperread.main(list(args))
        return status, json.loads(output.getvalue())

    def shown(self, *args):
        status, result = self.cli("log", "show", PAPER, *args)
        self.assertEqual(status, 0, result)
        self.assertIsInstance(result, list)
        return result

    def test_deep_log_round_trip_preserves_range_segment_and_prior_foundations(self):
        record = {
            "type": "deep", "segment": 2, "where": "S2.SS1–S2.SS2",
            "uses": ["winding", "守恒律"], "explained": ["full derivation"],
            "next": "segment 3",
        }
        status, entry = self.cli("log", "add", PAPER, "--json", "-", record=record)
        self.assertEqual(status, 0, entry)
        for key, value in record.items():
            self.assertEqual(entry[key], value)
        self.assertTrue(entry["time"])
        self.assertEqual(self.shown("--type", "deep"), [entry])
        self.assertEqual(json.loads(self.log_path.read_text(encoding="utf-8")), entry)
        self.assertFalse((self.home / "profile.json").exists())

    def test_deep_uses_defaults_to_empty_and_accepts_explicit_empty_list(self):
        for extra in ({}, {"uses": []}):
            with self.subTest(extra=extra):
                entry = self.store.log_add(PAPER, {"type": "deep", "segment": 1, **extra})
                self.assertEqual(entry["uses"], [])
                self.assertEqual(entry["segment"], 1)
        self.assertEqual(len(self.store.log_show(PAPER)), 2)

    def test_deep_segment_requires_a_positive_json_integer_without_appending(self):
        original = self.seed_logs([{"where": "S1.p1.s1", "feedback": "existing history"}])
        cases = [
            {}, {"segment": None}, {"segment": True}, {"segment": False},
            {"segment": 0}, {"segment": -1}, {"segment": 1.0},
            {"segment": 1.5}, {"segment": "1"}, {"segment": []}, {"segment": {}},
        ]
        for fields in cases:
            with self.subTest(fields=fields):
                with self.assertRaisesRegex(paperread.PaperError, "segment"):
                    self.store.log_add(PAPER, {"type": "deep", "uses": [], **fields})
                self.assertEqual(self.log_path.read_bytes(), original)

    def test_deep_uses_rejects_non_arrays_and_non_string_members_without_appending(self):
        original = self.seed_logs([{"type": "overview", "where": None}])
        for uses in (None, "winding", 1, True, {}, ["winding", 1], [None], [False], [[]]):
            with self.subTest(uses=uses):
                with self.assertRaisesRegex(paperread.PaperError, "uses"):
                    self.store.log_add(PAPER, {"type": "deep", "segment": 1, "uses": uses})
                self.assertEqual(self.log_path.read_bytes(), original)

    def test_non_deep_logs_reject_segment_even_when_null_or_type_is_omitted(self):
        original = self.seed_logs([{"where": "S1.p1.s1", "feedback": "keep this"}])
        for type_fields in ({}, {"type": "sentence"}, {"type": "overview"}):
            for segment in (None, 1):
                with self.subTest(type_fields=type_fields, segment=segment):
                    with self.assertRaisesRegex(paperread.PaperError, "segment"):
                        self.store.log_add(PAPER, {**type_fields, "segment": segment})
                    self.assertEqual(self.log_path.read_bytes(), original)

    def test_default_sentence_log_still_persists_without_deep_fields(self):
        entry = self.store.log_add(PAPER, {"where": "S1.p1.s1", "explained": ["winding"]})
        self.assertEqual(entry["type"], "sentence")
        self.assertEqual(entry["where"], "S1.p1.s1")
        self.assertNotIn("segment", entry)
        self.assertEqual(self.store.log_show(PAPER), [entry])

    def test_same_segment_feedback_appends_without_changing_history_or_profile(self):
        profile = {
            "background": "physics", "areas": {},
            "concepts": {
                "winding": {"status": "explained", "updated": "2026-11-01"},
                "conservation": {"status": "unsure", "source": "still confused"},
            },
        }
        profile_path = self.home / "profile.json"
        profile_path.write_text(json.dumps(profile, indent=2) + "\n", encoding="utf-8")
        profile_before = profile_path.read_bytes()
        history = self.seed_logs([{"where": "S1.p1.s1", "explained": ["winding"]}])
        explanation = self.store.log_add(PAPER, {
            "type": "deep", "segment": 3, "where": "S2",
            "uses": ["winding", "conservation"], "explained": ["full proof"],
        })
        after_explanation = self.log_path.read_bytes()
        feedback = []
        for words in ("读顺了，继续", "卡在这一步的条件"):
            feedback.append(self.store.log_add(PAPER, {
                "type": "deep", "segment": 3,
                "uses": ["winding", "conservation"], "feedback": words,
            }))
        entries = self.store.log_show(PAPER, last=0)
        self.assertEqual(entries[1:], [explanation, *feedback])
        self.assertEqual([entry["segment"] for entry in entries[1:]], [3, 3, 3])
        self.assertEqual([entry["feedback"] for entry in entries[2:]], ["读顺了，继续", "卡在这一步的条件"])
        self.assertTrue(after_explanation.startswith(history))
        self.assertTrue(self.log_path.read_bytes().startswith(after_explanation))
        self.assertEqual(profile_path.read_bytes(), profile_before)

    def test_feedback_only_deep_log_accepts_same_segment_and_defaults_uses(self):
        first = self.store.log_add(PAPER, {"type": "deep", "segment": 1, "uses": ["winding"]})
        reply = self.store.log_add(PAPER, {"type": "deep", "segment": 1, "feedback": "继续"})
        self.assertEqual(reply["uses"], [])
        self.assertEqual(reply["feedback"], "继续")
        self.assertEqual(self.store.log_show(PAPER), [first, reply])

    def test_show_filters_before_last_and_zero_returns_all_matches_in_order(self):
        records = []
        for number in range(12):
            records.extend([
                {"feedback": "sentence %d" % number},
                {"type": "deep", "segment": number + 1, "feedback": "deep %d" % number},
                {"type": "overview", "feedback": "overview %d" % number},
            ])
        original = self.seed_logs(records)
        for record_type in ("sentence", "overview", "deep"):
            expected = [r["feedback"] for r in records if r.get("type", "sentence") == record_type]
            for last in (None, 1, 2, 0):
                with self.subTest(record_type=record_type, last=last):
                    arguments = ["--type", record_type]
                    if last is not None:
                        arguments.extend(["--last", str(last)])
                    entries = self.shown(*arguments)
                    selected = expected if last == 0 else expected[-(10 if last is None else last):]
                    self.assertEqual([entry["feedback"] for entry in entries], selected)
                    self.assertTrue(all(entry["type"] == record_type for entry in entries))
                    self.assertEqual(self.log_path.read_bytes(), original)

    def test_show_without_filter_retains_mixed_history_and_legacy_sentence_type(self):
        records = [
            {"where": "S1.p1.s1", "feedback": "legacy"},
            {"type": "deep", "segment": 1, "uses": [], "feedback": "deep"},
            {"type": "overview", "where": None, "feedback": "overview"},
            {"type": "sentence", "feedback": "explicit sentence"},
        ]
        original = self.seed_logs(records)
        entries = self.shown("--last", "0")
        self.assertEqual(entries, [{"type": "sentence", **record} for record in records])
        self.assertEqual(self.shown("--last", "2"), entries[-2:])
        self.assertEqual(self.log_path.read_bytes(), original)

    def test_type_filter_returns_empty_for_missing_log_and_no_matches(self):
        for record_type in ("sentence", "overview", "deep"):
            with self.subTest(record_type=record_type):
                self.assertEqual(self.shown("--type", record_type), [])
                self.assertFalse(self.log_path.exists())
        original = self.seed_logs([{"where": "S1.p1.s1"}])
        self.assertEqual(self.shown("--type", "deep", "--last", "0"), [])
        self.assertEqual(self.log_path.read_bytes(), original)

    def test_invalid_type_and_invalid_deep_input_return_json_cli_errors_without_append(self):
        original = self.seed_logs([{"where": "S1.p1.s1", "feedback": "preserve"}])
        cases = [
            (("log", "show", PAPER, "--type", "typo"), None),
            (("log", "add", PAPER, "--json", "-"), {"type": "typo"}),
            (("log", "add", PAPER, "--json", "-"), {"type": "deep", "segment": True}),
        ]
        for args, record in cases:
            with self.subTest(args=args, record=record):
                status, error = self.cli(*args, record=record)
                self.assertNotEqual(status, 0)
                self.assertIsInstance(error.get("error"), str)
                self.assertTrue(error["error"])
                self.assertEqual(self.log_path.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
