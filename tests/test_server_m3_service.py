"""Map persistence stays independent of reading state and ongoing generation."""
from concurrent.futures import ThreadPoolExecutor
import json
import threading
import unittest
from unittest.mock import patch

import test_server_service as fixtures
from server.service import Application, core

PAPER = fixtures.PAPER


def layout(version=0):
    return {"version": version, "nodes": [{"name": "A term not in the record", "x": 1440, "y": 0}],
            "notes": [{"id": "n-1", "x": 0, "y": 900, "text": "Check this convention later."}]}


class MapServiceTests(unittest.TestCase):
    setUp = fixtures.ServiceTests.setUp

    def test_empty_read_and_empty_put_do_not_create_files(self):
        before = {p.name: p.read_bytes() for p in self.path.iterdir() if p.is_file()}
        empty = {"version": 0, "nodes": [], "notes": []}
        self.assertEqual(self.app.map_layout(PAPER), empty)
        self.assertEqual(self.app.update_map_layout(PAPER, empty), empty)
        after = {p.name: p.read_bytes() for p in self.path.iterdir() if p.is_file()}
        self.assertEqual(after, before)
        self.assertFalse((self.path / ".agent.lock").exists())

    def test_round_trip_restart_noop_and_clear_keep_monotonic_version(self):
        body = layout()
        saved = self.app.update_map_layout(PAPER, body)
        self.assertEqual(saved, dict(body, version=1))
        self.assertEqual(body["version"], 0)
        other = Application(self.store, fixtures.FakeBackend())
        self.addCleanup(other.close)
        self.assertEqual(other.map_layout(PAPER), saved)
        with patch("server.service.core.write_json", side_effect=AssertionError("No unchanged writes")):
            self.assertEqual(other.update_map_layout(PAPER, saved), saved)
        self.assertEqual(other.update_map_layout(PAPER, {"version": 1, "nodes": [], "notes": []}),
                         {"version": 2, "nodes": [], "notes": []})
        self.assertEqual(self.app.map_layout(PAPER), {"version": 2, "nodes": [], "notes": []})

    def test_stale_put_keeps_exact_existing_bytes(self):
        self.app.update_map_layout(PAPER, layout())
        before = (self.path / "map-layout.json").read_bytes()
        with self.assertRaisesRegex(RuntimeError, "重新读取"):
            self.app.update_map_layout(PAPER, layout())
        self.assertEqual((self.path / "map-layout.json").read_bytes(), before)

    def test_invalid_updates_do_not_change_existing_layout(self):
        self.app.update_map_layout(PAPER, layout())
        invalid = [None, [], {}, dict(layout(1), unexpected=True)]
        for value in (-1, True, 1.0, "1"):
            invalid.append(layout(value))
        for key, value in (("x", -1), ("x", 1441), ("y", 901), ("y", True), ("x", 1.5),
                           ("name", ""), ("name", "x" * 81), ("extra", "forbidden")):
            body = layout(1)
            body["nodes"][0][key] = value
            invalid.append(body)
        for key, value in (("id", "with spaces"), ("id", "x" * 41), ("text", " \n "),
                           ("text", "x" * 301), ("x", None), ("y", -1), ("extra", 0)):
            body = layout(1)
            body["notes"][0][key] = value
            invalid.append(body)
        invalid.extend([
            {"version": 1, "nodes": [{"name": name, "x": 0, "y": 0} for name in ("Term", " term ")], "notes": []},
            {"version": 1, "nodes": [{"name": str(i), "x": 0, "y": 0} for i in range(401)], "notes": []},
            {"version": 1, "nodes": [], "notes": [dict(layout()["notes"][0]) for _ in range(2)]},
            {"version": 1, "nodes": [], "notes": [{"id": str(i), "x": 0, "y": 0, "text": "t"} for i in range(61)]},
        ])
        before = (self.path / "map-layout.json").read_bytes()
        for index, body in enumerate(invalid):
            with self.subTest(case=index), self.assertRaises(ValueError):
                self.app.update_map_layout(PAPER, body)
            self.assertEqual((self.path / "map-layout.json").read_bytes(), before)

    def test_corrupt_saved_json_or_shape_is_preserved_by_get_and_put(self):
        target = self.path / "map-layout.json"
        for raw in (b"{broken", b"null", b'{"version":false,"nodes":[],"notes":[]}',
                    b'{"version":1,"nodes":[],"notes":[],"unexpected":0}'):
            target.write_bytes(raw)
            for action in (lambda: self.app.map_layout(PAPER), lambda: self.app.update_map_layout(PAPER, layout())):
                with self.subTest(raw=raw), self.assertRaisesRegex(ValueError, "保留原文件"):
                    action()
                self.assertEqual(target.read_bytes(), raw)

    def test_symlink_layout_is_rejected_without_touching_its_target(self):
        outside = self.home / "private.json"
        outside.write_text(json.dumps(layout()))
        before = outside.read_bytes()
        (self.path / "map-layout.json").symlink_to(outside)
        with self.assertRaises(ValueError):
            self.app.map_layout(PAPER)
        with self.assertRaises(ValueError):
            self.app.update_map_layout(PAPER, layout())
        self.assertTrue((self.path / "map-layout.json").is_symlink())
        self.assertEqual(outside.read_bytes(), before)

    def test_map_does_not_access_records_profiles_or_logs(self):
        with patch.object(self.app, "_state", side_effect=AssertionError("No reading-state access")), \
                patch.object(self.store, "profile", side_effect=AssertionError("No profile")), \
                patch.object(self.store, "log_show", side_effect=AssertionError("No logs")), \
                patch.object(self.store, "log_add", side_effect=AssertionError("No logs")), \
                patch.object(self.store, "update_profile", side_effect=AssertionError("No profile writes")):
            self.app.update_map_layout(PAPER, layout())
            self.assertEqual(self.app.map_layout(PAPER)["version"], 1)
        self.assertEqual(self.app.record(PAPER), {"version": 0, "concepts": []})
        self.assertFalse((self.path / "agent-state.json").exists())
        self.assertEqual(self.backend.prompts, [])

    def test_map_save_leaves_history_record_prompt_and_legacy_files_unchanged(self):
        task = self.app.explain(PAPER, fixtures.reading_request(app=self.app))
        result = [e for e in self.app.task_events(task) if e][-1][2]
        self.app.acknowledge(PAPER, result["id"])
        core.write_json(self.home / "profile.json", {"background": "old", "areas": {}, "concepts": {}})
        self.app.update_record(PAPER, {"version": 0, "concepts": [{"name": "Known term", "status": "known"}]})
        tracked = [self.path / "agent-state.json", self.path / "log.jsonl", self.path / "outline.md",
                   self.home / "profile.json", self.path / "paper.json"]
        before = {str(p): p.read_bytes() for p in tracked}
        state, data = core.read_json(self.path / "agent-state.json"), core.read_json(self.path / "paper.json")
        prompt, _ = self.app._prompt(self.path, data, {"mode": "continue"}, state, "point", reading_context="Basics")
        body = layout()
        body["notes"][0]["text"] = "MAP_ONLY_PRIVATE_SENTINEL"
        self.app.update_map_layout(PAPER, body)
        after_prompt, _ = self.app._prompt(self.path, data, {"mode": "continue"}, state, "point", reading_context="Basics")
        self.assertEqual(after_prompt, prompt)
        self.assertNotIn("MAP_ONLY_PRIVATE_SENTINEL", after_prompt)
        self.assertEqual({str(p): p.read_bytes() for p in tracked}, before)

    def test_save_during_generation_succeeds_and_model_completion_keeps_layout(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        prompts = []
        def generate(prompt, schema):
            prompts.append(prompt)
            entered.set()
            release.wait(3)
            return fixtures.overview()
        self.backend.generate = generate
        identifier = self.app.explain(PAPER, fixtures.reading_request(app=self.app))
        self.assertTrue(entered.wait(2))
        self.assertIn(PAPER, self.app.active)
        saved = self.app.update_map_layout(PAPER, layout())
        self.assertEqual(saved["version"], 1)
        release.set()
        events = [e for e in self.app.task_events(identifier) if e]
        self.assertEqual(events[-1][1], "result")
        self.assertTrue(self.app.wait_closed(2))
        self.app.acknowledge(PAPER, events[-1][2]["id"])
        self.assertEqual(self.app.map_layout(PAPER), saved)
        self.assertNotIn(layout()["notes"][0]["text"], prompts[0])

    def test_two_writers_same_version_have_one_winner_without_lost_updates(self):
        other = Application(self.store, fixtures.FakeBackend())
        self.addCleanup(other.close)
        gate = threading.Barrier(2)
        def save(app, text):
            body = layout()
            body["notes"][0]["text"] = text
            gate.wait(timeout=2)
            try:
                return ("saved", app.update_map_layout(PAPER, body))
            except RuntimeError:
                return ("conflict", None)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(save, self.app, "First writer")
            second = pool.submit(save, other, "Second writer")
            outputs = [first.result(timeout=3), second.result(timeout=3)]
        self.assertEqual(sorted(item[0] for item in outputs), ["conflict", "saved"])
        winner = next(item[1] for item in outputs if item[0] == "saved")
        self.assertEqual(self.app.map_layout(PAPER), winner)

    def test_atomic_replace_failure_preserves_previous_map_and_cleans_temp_file(self):
        self.app.update_map_layout(PAPER, layout())
        before = (self.path / "map-layout.json").read_bytes()
        body = layout(1)
        body["nodes"][0]["x"] = 10
        with patch.object(core.os, "replace", side_effect=OSError("simulated disk error")):
            with self.assertRaises(OSError):
                self.app.update_map_layout(PAPER, body)
        self.assertEqual((self.path / "map-layout.json").read_bytes(), before)
        self.assertEqual(list(self.path.glob(".map-layout.json*")), [])
        self.assertFalse((self.path / ".agent.lock").exists())

    def test_papers_are_isolated_and_no_concept_membership_is_required(self):
        other = "2601.00002v1"
        core.write_json(self.home / "papers" / other / "paper.json", core.read_json(self.path / "paper.json"))
        self.app.update_map_layout(PAPER, layout())
        self.assertEqual(self.app.map_layout(other), {"version": 0, "nodes": [], "notes": []})
        body = layout()
        body["nodes"][0]["name"] = "Deleted or not yet taught concept"
        saved = self.app.update_map_layout(other, body)
        self.assertEqual(saved["nodes"], body["nodes"])
        self.assertNotEqual(self.app.map_layout(PAPER)["nodes"], saved["nodes"])

    def test_map_get_is_independent_of_corrupt_reading_state(self):
        self.app.update_map_layout(PAPER, layout())
        (self.path / "agent-state.json").write_bytes(b"broken unrelated reading state")
        self.assertEqual(self.app.map_layout(PAPER)["version"], 1)
        self.assertEqual(self.app.update_map_layout(PAPER, {"version": 1, "nodes": [], "notes": []})["version"], 2)
        self.assertEqual((self.path / "agent-state.json").read_bytes(), b"broken unrelated reading state")


if __name__ == "__main__":
    unittest.main()
