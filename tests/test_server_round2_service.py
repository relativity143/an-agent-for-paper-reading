"""Offline cancellation races and round-two HTTP contracts, no model calls."""
import http.client
import json
import threading
import unittest
from unittest.mock import patch

from server.http import create_server
from server.backends import BackendError
from server.config import load_config
from server.service import Application, Task, core
import test_server_service as fixtures
from test_server_service import FakeBackend, overview, point, PAPER, reading_request


class RoundTwoServiceTests(unittest.TestCase):
    setUp = fixtures.ServiceTests.setUp
    run_turn = fixtures.ServiceTests.run_turn

    def blocking_backend(self):
        entered, release = threading.Event(), threading.Event()
        original = self.backend.generate
        def generate(prompt, schema):
            entered.set()
            release.wait(4)
            return original(prompt, schema)
        self.backend.generate = generate
        self.addCleanup(release.set)
        return entered, release

    def test_cancel_returns_terminal_error_and_late_result_never_saves(self):
        entered, release = self.blocking_backend()
        task = self.app.explain(PAPER, reading_request({"mode": "continue"}, app=self.app, paper=PAPER))
        self.assertTrue(entered.wait(2))
        self.assertEqual(self.app.cancel_task(task), {"task": task, "cancelled": True})
        events = list(self.app.task_events(task))
        self.assertEqual(events[-1][1:], ("error", "已停止这一轮。"))
        self.assertNotIn(PAPER, self.app.active)
        release.set()
        self.assertTrue(self.app.wait_closed(2))
        self.assertEqual(self.app.history(PAPER), [])
        self.assertFalse((self.path / "outline.md").exists())
        self.assertEqual(list(self.app.task_events(task)), events)

    def test_cancel_allows_immediate_settings_switch_and_new_task(self):
        entered, release = self.blocking_backend()
        task = self.app.explain(PAPER, reading_request({"mode": "continue"}, app=self.app, paper=PAPER))
        self.assertTrue(entered.wait(2))
        self.app.cancel_task(task)
        replacement = FakeBackend(overview())
        with patch("server.backends.shutil.which", return_value="/test/codex"), patch(
                "server.service.create_backend", return_value=replacement):
            response = self.app.update_settings({"backend": "codex", "model": "valid-model", "timeout_seconds": 450})
        self.assertEqual(response["timeout_seconds"], 450)
        new_task = self.app.explain(PAPER, reading_request({"mode": "continue"}, app=self.app, paper=PAPER))
        self.assertNotEqual(task, new_task)
        result = list(self.app.task_events(new_task))[-1]
        self.assertEqual(result[1], "result", result)
        release.set()
        self.assertTrue(self.app.wait_closed(2))
        self.assertEqual(len(self.app.history(PAPER)), 1)
        self.assertEqual(self.app.history(PAPER)[0]["id"], result[2]["id"])

    def test_cancel_finished_is_idempotent_and_does_not_erase_result(self):
        _, kind, result = self.run_turn()
        self.assertEqual(kind, "result")
        task = next(iter(self.app.tasks))
        before = (self.path / "agent-state.json").read_bytes()
        for _ in range(2):
            self.assertTrue(self.app.cancel_task(task)["cancelled"])
        self.assertEqual((self.path / "agent-state.json").read_bytes(), before)
        self.assertEqual(list(self.app.task_events(task))[-1][1], "result")

    def test_cancel_unknown_task_is_not_found(self):
        with self.assertRaises(FileNotFoundError):
            self.app.cancel_task("unknown")

    def test_cleanup_failure_blocks_switch_and_can_be_retried(self):
        entered, release = self.blocking_backend()
        identifier = self.app.explain(PAPER, reading_request({"mode": "continue"}, app=self.app, paper=PAPER))
        self.assertTrue(entered.wait(2))
        task = self.app.get_task(identifier)
        calls = []
        def stop():
            calls.append(1)
            if len(calls) == 1:
                raise OSError("synthetic cleanup failure")
        task.cancellation.register("test-resource", stop)
        with self.assertRaises(BackendError):
            self.app.cancel_task(identifier)
        release.set()
        self.assertTrue(self.app.wait_closed(2))
        self.assertTrue(task.cleanup_pending)
        with self.assertRaises(RuntimeError):
            self.app.update_settings({"backend": "claude"})
        with self.assertRaises(RuntimeError):
            self.app.explain(PAPER, reading_request({"mode": "continue"}, app=self.app, paper=PAPER))
        self.assertTrue(self.app.cancel_task(identifier)["cancelled"])
        self.assertFalse(task.cleanup_pending)
        self.assertEqual(len(calls), 2)
        self.assertNotIn(PAPER, self.app.active)
        self.assertEqual(self.app.history(PAPER), [])

    def test_completed_task_eviction_cannot_leave_dangling_active_entry(self):
        for index in range(128):
            task = Task("old-" + str(index))
            task.emit("error", "finished")
            self.app.tasks[task.id] = task
            if index == 0:
                self.app.active[task.paper] = task.id
        entered, release = self.blocking_backend()
        identifier = self.app.explain(PAPER, reading_request({"mode": "continue"}, app=self.app, paper=PAPER))
        self.assertTrue(entered.wait(2))
        self.assertNotIn("old-0", self.app.active)
        self.assertTrue(all(key in self.app.tasks for key in self.app.active.values()))
        with self.assertRaises(RuntimeError):
            self.app.update_settings({"backend": "claude"})
        self.app.cancel_task(identifier)
        release.set()

    def test_natural_cleanup_failure_remains_retryable(self):
        entered, release = self.blocking_backend()
        task_id = self.app.explain(PAPER, reading_request({"mode": "continue"}, app=self.app, paper=PAPER))
        self.assertTrue(entered.wait(2))
        task = self.app.get_task(task_id)
        stopped = []
        task.cancellation.register("leftover-process", lambda: stopped.append(True))
        self.backend.responses = [BackendError("模型进程未能完全退出。")]
        release.set()
        self.assertTrue(self.app.wait_closed(2))
        self.assertTrue(task.cleanup_pending)
        self.assertIn(PAPER, self.app.active)
        with self.assertRaises(RuntimeError):
            self.app.update_settings({"backend": "claude"})
        self.app.cancel_task(task_id)
        self.assertEqual(stopped, [True])
        self.assertNotIn(PAPER, self.app.active)
        self.assertEqual(self.app.history(PAPER), [])

    def test_settings_report_running_model_and_timeout_after_external_edit(self):
        core.write_json(self.home / "config.json", {"backend": "claude", "timeout_seconds": 300,
                                                   "claude": {"model": "opus"}})
        self.app.config = load_config(self.home)
        core.write_json(self.home / "config.json", {"backend": "codex", "timeout_seconds": 600,
                     "claude": {"model": "sonnet", "model_suggestions": ["sonnet"]}})
        with patch("server.backends.shutil.which", return_value="/test/cli"):
            settings = self.app.settings()
        self.assertEqual(settings["backend"], "claude")
        self.assertEqual(settings["timeout_seconds"], 300)
        selected = next(item for item in settings["backends"] if item["name"] == "claude")
        self.assertEqual(selected["model"], "opus")
        self.assertEqual(selected["model_suggestions"], ["sonnet"])

    def test_other_paper_keeps_running_and_blocks_settings(self):
        other = "2601.00002v1"
        core.write_json(self.home / "papers" / other / "paper.json", core.read_json(self.path / "paper.json"))
        two_started, release = threading.Event(), threading.Event()
        count = []
        def generate(prompt, schema):
            count.append(prompt)
            if len(count) == 2:
                two_started.set()
            release.wait(4)
            return overview()
        self.backend.generate = generate
        self.addCleanup(release.set)
        first = self.app.explain(PAPER, reading_request({"mode": "continue"}, app=self.app, paper=PAPER))
        second = self.app.explain(other, reading_request({"mode": "continue"}, app=self.app, paper=other))
        self.assertTrue(two_started.wait(2))
        self.app.cancel_task(first)
        self.assertFalse(self.app.get_task(second).cancellation.is_set())
        with self.assertRaises(RuntimeError):
            self.app.update_settings({"backend": "claude"})
        release.set()
        self.assertEqual(list(self.app.task_events(second))[-1][1], "result")
        self.assertTrue(self.app.wait_closed(2))
        self.assertEqual(self.app.history(PAPER), [])
        self.assertEqual(len(self.app.history(other)), 1)

    def test_cancel_queued_task_never_calls_backend(self):
        self.app.close()
        self.app = Application(self.store, self.backend, workers=1)
        self.addCleanup(self.app.close)
        entered, release = self.blocking_backend()
        first = self.app.explain(PAPER, reading_request({"mode": "continue"}, app=self.app, paper=PAPER))
        self.assertTrue(entered.wait(2))
        other = "2601.00002v1"
        core.write_json(self.home / "papers" / other / "paper.json", core.read_json(self.path / "paper.json"))
        second = self.app.explain(other, reading_request({"mode": "continue"}, app=self.app, paper=other))
        self.app.cancel_task(second)
        self.assertTrue(self.app.get_task(second).future.cancelled())
        release.set()
        self.assertEqual(list(self.app.task_events(first))[-1][1], "result")
        self.assertTrue(self.app.wait_closed(2))
        self.assertEqual(len(self.backend.prompts), 1)
        self.assertEqual(self.app.history(other), [])

    def test_cancel_while_waiting_to_save_discards_valid_result(self):
        entered, release = self.blocking_backend()
        task = self.app.explain(PAPER, reading_request({"mode": "continue"}, app=self.app, paper=PAPER))
        self.assertTrue(entered.wait(2))
        with core.file_lock(self.path / ".agent.lock"):
            release.set()
            self.app.cancel_task(task)
        self.assertTrue(self.app.wait_closed(2))
        self.assertEqual(self.app.history(PAPER), [])

    def test_cancel_during_validation_prevents_repair_call(self):
        self.backend.responses = [{"type": "overview"}]
        entered, release = threading.Event(), threading.Event()
        def validate(*args, **kwargs):
            entered.set()
            release.wait(4)
            raise ValueError("需修正")
        self.addCleanup(release.set)
        with patch("server.service.validate_explanation", side_effect=validate):
            task = self.app.explain(PAPER, reading_request({"mode": "continue"}, app=self.app, paper=PAPER))
            self.assertTrue(entered.wait(2))
            self.app.cancel_task(task)
            release.set()
            self.assertTrue(self.app.wait_closed(2))
        self.assertEqual(len(self.backend.prompts), 1)
        self.assertEqual(self.app.history(PAPER), [])

    def test_pending_exposed_on_metadata_survives_cancel_and_clears_on_answer(self):
        question = {"mode": "ask", "where": "S1.p1", "quote": "Original text.", "question": "基础是什么？"}
        entered, release = self.blocking_backend()
        task = self.app.explain(PAPER, reading_request(question, app=self.app, paper=PAPER))
        self.assertTrue(entered.wait(2))
        expected = {k: question.get(k) for k in ("quote", "question", "where", "page")}
        self.assertEqual(self.app.metadata(PAPER)["pending"], expected)
        self.app.cancel_task(task)
        release.set()
        self.assertTrue(self.app.wait_closed(2))
        self.assertEqual(self.app.metadata(PAPER)["pending"], expected)
        self.backend.generate = FakeBackend(overview(), point()).generate
        _, _, first = self.run_turn()
        self.app.acknowledge(PAPER, first["id"])
        self.assertIsNone(first["asked"])
        self.assertEqual(self.app.metadata(PAPER)["pending"], expected)
        _, _, answer = self.run_turn()
        self.assertEqual(answer["asked"], expected)
        self.assertIsNone(self.app.metadata(PAPER)["pending"])

    def test_pending_old_state_is_null_without_migration(self):
        old = {"explanations": [], "displayed": []}
        core.write_json(self.path / "agent-state.json", old)
        before = (self.path / "agent-state.json").read_bytes()
        self.assertIsNone(self.app.metadata(PAPER)["pending"])
        self.assertEqual((self.path / "agent-state.json").read_bytes(), before)


class RoundTwoHTTPTests(unittest.TestCase):
    setUp = fixtures.ServiceTests.setUp

    def start_http(self):
        self.server = create_server(self.app, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01})
        self.thread.start()
        def close():
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(2)
        self.addCleanup(close)

    def request(self, method, route, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        payload = json.dumps({} if body is None else body).encode() if method in {"POST", "PUT"} else None
        try:
            connection.request(method, route, payload, {"Content-Type": "application/json", **(headers or {})})
            response = connection.getresponse()
            content = response.read().decode()
            return response.status, content
        finally:
            connection.close()

    def test_cancel_route_event_stream_and_origin(self):
        entered, release = threading.Event(), threading.Event()
        def generate(prompt, schema):
            entered.set()
            release.wait(4)
            return overview()
        self.backend.generate = generate
        self.addCleanup(release.set)
        self.start_http()
        task = self.app.explain(PAPER, reading_request({"mode": "continue"}, app=self.app, paper=PAPER))
        self.assertTrue(entered.wait(2))
        route = f"/api/tasks/{task}/cancel"
        self.assertEqual(self.request("POST", route, headers={"Origin": "https://foreign.invalid"})[0], 403)
        self.assertFalse(self.app.get_task(task).done)
        self.assertEqual(self.request("POST", route, {"extra": 1})[0], 400)
        for _ in range(2):
            status, body = self.request("POST", route)
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body), {"task": task, "cancelled": True})
        status, events = self.request("GET", f"/api/tasks/{task}/events")
        self.assertEqual(status, 200)
        self.assertIn("event: error\ndata: 已停止这一轮。", events)
        self.assertNotIn("event: result", events)
        self.assertEqual(self.request("POST", "/api/tasks/missing/cancel")[0], 404)
        release.set()

    def test_settings_put_model_and_limits_at_http_boundary(self):
        self.start_http()
        with patch("server.backends.shutil.which", return_value="/test/claude"), patch(
                "server.service.create_backend", return_value=self.backend):
            self.assertEqual(self.request("PUT", "/api/settings", {"backend": "claude", "model": "sonnet"})[0], 200)
            for changes in ({"model": "-x"}, {"model": "x y"}, {"model": "x" * 101},
                            {"timeout_seconds": 20}, {"timeout_seconds": 601}, {"timeout_seconds": True},
                            {"backend": "api", "model": "x"}, {"api_key": "PRIVATE"}, {}):
                before = (self.home / "config.json").read_bytes()
                status, body = self.request("PUT", "/api/settings", changes)
                self.assertEqual(status, 400, changes)
                self.assertNotIn("PRIVATE", body)
                self.assertEqual((self.home / "config.json").read_bytes(), before)
            status, body = self.request("PUT", "/api/settings", {"timeout_seconds": 600, "model": None})
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)["timeout_seconds"], 600)

    def test_header_and_parser_errors_are_chinese(self):
        self.start_http()
        for method, route, body, headers, expected in (
            ("POST", "/api/papers/upload", {}, {}, 400),
            ("POST", "/api/papers", {}, {"Content-Length": "1048577"}, 413),
            ("PUT", "/api/settings", {}, {"Origin": "null"}, 403),
            ("GET", "/api/not-found", None, {}, 404),
            ("DELETE", "/api/papers", None, {}, 405),
        ):
            status, text = self.request(method, route, body, headers)
            self.assertEqual(status, expected)
            self.assertRegex(json.loads(text)["error"], "[\u4e00-\u9fff]")


if __name__ == "__main__":
    unittest.main()
