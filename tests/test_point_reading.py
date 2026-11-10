"""Offline regression checks for implementation.md section 18.

Reader data and CLI homes are temporary. The CLI subprocesses load a small
network guard so the examples can only exercise the synthetic cached paper.
"""
import copy
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills" / "paper-foundations" / "scripts" / "paperread.py"
TEMPLATE = ROOT / "skills" / "paper-foundations" / "references" / "output-template.md"
SPEC = importlib.util.spec_from_file_location("paperread_point_reading", SCRIPT)
paperread = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = paperread
SPEC.loader.exec_module(paperread)
PAPER = "2610.27436v1"


class NoNetworkClient:
    def get(self, url):
        raise AssertionError("Unexpected network request: " + url)


class PointReadingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="paperread-point-test-")
        self.addCleanup(self.temp.cleanup)
        self.home = (Path(self.temp.name) / "private").resolve()
        self.store = paperread.Store(self.home, client=NoNetworkClient())
        self.paper_path = self.home / "papers" / PAPER
        paperread.write_json(self.paper_path / "paper.json", {"title": "Synthetic point fixture"})
        self.log_path = self.paper_path / "log.jsonl"
        self.profile_path = self.home / "profile.json"
        guard = mock.patch.object(paperread, "urlopen", side_effect=AssertionError("Unexpected network access"))
        guard.start()
        self.addCleanup(guard.stop)
        self.guard_path = Path(self.temp.name) / "network_guard"
        self.guard_path.mkdir()
        (self.guard_path / "sitecustomize.py").write_text(
            "import sys\n"
            "def reject_network(event, args):\n"
            "    if event in ('socket.connect', 'socket.getaddrinfo'):\n"
            "        raise AssertionError('Unexpected network access')\n"
            "sys.addaudithook(reject_network)\n", encoding="utf-8")

    def cli(self, *args, record=None):
        env = dict(os.environ, PAPERREAD_HOME=str(self.home),
                   PYTHONPATH=str(self.guard_path), PYTHONDONTWRITEBYTECODE="1")
        result = subprocess.run([sys.executable, "-B", str(SCRIPT), *args], cwd=ROOT,
                                env=env, input=None if record is None else json.dumps(record, ensure_ascii=False),
                                text=True, capture_output=True, timeout=15)
        self.assertNotIn("Traceback", result.stderr, result.stderr)
        return result.returncode, json.loads(result.stdout)

    def seed_logs(self, records):
        # Deliberately noncanonical whitespace detects accidental migrations.
        text = "\n" + "\n\n".join(json.dumps(record, ensure_ascii=False, separators=(", ", " : "))
                                       for record in records) + "\n"
        self.log_path.write_text(text, encoding="utf-8")
        return self.log_path.read_bytes()

    def seed_profile(self):
        data = {"background": "Synthetic background", "areas": {}, "concepts": {
            "绕数": {"status": "explained", "paper": PAPER, "where": "S2.p1"},
            "边界条件": {"status": "unsure", "paper": PAPER, "where": "S2.p2"},
            "哈密顿量": {"status": "known", "paper": PAPER, "source": "我看懂了"},
        }}
        self.profile_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return self.profile_path.read_bytes()

    def test_sentence_accepts_optional_point_and_step_without_mutating_input(self):
        cases = ({}, {"point": "实空间绕数"}, {"point": ""}, {"step": None},
                 {"step": 1}, {"step": 10 ** 30}, {"point": "实空间绕数", "step": 3})
        saved = []
        for explicit_type in (False, True):
            for fields in cases:
                with self.subTest(explicit_type=explicit_type, fields=fields):
                    record = {"where": "S2.SS2.p1–S2.E4", "quote": "", **fields}
                    if explicit_type:
                        record["type"] = "sentence"
                    before = copy.deepcopy(record)
                    entry = self.store.log_add(PAPER, record)
                    self.assertEqual(record, before)
                    self.assertEqual(entry["type"], "sentence")
                    for key in ("point", "step"):
                        if key in fields:
                            self.assertEqual(entry[key], fields[key])
                        else:
                            self.assertNotIn(key, entry)
                    saved.append(entry)
        self.assertEqual(self.store.log_show(PAPER, last=0), saved)

    def test_sentence_rejects_nonstring_point_without_appending(self):
        original = self.seed_logs([{"where": "S1.p1.s1", "feedback": "Preserve this history"}])
        for explicit_type in (False, True):
            for point in (None, True, False, 0, 1, 1.5, [], {}, ["绕数"]):
                with self.subTest(explicit_type=explicit_type, point=point):
                    record = {"point": point}
                    if explicit_type:
                        record["type"] = "sentence"
                    with self.assertRaises(paperread.PaperError):
                        self.store.log_add(PAPER, record)
                    self.assertEqual(self.log_path.read_bytes(), original)

    def test_sentence_rejects_nonpositive_and_noninteger_step_without_appending(self):
        original = self.seed_logs([{"type": "sentence", "where": "S1.p1.s1"}])
        for explicit_type in (False, True):
            for step in (True, False, 0, -1, 1.0, 1.5, "1", "0", "", [], {}, [1]):
                with self.subTest(explicit_type=explicit_type, step=step):
                    record = {"step": step}
                    if explicit_type:
                        record["type"] = "sentence"
                    with self.assertRaises(paperread.PaperError):
                        self.store.log_add(PAPER, record)
                    self.assertEqual(self.log_path.read_bytes(), original)

    def test_other_record_types_reject_step_including_null(self):
        records = (
            {"type": "overview", "where": None},
            {"type": "deep", "segment": 1, "where": "S2–S3"},
            {"type": "field", "kind": "position", "items": [{
                "label": "[1]", "title": "Synthetic source", "role": "foundational",
                "checked": "abstract", "link": "https://example.org/source"}]},
            {"type": "recap", "where": None},
        )
        for record in records:
            # First prove that step is the only invalid part of each fixture.
            self.store.log_add(PAPER, record)
            original = self.log_path.read_bytes()
            for step in (None, 1, True, 0, "1", {}):
                with self.subTest(record_type=record["type"], step=step):
                    with self.assertRaises(paperread.PaperError):
                        self.store.log_add(PAPER, dict(record, step=step))
                    self.assertEqual(self.log_path.read_bytes(), original)

    def test_old_records_read_without_fabricated_fields_or_byte_changes(self):
        legacy = [{"where": "S1.p1.s1", "quote": "A synthetic legacy sentence."},
                  {"type": "sentence", "where": "p3", "explained": ["绕数"]},
                  {"type": "overview", "where": None},
                  {"type": "deep", "segment": 1, "where": "S2"}]
        original = self.seed_logs(legacy)
        expected = [{"type": "sentence", **record} for record in legacy]
        self.assertEqual(self.store.log_show(PAPER, last=0), expected)
        self.assertEqual(self.store.log_show(PAPER, last=0, record_type="sentence"), expected[:2])
        status, records = self.cli("log", "show", PAPER, "--last", "0")
        self.assertEqual((status, records), (0, expected))
        for record in records:
            self.assertNotIn("point", record)
            self.assertNotIn("step", record)
        self.assertEqual(self.log_path.read_bytes(), original)

    def test_appending_a_point_preserves_existing_legacy_bytes_and_profile(self):
        original = self.seed_logs([{"where": "S1.p1.s1", "feedback": "Existing feedback"}])
        profile = self.seed_profile()
        entry = self.store.log_add(PAPER, {"point": "实空间绕数", "step": 2, "where": "S2.p1–S2.E4"})
        appended = self.log_path.read_bytes()
        self.assertEqual(appended[:len(original)], original)
        self.assertEqual(json.loads(appended[len(original):]), entry)
        self.assertEqual(self.profile_path.read_bytes(), profile)

    def test_real_cli_roundtrips_null_and_integer_steps_ranges_and_empty_quotes(self):
        profile = self.seed_profile()
        records = [
            {"type": "sentence", "point": "实空间绕数", "step": 2,
             "where": "S2.SS2.p1–S2.E4", "quote": "", "explained": ["迹"]},
            {"type": "sentence", "point": "补充背景", "step": None,
             "where": "p4–p5", "quote": "", "feedback": "继续"},
            {"point": "", "where": "S3.F1", "quote": ""},
        ]
        saved = []
        for index, record in enumerate(records):
            with self.subTest(record=record):
                if index == 0:
                    # Exercise the JSON argument as well as the stdin path.
                    status, entry = self.cli("log", "add", PAPER, "--json", json.dumps(record))
                else:
                    status, entry = self.cli("log", "add", PAPER, "--json", "-", record=record)
                self.assertEqual(status, 0, entry)
                self.assertEqual(entry["type"], "sentence")
                for key, value in record.items():
                    self.assertEqual(entry[key], value)
                saved.append(entry)
        self.assertIsNone(saved[1]["step"])
        self.assertNotIn("step", saved[2])
        self.assertEqual(self.cli("log", "show", PAPER, "--type", "sentence", "--last", "0"), (0, saved))
        self.assertEqual(self.profile_path.read_bytes(), profile)

    def test_real_cli_invalid_point_or_step_returns_json_error_without_append(self):
        original = self.seed_logs([{"where": "S1.p1.s1"}])
        for fields in ({"point": None}, {"step": True}, {"step": 1.0}, {"step": "1"},
                       {"step": 0}, {"type": "recap", "step": None}):
            with self.subTest(fields=fields):
                status, result = self.cli("log", "add", PAPER, "--json", "-", record=fields)
                self.assertEqual(status, 1)
                self.assertIsInstance(result.get("error"), str)
                self.assertTrue(result["error"])
                self.assertEqual(self.log_path.read_bytes(), original)

    def test_appending_points_does_not_create_a_profile(self):
        self.store.log_add(PAPER, {"point": "绕数", "step": 1})
        status, result = self.cli("log", "add", PAPER, "--json", "-", record={"point": "背景", "step": None})
        self.assertEqual(status, 0, result)
        self.assertFalse(self.profile_path.exists())

    def test_output_template_json_examples_are_accepted_by_real_cli(self):
        blocks = re.findall(r"^```json[ \t]*\n(.*?)^```[ \t]*$",
                            TEMPLATE.read_text(encoding="utf-8"), flags=re.MULTILINE | re.DOTALL)
        self.assertTrue(blocks, "The output template must contain executable JSON examples")
        records = [json.loads(block) for block in blocks]
        self.assertTrue(any(record.get("type", "sentence") == "sentence"
                            and "point" in record and "step" in record for record in records),
                        "The template must demonstrate the new point and step fields")
        saved = []
        for index, record in enumerate(records, 1):
            with self.subTest(example=index, record_type=record.get("type", "sentence")):
                status, entry = self.cli("log", "add", PAPER, "--json", "-", record=record)
                self.assertEqual(status, 0, entry)
                for key, value in record.items():
                    self.assertEqual(entry[key], value)
                saved.append(entry)
        self.assertEqual(self.cli("log", "show", PAPER, "--last", "0"), (0, saved))
        self.assertFalse(self.profile_path.exists())


if __name__ == "__main__":
    unittest.main()
