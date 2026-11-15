"""Offline settings, paper catalogue and persisted-question regressions."""
import copy
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from server.config import load_config
from server.schema import explanation_schema, validate_explanation, ValidationError
from server.service import Application, core
from test_server_service import FakeBackend, overview, point, PAPER


class LaunchServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="paperread-launch-service-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        self.store = core.Store(self.home)
        self.path = self.home / "papers" / PAPER
        self.data = {"title": "Synthetic launch paper", "authors": ["Test Author"], "source": "arxiv_html",
                     "source_url": "https://arxiv.org/html/" + PAPER, "parser_version": core.PARSER_VERSION,
                     "sections": [{"id": "S1", "title": "Setup", "order": 1, "end_order": 4}],
                     "paragraphs": [{"id": "S1.p1", "text": "Original text.", "section": "S1", "equations": [],
                                     "order": 2, "end_order": 3, "sentences": ["Original text."]}],
                     "figures": [], "equations": [], "references": []}
        core.write_json(self.path / "paper.json", self.data)
        self.config = {"backend": "claude", "custom": {"keep": [1, 2]},
                       "api": {"base_url": "https://provider.invalid/v1", "model": "test-model",
                               "api_key": "SECRET_TEST_KEY", "custom_option": True}}
        core.write_json(self.home / "config.json", self.config)
        self.backend = FakeBackend(overview(), point(), point())
        self.app = Application(self.store, self.backend, config=load_config(self.home),
                               backend_info={"backend": "claude", "backends": []})
        self.addCleanup(self.app.close)
        installed = patch("server.backends.shutil.which", side_effect=lambda executable: "/private/test/bin/" + executable
                          if executable == "claude" else None)
        installed.start()
        self.addCleanup(installed.stop)

    def turn(self, request=None):
        task = self.app.explain(PAPER, request or {"mode": "continue"})
        events = [event for event in self.app.task_events(task) if event]
        self.assertTrue(self.app.wait_closed(2))
        self.assertEqual(events[-1][1], "result", events[-1])
        return events[-1][2]

    def test_settings_public_view_and_unknown_config_fields(self):
        info = self.app.settings()
        self.assertEqual(set(info), {"backend", "backends", "api"})
        self.assertEqual(info["backend"], "claude")
        self.assertTrue(info["api"]["key_configured"])
        self.assertNotIn("SECRET_TEST_KEY", json.dumps(info))
        self.assertNotIn("/private/test", json.dumps(info))
        self.assertNotIn(str(self.home), json.dumps(info))
        self.assertTrue(all(item["authenticated"] is None for item in info["backends"]))

    def test_hot_switch_changes_health_and_next_generation_preserving_config(self):
        replacement = FakeBackend(overview())
        with patch("server.service.create_backend", return_value=replacement):
            response = self.app.update_settings({"backend": "api"})
        self.assertEqual(response["backend"], "api")
        self.assertEqual(self.app.health()["backend"], "api")
        self.assertEqual(self.app.settings()["backend"], "api")
        self.assertEqual(core.read_json(self.home / "config.json"), {**self.config, "backend": "api"})
        self.turn()
        self.assertEqual(len(replacement.prompts), 1)
        self.assertEqual(self.backend.prompts, [])

    def test_extra_fields_unavailable_and_invalid_values_preserve_config(self):
        before = (self.home / "config.json").read_bytes()
        for body in ({"backend": "codex"}, {"backend": "wrong"}, {"backend": ["api"]}, {},
                     {"backend": "api", "api_key": "do-not-accept"}, {"backend": "api", "extra": True}):
            with self.subTest(body=body), self.assertRaises(ValueError):
                self.app.update_settings(body)
            self.assertEqual((self.home / "config.json").read_bytes(), before)
            self.assertEqual(self.app.health()["backend"], "claude")

    def test_malformed_config_and_adapter_failure_preserve_original(self):
        path = self.home / "config.json"
        path.write_bytes(b'{"broken":')
        with self.assertRaises(ValueError):
            self.app.update_settings({"backend": "claude"})
        self.assertEqual(path.read_bytes(), b'{"broken":')
        core.write_json(path, self.config)
        before = path.read_bytes()
        with patch("server.service.create_backend", side_effect=RuntimeError("safe failure")), self.assertRaises(RuntimeError):
            self.app.update_settings({"backend": "api"})
        self.assertEqual(path.read_bytes(), before)
        self.assertIs(self.app.backend, self.backend)

    def test_missing_config_created(self):
        (self.home / "config.json").unlink()
        with patch("server.service.create_backend", return_value=FakeBackend()):
            self.app.update_settings({"backend": "claude"})
        self.assertEqual(core.read_json(self.home / "config.json"), {"backend": "claude"})

    def test_switch_during_generation_rejected_without_config_write(self):
        entered, release = threading.Event(), threading.Event()
        def generate(prompt, schema):
            entered.set()
            release.wait(3)
            return overview()
        self.backend.generate = generate
        task = self.app.explain(PAPER, {"mode": "continue"})
        self.assertTrue(entered.wait(2))
        before = (self.home / "config.json").read_bytes()
        try:
            with self.assertRaises(RuntimeError):
                self.app.update_settings({"backend": "api"})
            self.assertEqual((self.home / "config.json").read_bytes(), before)
        finally:
            release.set()
            list(self.app.task_events(task))

    def test_first_ask_saved_on_answer_not_overview_and_survives_history(self):
        request = {"mode": "ask", "where": "S1.p1", "quote": "Original text.", "question": "What is the basic idea?"}
        first = self.turn(request)
        self.assertIsNone(first["asked"])
        self.app.acknowledge(PAPER, first["id"])
        response = self.turn()
        expected = {"quote": request["quote"], "question": request["question"], "where": "S1.p1", "page": None}
        self.assertEqual(response["asked"], expected)
        self.assertEqual(self.app.history(PAPER)[-1]["asked"], expected)
        validate_explanation(response, paper=PAPER)
        self.app.acknowledge(PAPER, response["id"])
        third = self.turn()
        self.assertIsNone(third["asked"])
        context = json.loads(self.backend.prompts[-1].split("本轮材料（JSON 数据）：\n", 1)[1])
        self.assertTrue(all("asked" not in item for item in context["previous_explanations"]))
        self.assertNotIn("asked", explanation_schema("point")["properties"])

    def test_model_cannot_supply_asked_and_missing_values_are_null(self):
        first = self.turn()
        self.app.acknowledge(PAPER, first["id"])
        forged = point()
        forged["asked"] = {"question": "forged by model"}
        self.backend.responses = [forged]
        response = self.turn({"mode": "ask", "question": "Reader question"})
        self.assertEqual(response["asked"], {"quote": None, "question": "Reader question", "where": None, "page": None})
        bad = copy.deepcopy(response)
        bad["asked"]["page"] = True
        with self.assertRaises(ValidationError):
            validate_explanation(bad)

    def test_legacy_history_default_does_not_migrate_disk(self):
        legacy = dict(overview(), id="old", paper=PAPER)
        state = {"explanations": [legacy], "displayed": [], "pending": None}
        core.write_json(self.path / "agent-state.json", state)
        before = (self.path / "agent-state.json").read_bytes()
        self.assertIsNone(self.app.history(PAPER)[0]["asked"])
        self.assertIsNone(self.turn()["asked"])
        self.assertEqual((self.path / "agent-state.json").read_bytes(), before)

    def test_catalog_sorts_real_display_time_and_skips_corrupt_entries(self):
        recent = dict(self.data, title="Recently read", source="ar5iv")
        core.write_json(self.home / "papers" / "recent" / "paper.json", recent)
        core.write_json(self.home / "papers" / "recent" / "agent-state.json",
                        {"explanations": [{"id": "x"}], "displayed": ["x"], "last_read": "2026-11-15T11:00:00+08:00"})
        core.write_json(self.home / "papers" / "older" / "paper.json", dict(self.data, title="Earlier"))
        core.write_json(self.home / "papers" / "older" / "agent-state.json",
                        {"explanations": [{"id": "y"}], "displayed": ["y"], "last_read": "2026-11-15T02:00:00Z"})
        broken = self.home / "papers" / "broken"
        broken.mkdir()
        (broken / "paper.json").write_text("{", encoding="utf-8")
        outside = self.home / "outside"
        outside.mkdir()
        core.write_json(outside / "paper.json", self.data)
        (self.home / "papers" / "linked").symlink_to(outside, target_is_directory=True)
        papers = self.app.list_papers()["papers"]
        self.assertEqual([item["paper"] for item in papers], ["recent", "older", PAPER])
        self.assertEqual(papers[0]["source"], "arxiv_html")
        self.assertEqual(papers[0]["explanations"], 1)
        self.assertIsNone(papers[-1]["last_read"])
        self.assertEqual((broken / "paper.json").read_text(), "{")
        self.assertNotIn(str(self.home), json.dumps(papers))

    def test_generated_is_not_read_repeated_receipt_updates_time_only(self):
        first = self.turn()
        self.assertIsNone(self.app.list_papers()["papers"][0]["last_read"])
        with patch("server.service.core.now", return_value="2026-11-15T01:00:00+00:00"):
            self.app.acknowledge(PAPER, first["id"])
        old_log = (self.path / "log.jsonl").read_bytes()
        with patch("server.service.core.now", return_value="2026-11-15T02:00:00+00:00"):
            self.app.acknowledge(PAPER, first["id"])
        self.assertEqual(self.app.list_papers()["papers"][0]["last_read"], "2026-11-15T02:00:00+00:00")
        self.assertEqual((self.path / "log.jsonl").read_bytes(), old_log)
        self.assertFalse((self.home / "profile.json").exists())

    def test_old_displayed_state_uses_log_time_without_mutation(self):
        core.write_json(self.path / "agent-state.json", {"explanations": [{"id": "old"}], "displayed": ["old"]})
        self.store.log_add(PAPER, {"type": "overview", "where": None, "explanation_id": "old", "time": "2026-11-15T01:00:00Z"})
        before = (self.path / "agent-state.json").read_bytes()
        self.assertEqual(self.app.list_papers()["papers"][0]["last_read"], "2026-11-15T01:00:00+00:00")
        self.assertEqual((self.path / "agent-state.json").read_bytes(), before)

    def test_local_pdf_does_not_expose_original_uri(self):
        data = dict(self.data, source="local_pdf", source_url="file:///private/reader/topic.pdf")
        core.write_json(self.path / "paper.json", data)
        (self.path / "paper.pdf").write_bytes(b"%PDF- synthetic text extraction fixture")
        self.assertIsNone(self.app.metadata(PAPER)["source_url"])
        with patch("server.service.extract_pdf", return_value=("[PDF 第 1 页] source text", 1)):
            prompt, _ = self.app._prompt(self.path, data, {"mode": "continue"}, {"explanations": []}, "overview")
        self.assertNotIn("file:///", prompt)
        self.assertNotIn("/private/reader/", prompt)


    def test_close_while_generation_waits_for_file_lock_cancels_without_writing(self):
        self.turn()  # Leave the overview undisplayed so recovery would save an ask.
        original_lock = core.file_lock
        waiting = threading.Event()

        def observed_lock(path, *args, **kwargs):
            if threading.current_thread().name.startswith("paperread"):
                waiting.set()
            return original_lock(path, *args, **kwargs)

        with original_lock(self.path / ".agent.lock"):
            with patch.object(core, "file_lock", side_effect=observed_lock):
                task_id = self.app.explain(PAPER, {"mode": "ask", "question": "A question before shutdown"})
                self.assertTrue(waiting.wait(2))
                before = (self.path / "agent-state.json").read_bytes()
                self.app.close(wait=False)
        self.assertTrue(self.app.wait_closed(2))
        self.assertEqual((self.path / "agent-state.json").read_bytes(), before)
        self.assertEqual(self.app.get_task(task_id).events[-1][1], "error")

    def test_close_finishes_cancelled_queued_task_and_event_stream(self):
        entered, release = threading.Event(), threading.Event()
        app = Application(self.store, self.backend, workers=1)
        self.addCleanup(app.close)
        self.addCleanup(release.set)

        def generate(prompt, schema):
            entered.set()
            release.wait(3)
            return overview()

        self.backend.generate = generate
        queued_path = self.home / "papers" / "queued-paper"
        core.write_json(queued_path / "paper.json", self.data)
        app.explain(PAPER, {"mode": "continue"})
        self.assertTrue(entered.wait(2))
        task_id = app.explain("queued-paper", {"mode": "continue"})
        app.close(wait=False)
        queued = app.get_task(task_id)
        self.assertTrue(queued.done)
        self.assertNotIn("queued-paper", app.active)
        self.assertEqual([event[1] for event in app.task_events(task_id)], ["error"])
        self.assertFalse((queued_path / "agent-state.json").exists())
        release.set()
        self.assertTrue(app.wait_closed(2))
        self.assertEqual(app.active, {})


if __name__ == "__main__":
    unittest.main()
