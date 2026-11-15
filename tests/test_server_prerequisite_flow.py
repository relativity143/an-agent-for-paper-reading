"""Paper-only preparation: failure, isolation, invalidation and task lifecycle."""
import json
import threading
import unittest
from unittest.mock import patch

import test_server_service as fixtures
from server.backends import BackendError
from server.service import Application, core

PAPER = fixtures.PAPER


def requirements():
    return {"items": [{"name": "Model parameter", "description": "A number that specifies a model.",
                       "why": "The setup sets x=1.", "where": dict(fixtures.LOCATION)}]}


class PrerequisiteFlowTests(unittest.TestCase):
    setUp = fixtures.ServiceTests.setUp

    def finish(self, identifier):
        task = self.app.get_task(identifier)
        task.future.result(timeout=5)
        return [event for event in self.app.task_events(identifier) if event][-1]

    def prepare(self, *responses):
        self.backend.responses = list(responses or [requirements()])
        return self.finish(self.app.prepare_prerequisites(PAPER))

    def alter_paper(self, **changes):
        data = core.read_json(self.path / "paper.json")
        data.update(changes)
        core.write_json(self.path / "paper.json", data)

    def test_uncached_get_is_read_only_and_prepare_reads_no_learner_state(self):
        before = {p.name: p.read_bytes() for p in self.path.iterdir()}
        self.assertEqual(self.app.prerequisites(PAPER), {"paper": PAPER, "ready": False, "items": []})
        self.assertEqual({p.name: p.read_bytes() for p in self.path.iterdir()}, before)
        with patch.object(self.app, "_state", side_effect=AssertionError("No reading state")), \
                patch.object(self.store, "profile", side_effect=AssertionError("No profile")), \
                patch.object(self.store, "log_show", side_effect=AssertionError("No logs")), \
                patch.object(self.store, "log_add", side_effect=AssertionError("No log writes")), \
                patch.object(self.store, "update_profile", side_effect=AssertionError("No profile writes")):
            self.assertEqual(self.prepare()[1], "result")
            self.assertTrue(self.app.prerequisites(PAPER)["ready"])
        self.assertEqual({p.name for p in self.path.iterdir()}, {"paper.json", "prerequisites.json"})
        material = json.loads(self.backend.prompts[0].split("本轮材料（JSON 数据）：\n")[1])
        self.assertEqual(set(material), {"paper", "type", "title", "paper_text", "source_url", "pdf_pages", "valid_ids"})
        self.assertEqual(material["type"], "prerequisites")
        self.assertIn("x=1", material["paper_text"])

    def test_cache_reuse_returns_independent_data_and_does_not_invoke_model(self):
        original = self.prepare()[2]
        self.assertEqual(len(self.backend.prompts), 1)
        original["items"][0]["name"] = "Changed outside service"
        second = self.prepare()[2]
        self.assertEqual(second["items"], requirements()["items"])
        self.assertEqual(len(self.backend.prompts), 1)
        other = Application(self.store, fixtures.FakeBackend())
        self.addCleanup(other.close)
        self.assertEqual(other.prerequisites(PAPER), second)

    def test_changed_paper_invalidates_cache_without_overwriting_until_success(self):
        self.prepare()
        cached = (self.path / "prerequisites.json").read_bytes()
        self.alter_paper(title="Updated manuscript")
        self.assertFalse(self.app.prerequisites(PAPER)["ready"])
        self.assertEqual(self.prepare(BackendError("Unavailable"))[1], "error")
        self.assertEqual((self.path / "prerequisites.json").read_bytes(), cached)
        self.assertEqual(self.prepare()[1], "result")
        self.assertNotEqual((self.path / "prerequisites.json").read_bytes(), cached)

    def test_source_refresh_before_cache_lookup_uses_fresh_snapshot(self):
        self.prepare()
        original = self.app._load
        def refresh_after_initial_load(identifier):
            path, data = original(identifier)
            if data["title"] == "Synthetic paper":
                self.alter_paper(title="Refreshed before lock")
            return path, data
        with patch.object(self.app, "_load", side_effect=refresh_after_initial_load):
            self.assertFalse(self.app.prerequisites(PAPER)["ready"])
        self.alter_paper(title="Synthetic paper")
        with patch.object(self.app, "_load", side_effect=refresh_after_initial_load):
            self.assertEqual(self.prepare()[1], "result")
        self.assertEqual(len(self.backend.prompts), 2)
        material = json.loads(self.backend.prompts[-1].split("本轮材料（JSON 数据）：\n")[1])
        self.assertEqual(material["title"], "Refreshed before lock")
        self.assertTrue(self.app.prerequisites(PAPER)["ready"])

    def test_local_pdf_uses_file_pages_redacts_path_and_hashes_pdf_bytes(self):
        self.alter_paper(source="local_pdf", source_url="file:///private/reader/paper.pdf")
        pdf = self.path / "paper.pdf"
        pdf.write_bytes(b"%PDF- synthetic version one")
        response = requirements()
        response["items"][0]["where"] = {"ids": [], "page": 2, "quote": "A parameter x."}
        with patch("server.service.extract_pdf", return_value=("[Page 2] A parameter x.", 2)) as extract:
            self.assertEqual(self.prepare(response)[1], "result")
            extract.assert_called_once_with(pdf)
        material = json.loads(self.backend.prompts[0].split("本轮材料（JSON 数据）：\n")[1])
        self.assertEqual(material["pdf_pages"], 2)
        self.assertIsNone(material["source_url"])
        self.assertFalse("/private/reader" in self.backend.prompts[0])
        pdf.write_bytes(b"%PDF- synthetic version two")
        self.assertFalse(self.app.prerequisites(PAPER)["ready"])

    def test_paper_changed_during_generation_does_not_save_stale_result(self):
        def generating(prompt, schema):
            self.alter_paper(title="Changed while model ran")
            return requirements()
        self.backend.generate = generating
        _, kind, message = self.prepare()
        self.assertEqual(kind, "error")
        self.assertIn("原文在分析期间发生变化", message)
        self.assertFalse((self.path / "prerequisites.json").exists())
        self.assertFalse((self.path / "agent-state.json").exists())

    def test_validation_retries_only_once_then_fails_without_partial_cache(self):
        invalid = {"items": []}
        self.assertEqual(self.prepare(invalid, invalid, requirements())[1], "error")
        self.assertEqual(len(self.backend.prompts), 2)
        self.assertFalse((self.path / "prerequisites.json").exists())
        self.assertFalse((self.path / "agent-state.json").exists())
        self.assertNotIn(PAPER, self.app.active)
        self.assertEqual(self.prepare(invalid, requirements())[1], "result")
        self.assertEqual(len(self.backend.prompts), 4)

    def test_backend_error_and_unexpected_error_are_retryable_without_private_details(self):
        for error in (BackendError("Model unavailable"), OSError("/private/test-secret")):
            with self.subTest(error=type(error).__name__):
                _, kind, message = self.prepare(error)
                self.assertEqual(kind, "error")
                self.assertNotIn("test-secret", message)
                self.assertNotIn(PAPER, self.app.active)
                self.assertFalse((self.path / "prerequisites.json").exists())
        self.assertEqual(self.prepare()[1], "result")

    def test_corrupt_cache_is_preserved_on_get_and_failure_then_explicitly_rebuilt(self):
        target = self.path / "prerequisites.json"
        target.write_bytes(b"{broken")
        with self.assertRaisesRegex(ValueError, "缓存损坏"):
            self.app.prerequisites(PAPER)
        self.assertEqual(target.read_bytes(), b"{broken")
        self.assertEqual(self.prepare(BackendError("Unavailable"))[1], "error")
        self.assertEqual(target.read_bytes(), b"{broken")
        self.assertEqual(self.prepare()[1], "result")
        self.assertTrue(self.app.prerequisites(PAPER)["ready"])

    def test_symlink_cache_is_never_followed_or_replaced(self):
        target = self.home / "private.json"
        target.write_bytes(b"Keep original")
        link = self.path / "prerequisites.json"
        link.symlink_to(target)
        with self.assertRaisesRegex(ValueError, "不是普通文件"):
            self.app.prerequisites(PAPER)
        self.assertEqual(self.prepare()[1], "error")
        self.assertTrue(link.is_symlink())
        self.assertEqual(target.read_bytes(), b"Keep original")
        self.assertEqual(len(self.backend.prompts), 0)

    def test_failed_atomic_cache_write_keeps_previous_cache(self):
        self.prepare()
        target = self.path / "prerequisites.json"
        before = target.read_bytes()
        self.alter_paper(title="Updated source")
        with patch.object(core.os, "replace", side_effect=OSError("Synthetic write failure")):
            self.assertEqual(self.prepare()[1], "error")
        self.assertEqual(target.read_bytes(), before)
        self.assertEqual({p.name for p in self.path.iterdir()}, {"paper.json", "prerequisites.json"})

    def test_context_limits_fail_before_backend_without_truncating_source(self):
        with patch("server.service.MAX_PAPER_CHARS", 1):
            self.assertEqual(self.prepare()[1], "error")
        with patch("server.service.MAX_PROMPT_CHARS", 1):
            self.assertEqual(self.prepare()[1], "error")
        self.assertEqual(len(self.backend.prompts), 0)
        self.assertFalse((self.path / "prerequisites.json").exists())

    def test_repair_cannot_exceed_prompt_limit(self):
        self.prepare()
        limit = len(self.backend.prompts[0])
        self.alter_paper(title="X")
        with patch("server.service.MAX_PROMPT_CHARS", limit):
            self.assertEqual(self.prepare({"items": []}, requirements())[1], "error")
        self.assertEqual(len(self.backend.prompts), 2)
        self.assertFalse(self.app.prerequisites(PAPER)["ready"])

    def test_cancellation_releases_slot_and_cannot_commit_late_result(self):
        entered, release = threading.Event(), threading.Event()
        def generating(prompt, schema):
            entered.set()
            release.wait(5)
            return requirements()
        self.backend.generate = generating
        task = self.app.prepare_prerequisites(PAPER)
        try:
            self.assertTrue(entered.wait(3))
            self.assertTrue(self.app.cancel_task(task)["cancelled"])
        finally:
            release.set()
        self.assertEqual(self.finish(task)[1], "error")
        self.assertFalse((self.path / "prerequisites.json").exists())
        self.assertNotIn(PAPER, self.app.active)
        self.assertFalse((self.path / "agent-state.json").exists())
        self.assertEqual(self.prepare()[1], "result")

    def test_preparation_blocks_record_and_other_generation_but_allows_map(self):
        entered, release = threading.Event(), threading.Event()
        def generating(prompt, schema):
            entered.set()
            release.wait(5)
            return requirements()
        self.backend.generate = generating
        task = self.app.prepare_prerequisites(PAPER)
        try:
            self.assertTrue(entered.wait(3))
            with self.assertRaises(RuntimeError):
                self.app.prepare_prerequisites(PAPER)
            with self.assertRaises(RuntimeError):
                self.app.explain(PAPER, {"mode": "continue", "reading_context": "New selection", "record_version": 0})
            with self.assertRaises(RuntimeError):
                self.app.update_record(PAPER, {"version": 0, "concepts": []})
            self.app.update_map_layout(PAPER, {"version": 0, "nodes": [], "notes": []})
        finally:
            release.set()
        self.assertEqual(self.finish(task)[1], "result")

    def test_preparation_does_not_bypass_explain_background_and_version_checks(self):
        self.prepare()
        for body in ({"mode": "continue", "record_version": 0},
                     {"mode": "continue", "reading_context": "I choose to learn basics"}):
            with self.assertRaises(ValueError):
                self.app.explain(PAPER, body)
        with self.assertRaises(RuntimeError):
            self.app.explain(PAPER, {"mode": "continue", "reading_context": "Basics", "record_version": 99})
        self.assertEqual(len(self.backend.prompts), 1)


if __name__ == "__main__":
    unittest.main()
