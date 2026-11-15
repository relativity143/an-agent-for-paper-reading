"""Offline prerequisite HTTP boundary and real-Application cache round trip."""
import json
from pathlib import Path
import tempfile
import threading
import unittest

from server.http import MAX_BODY_BYTES, create_server
from server.service import core
import test_server_integration as integration
import test_server_m2_http as m2_http


def prerequisite_items():
    return [{"name": "Toy-model parameter", "description": "A value used to specify a model.",
             "why": "The paragraph sets x=1.", "where": integration.LOCATION}]


class PrerequisiteApp:
    """Record only prerequisite calls; never load a paper or a model."""

    def __init__(self):
        self.calls = []
        self.error = None
        self.result = {"paper": "paper-1", "ready": False, "items": []}

    def _call(self, name, paper):
        self.calls.append((name, paper))
        if self.error is not None:
            raise self.error

    def prerequisites(self, paper):
        self._call("prerequisites", paper)
        return self.result

    def prepare_prerequisites(self, paper):
        self._call("prepare_prerequisites", paper)
        return "task-1"


class PrerequisiteHttpTests(unittest.TestCase):
    path = "/api/papers/paper-1/prerequisites"

    close_server = m2_http.M2HttpTests.close_server
    request = m2_http.M2HttpTests.request
    json_request = m2_http.M2HttpTests.json_request
    raw_request = m2_http.M2HttpTests.raw_request

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="paperread-prerequisite-http-")
        self.addCleanup(self.directory.cleanup)
        self.app = PrerequisiteApp()
        self.server = create_server(self.app, port=0, web_root=Path(self.directory.name))
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)
        self.host = f"127.0.0.1:{self.server.server_port}"

    def test_get_and_head_preserve_cache_shape_and_private_headers(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")
        for ready in (False, True):
            with self.subTest(ready=ready):
                self.app.result = {"paper": "paper-1", "ready": ready,
                                   "items": prerequisite_items() if ready else []}
                status, headers, body = self.request("GET", self.path)
                self.assertEqual((status, json.loads(body)), (200, self.app.result))
                self.assertEqual(self.app.calls[-1], ("prerequisites", "paper-1"))
                self.assertEqual(headers["Cache-Control"], "no-store")
                self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
                self.assertNotIn("Access-Control-Allow-Origin", headers)
                status, head_headers, body = self.request("HEAD", self.path)
                self.assertEqual((status, body), (200, b""))
                self.assertEqual(head_headers["Content-Length"], headers["Content-Length"])
                self.assertEqual(self.app.calls[-1], ("prerequisites", "paper-1"))

    def test_post_empty_object_returns_task_without_background_or_record_version(self):
        status, headers, body = self.json_request("POST", self.path, {})
        self.assertEqual((status, json.loads(body)), (202, {"task": "task-1"}))
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(self.app.calls, [("prepare_prerequisites", "paper-1")])

    def test_post_rejects_every_field_before_dispatch(self):
        for body in ({"reading_context": "Basics"}, {"record_version": 0},
                     {"refresh": True}, {"items": []}, {"anything": None}):
            with self.subTest(body=body):
                status, _, result = self.json_request("POST", self.path, body)
                self.assertEqual(status, 400)
                self.assertIn("error", json.loads(result))
        self.assertEqual(self.app.calls, [])

    def test_origin_checks_precede_prerequisite_reads_and_generation(self):
        forbidden = [
            {"Host": "attacker.example"}, {"Host": "127.0.0.1:1"},
            {"Origin": "https://attacker.example"}, {"Origin": "null"},
            {"Origin": f"http://localhost:{self.server.server_port}"},
            {"Sec-Fetch-Site": "cross-site"}, {"Referer": "https://attacker.example/page"},
        ]
        for method in ("GET", "HEAD", "POST"):
            for headers in forbidden:
                with self.subTest(method=method, headers=headers):
                    status, _, _ = self.json_request(
                        method, self.path, {} if method == "POST" else None, headers)
                    self.assertEqual(status, 403)
        self.assertEqual(self.app.calls, [])
        allowed = {"Origin": f"http://{self.host}", "Sec-Fetch-Site": "same-origin",
                   "Referer": f"http://{self.host}/"}
        for method in ("GET", "HEAD", "POST"):
            self.assertEqual(self.json_request(
                method, self.path, {} if method == "POST" else None, allowed)[0],
                202 if method == "POST" else 200)

    def test_errors_keep_service_statuses_and_do_not_leak_private_paths(self):
        for error, expected in ((ValueError("基础清单内容不合格。"), 400),
                                (RuntimeError("正在生成，请稍后重试。"), 409),
                                (FileNotFoundError("/private/paper.json"), 404),
                                (Exception("secret-token /private/paper.json"), 500)):
            self.app.error = error
            for method in ("GET", "HEAD", "POST"):
                with self.subTest(error=type(error).__name__, method=method):
                    status, headers, body = self.json_request(
                        method, self.path, {} if method == "POST" else None)
                    self.assertEqual(status, expected)
                    self.assertIn("application/json", headers["Content-Type"])
                    self.assertNotIn(b"/private", body)
                    self.assertNotIn(b"secret-token", body)
                    if method == "HEAD":
                        self.assertEqual(body, b"")
                    else:
                        self.assertEqual(set(json.loads(body)), {"error"})
                        if expected in {400, 409}:
                            self.assertEqual(json.loads(body)["error"], str(error))

    def test_invalid_json_and_body_limits_do_not_dispatch(self):
        cases = [
            (b"", {}, 400), (b"invalid", {}, 400), (b"\xff", {}, 400),
            (b"[]", {}, 400), (b"null", {}, 400), (b"false", {}, 400),
            (b"0", {}, 400), (b'""', {}, 400), (b"{}{}", {}, 400),
            (b'{"x": NaN}', {}, 400), (b'{"x": Infinity}', {}, 400),
            (b"{}", {"Content-Type": "text/plain"}, 415),
            (b"{}", {"Content-Length": str(MAX_BODY_BYTES + 1)}, 413),
            (b"{}", {"Transfer-Encoding": "chunked"}, 400),
        ]
        for body, headers, expected in cases:
            with self.subTest(body=body, headers=headers):
                self.assertEqual(self.request("POST", self.path, body,
                    {"Content-Type": "application/json", **headers})[0], expected)
        self.assertEqual(self.app.calls, [])

    def test_invalid_headers_and_incomplete_body_do_not_dispatch(self):
        cases = [
            ("", 411), ("Content-Length: -1\r\n", 400),
            ("Content-Length: 10\r\nContent-Length: 10\r\n", 400),
            ("Content-Length: 10\r\nHost: attacker.example\r\n", 400),
            ("Content-Length: 10\r\nOrigin: null\r\n", 403),
            (f"Content-Length: {MAX_BODY_BYTES + 1}\r\nExpect: 100-continue\r\n", 413),
        ]
        for headers, expected in cases:
            with self.subTest(headers=headers):
                self.assertEqual(self.raw_request("POST", self.path, headers)[0], expected)
        self.assertEqual(self.raw_request("POST", self.path, "Content-Length: 100\r\n",
                                         b"{}", close_write=True)[0], 400)
        self.assertEqual(self.app.calls, [])

    def test_unsupported_methods_and_paths_do_not_dispatch(self):
        cases = [
            ("PUT", self.path, 405), ("DELETE", self.path, 405),
            ("PATCH", self.path, 405), ("OPTIONS", self.path, 405),
            ("GET", "/api/papers/%2e%2e/prerequisites", 404),
            ("POST", "/api/papers/%2e%2e/prerequisites", 404),
            ("GET", self.path + "/extra", 404), ("POST", self.path + "/extra", 404),
        ]
        for method, path, expected in cases:
            with self.subTest(method=method, path=path):
                self.assertEqual(self.json_request(method, path, {})[0], expected)
        self.assertEqual(self.app.calls, [])


class PrerequisiteHttpIntegrationTests(unittest.TestCase):
    setUp = integration.ServerIntegrationTests.setUp
    start = integration.ServerIntegrationTests.start
    stop = integration.ServerIntegrationTests.stop
    stop_all = integration.ServerIntegrationTests.stop_all
    request = integration.ServerIntegrationTests.request
    json_request = integration.ServerIntegrationTests.json_request

    def prepare(self, path):
        task = self.json_request("POST", path, {}, expected=202)["task"]
        status, headers, stream = self.request("GET", f"/api/tasks/{task}/events")
        self.assertEqual(status, 200)
        self.assertIn("text/event-stream", headers["Content-Type"])
        events = []
        for frame in stream.decode("utf-8").split("\n\n"):
            fields = {}
            for line in frame.splitlines():
                if ": " in line and not line.startswith(":"):
                    key, value = line.split(": ", 1)
                    fields.setdefault(key, []).append(value)
            if "event" in fields:
                events.append((int(fields["id"][0]), fields["event"][0],
                               "\n".join(fields.get("data", []))))
        self.assertTrue(events, stream)
        self.assertEqual(events[-1][1], "result", events)
        self.assertEqual(sum(kind == "result" for _, kind, _ in events), 1)
        self.assertFalse(any(kind == "error" for _, kind, _ in events))
        self.assertEqual([identifier for identifier, _, _ in events],
                         sorted({identifier for identifier, _, _ in events}))
        status, _, replay = self.request("GET", f"/api/tasks/{task}/events",
                                         headers={"Last-Event-ID": str(events[-1][0])})
        self.assertEqual((status, replay), (200, b""))
        return json.loads(events[-1][2])

    def test_generate_reuse_and_restart_preserve_reader_records(self):
        paper = integration.PAPER
        path = f"/api/papers/{paper}/prerequisites"
        self.backend.responses = [{"items": prerequisite_items()}]
        core.write_json(self.home / "profile.json", {
            "background": "PRIVATE_PROFILE", "areas": {},
            "concepts": {"Old foundation": {"status": "known", "source": "PRIVATE_CONFIRMATION"}}})
        record = self.app.update_record(paper, {"version": 0, "concepts": [
            {"name": "Old foundation", "status": "known", "note": "PRIVATE_RECORD"}]})
        self.store.log_add(paper, {"explained": ["PRIVATE_LOG"]})
        (self.paper_dir / "outline.md").write_text("PRIVATE_OUTLINE\n", encoding="utf-8")
        preserved = [self.home / "profile.json", self.paper_dir / "agent-state.json",
                     self.paper_dir / "log.jsonl", self.paper_dir / "outline.md"]
        before = {target: target.read_bytes() for target in preserved}
        self.assertEqual(self.json_request("GET", path), {"paper": paper, "ready": False, "items": []})
        self.assertEqual(self.backend.prompts, [])
        self.assertEqual(self.app.tasks, {})

        result = {"paper": paper, "ready": True, "items": prerequisite_items()}
        self.assertEqual(self.prepare(path), result)
        self.assertEqual(self.json_request("GET", path), result)
        self.assertEqual(self.prepare(path), result)
        self.assertEqual(len(self.backend.prompts), 1)
        for token in ("PRIVATE_PROFILE", "PRIVATE_CONFIRMATION", "PRIVATE_RECORD", "PRIVATE_LOG", "PRIVATE_OUTLINE"):
            self.assertNotIn(token, self.backend.prompts[0])
        self.assertEqual(self.app.record(paper), record)
        self.assertEqual(self.app.history(paper), [])
        self.assertEqual({target: target.read_bytes() for target in preserved}, before)
        self.assertEqual(self.app.reading_sessions, {})
        self.assertEqual(self.client.attempts, [])

        self.stop(self.resources[0])
        restarted_backend = integration.FakeBackend()
        self.start(restarted_backend)
        self.assertEqual(self.json_request("GET", path), result)
        self.assertEqual(self.prepare(path), result)
        self.assertEqual(restarted_backend.prompts, [])
        self.assertEqual({target: target.read_bytes() for target in preserved}, before)
        self.assertEqual(self.client.attempts, [])

    def test_missing_paper_and_forbidden_origin_do_not_start_generation(self):
        path = f"/api/papers/{integration.PAPER}/prerequisites"
        for method in ("GET", "POST"):
            self.json_request(method, "/api/papers/missing/prerequisites",
                              {} if method == "POST" else None, expected=400)
            status, _, _ = self.request(method, path, {} if method == "POST" else None,
                                        headers={"Origin": "https://attacker.example"})
            self.assertEqual(status, 403)
        self.assertEqual(self.backend.prompts, [])
        self.assertEqual(self.app.tasks, {})
        self.assertEqual(self.client.attempts, [])


if __name__ == "__main__":
    unittest.main()
