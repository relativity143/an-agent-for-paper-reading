"""M2 route and request-boundary checks against an isolated loopback server."""
import http.client
import json
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest.mock import patch

from server.http import MAX_BODY_BYTES, create_server
import test_server_integration as integration


class RecordApp:
    """Record calls without loading papers, profiles, or a model backend."""

    def __init__(self):
        self.calls = []
        self.result = {"version": 0, "concepts": []}
        self.error = None

    def _call(self, *args):
        self.calls.append(args)
        if self.error is not None:
            raise self.error
        return self.result

    def record(self, paper):
        return self._call("record", paper)

    def update_record(self, paper, body):
        return self._call("update_record", paper, body)

    def feedback(self, paper, body):
        return self._call("feedback", paper, body)

    def explain(self, paper, body):
        self._call("explain", paper, body)
        return "task-1"

    def metadata(self, paper):
        return self._call("metadata", paper)

    def content(self, paper):
        return self._call("content", paper)

    def history(self, paper):
        return self._call("history", paper)


class M2HttpTests(unittest.TestCase):
    record_path = "/api/papers/paper-1/record"
    feedback_path = "/api/papers/paper-1/feedback"
    mutations = (("PUT", record_path), ("POST", feedback_path))

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="paperread-m2-http-")
        self.addCleanup(self.directory.cleanup)
        self.app = RecordApp()
        self.server = create_server(self.app, port=0, web_root=Path(self.directory.name))
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)
        self.host = f"127.0.0.1:{self.server.server_port}"

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.assertFalse(self.thread.is_alive())

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def json_request(self, method, path, body=None, headers=None):
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        return self.request(method, path, data,
                            {"Content-Type": "application/json", **(headers or {})})

    def raw_request(self, method, path, headers, body=b"", *, close_write=False):
        data = (f"{method} {path} HTTP/1.1\r\nHost: {self.host}\r\n"
                "Content-Type: application/json\r\n" + headers + "\r\n").encode() + body
        with socket.create_connection(self.server.server_address, timeout=2) as connection:
            connection.sendall(data)
            if close_write:
                connection.shutdown(socket.SHUT_WR)
            response = http.client.HTTPResponse(connection)
            response.begin()
            return response.status, response.read()

    def test_get_record_and_head_return_service_record_with_private_cache_headers(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")
        status, headers, body = self.request("GET", self.record_path)
        self.assertEqual((status, json.loads(body)), (200, {"version": 0, "concepts": []}))
        self.assertEqual(self.app.calls, [("record", "paper-1")])
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        status, head_headers, body = self.request("HEAD", self.record_path)
        self.assertEqual((status, body), (200, b""))
        self.assertEqual(head_headers["Content-Length"], headers["Content-Length"])

    def test_put_forwards_full_replacement_and_returns_server_provenance(self):
        payload = {"version": 7, "concepts": [
            {"name": "绕数", "status": "unsure", "note": "为什么是整数没懂"}]}
        self.app.result = {"version": 8, "concepts": [{
            **payload["concepts"][0], "explained_in": "explanation-1", "where": "S2.p1",
            "updated": "2026-11-15T08:41:00Z"}]}
        status, _, body = self.json_request("PUT", self.record_path, payload)
        self.assertEqual((status, json.loads(body)), (200, self.app.result))
        self.assertEqual(self.app.calls, [("update_record", "paper-1", payload)])
        self.assertIn("绕数".encode("utf-8"), body)
        empty = {"version": 8, "concepts": []}
        self.app.result = {"version": 9, "concepts": []}
        status, _, body = self.json_request("PUT", self.record_path, empty)
        self.assertEqual((status, json.loads(body)), (200, self.app.result))
        self.assertEqual(self.app.calls[-1], ("update_record", "paper-1", empty))

    def test_feedback_kinds_preserve_payload_and_return_latest_record(self):
        for kind in ("unclear", "stuck", "known"):
            with self.subTest(kind=kind):
                payload = {"explanation": "explanation-1", "kind": kind}
                if kind != "unclear":
                    payload.update(concept="左右本征态", note="反馈说明")
                self.app.result = {"version": 3, "concepts": []}
                status, _, body = self.json_request("POST", self.feedback_path, payload)
                self.assertEqual((status, json.loads(body)), (200, self.app.result))
                self.assertEqual(self.app.calls[-1], ("feedback", "paper-1", payload))

    def test_explain_forwards_confirmed_record_version(self):
        payload = {"mode": "continue", "reading_context": "从基础讲起。", "record_version": 3}
        status, _, body = self.json_request("POST", "/api/papers/paper-1/explain", payload)
        self.assertEqual((status, json.loads(body)), (202, {"task": "task-1"}))
        self.assertEqual(self.app.calls, [("explain", "paper-1", payload)])

    def test_application_validation_conflicts_and_missing_papers_keep_statuses(self):
        for error, expected in ((ValueError("记录字段不合格。"), 400),
                                (RuntimeError("记录已变更，请重新读取。"), 409),
                                (FileNotFoundError("/private/paper-state.json"), 404)):
            for method, path in (("GET", self.record_path), *self.mutations):
                with self.subTest(error=type(error).__name__, method=method):
                    self.app.error = error
                    status, headers, body = self.json_request(method, path, {} if method != "GET" else None)
                    self.assertEqual(status, expected)
                    self.assertIn("application/json", headers["Content-Type"])
                    self.assertEqual(set(json.loads(body)), {"error"})
                    if expected != 404:
                        self.assertEqual(json.loads(body)["error"], str(error))
                    self.assertNotIn(b"/private", body)

    def test_unexpected_application_errors_are_redacted(self):
        self.app.error = Exception("private-secret /Users/private/record.json")
        for method, path in (("GET", self.record_path), *self.mutations):
            with self.subTest(method=method):
                status, _, body = self.json_request(method, path, {} if method != "GET" else None)
                self.assertEqual(status, 500)
                self.assertNotIn(b"private-secret", body)
                self.assertNotIn(b"/Users", body)

    def test_origin_checks_precede_reading_and_mutation_calls(self):
        forbidden = [
            {"Host": "attacker.example"}, {"Host": "127.0.0.1:1"},
            {"Origin": "https://attacker.example"}, {"Origin": "null"},
            {"Origin": f"http://localhost:{self.server.server_port}"},
            {"Sec-Fetch-Site": "cross-site"}, {"Sec-Fetch-Site": "same-site"},
            {"Referer": "https://attacker.example/page"},
        ]
        for method, path in (("GET", self.record_path), *self.mutations):
            for headers in forbidden:
                with self.subTest(method=method, headers=headers):
                    status, _, _ = self.json_request(method, path, {} if method != "GET" else None, headers)
                    self.assertEqual(status, 403)
        self.assertEqual(self.app.calls, [])
        allowed = {"Origin": f"http://{self.host}", "Sec-Fetch-Site": "same-origin",
                   "Referer": f"http://{self.host}/"}
        self.assertEqual(self.json_request("PUT", self.record_path, {}, allowed)[0], 200)

    def test_invalid_json_shapes_constants_encoding_and_body_limits_do_not_dispatch(self):
        cases = [
            (b"invalid", {}, 400), (b"\xff", {}, 400), (b"[]", {}, 400),
            (b"null", {}, 400), (b"{}{}", {}, 400),
            (b'{"version": NaN}', {}, 400), (b'{"version": Infinity}', {}, 400),
            (b'{"version": -Infinity}', {}, 400),
            (b"{}", {"Content-Type": "text/plain"}, 415),
            (b"{}", {"Content-Length": str(MAX_BODY_BYTES + 1)}, 413),
            (b"{}", {"Transfer-Encoding": "chunked"}, 400),
        ]
        for method, path in self.mutations:
            for body, headers, expected in cases:
                with self.subTest(method=method, body=body, headers=headers):
                    status, _, _ = self.request(method, path, body,
                                               {"Content-Type": "application/json", **headers})
                    self.assertEqual(status, expected)
        self.assertEqual(self.app.calls, [])

    def test_invalid_headers_are_rejected_without_waiting_for_body(self):
        cases = [
            ("", 411), ("Content-Length: -1\r\n", 400),
            ("Content-Length: 10\r\nContent-Length: 10\r\n", 400),
            ("Content-Length: 10\r\nHost: attacker.example\r\n", 400),
            ("Content-Length: 10\r\nOrigin: null\r\n", 403),
            (f"Content-Length: {MAX_BODY_BYTES + 1}\r\nExpect: 100-continue\r\n", 413),
            ("Content-Length: 10\r\nTransfer-Encoding: chunked\r\n", 400),
        ]
        for method, path in self.mutations:
            for headers, expected in cases:
                with self.subTest(method=method, headers=headers):
                    status, _ = self.raw_request(method, path, headers)
                    self.assertEqual(status, expected)
        self.assertEqual(self.app.calls, [])

    def test_partial_body_eof_and_timeout_do_not_dispatch(self):
        for method, path in self.mutations:
            with self.subTest(method=method, failure="eof"):
                status, _ = self.raw_request(method, path, "Content-Length: 100\r\n",
                                             b"{}", close_write=True)
                self.assertEqual(status, 400)
            with self.subTest(method=method, failure="timeout"):
                with patch("server.http.SOCKET_TIMEOUT", 0.1):
                    status, _ = self.raw_request(method, path, "Content-Length: 100\r\n", b"{}")
                self.assertEqual(status, 408)
        self.assertEqual(self.app.calls, [])

    def test_other_methods_paths_and_traversal_do_not_dispatch_m2_actions(self):
        cases = [
            ("DELETE", self.record_path, 405), ("PATCH", self.record_path, 405),
            ("OPTIONS", self.feedback_path, 405), ("POST", self.record_path, 404),
            ("PUT", self.feedback_path, 405), ("GET", self.feedback_path, 404),
            ("GET", "/api/papers/%2e%2e/record", 404),
            ("PUT", "/api/papers/paper-1/record/extra", 405),
            ("POST", "/api/papers/paper-1/feedback/extra", 404),
        ]
        for method, path, expected in cases:
            with self.subTest(method=method, path=path):
                status, _, _ = self.json_request(method, path, {})
                self.assertEqual(status, expected)
        self.assertEqual(self.app.calls, [])


class M2HttpIntegrationTests(unittest.TestCase):
    # Reuse only fixture helpers, without inheriting and rediscovering M1 tests.
    setUp = integration.ServerIntegrationTests.setUp
    start = integration.ServerIntegrationTests.start
    stop = integration.ServerIntegrationTests.stop
    stop_all = integration.ServerIntegrationTests.stop_all
    request = integration.ServerIntegrationTests.request
    json_request = integration.ServerIntegrationTests.json_request
    turn = integration.ServerIntegrationTests.turn
    acknowledge = integration.ServerIntegrationTests.acknowledge
    record = integration.ServerIntegrationTests.record

    def test_real_application_record_feedback_and_version_conflicts_round_trip(self):
        paper_path = f"/api/papers/{integration.PAPER}"
        empty = {"version": 0, "concepts": []}
        self.assertEqual(self.record(), empty)
        request = {"mode": "continue", "reading_context": "从基础讲起。"}
        self.json_request("POST", paper_path + "/explain", request, expected=400)
        self.json_request("POST", paper_path + "/explain",
                          {**request, "record_version": 9}, expected=409)
        self.assertEqual(self.app.tasks, {})
        self.assertEqual(self.backend.prompts, [])

        overview = self.turn()  # The helper decodes the terminal SSE result as JSON.
        self.assertEqual(overview["type"], "overview")
        self.acknowledge(overview)
        self.assertEqual(self.record(), empty)
        point = self.turn()
        self.assertEqual(point["type"], "point")
        self.assertEqual(self.record(), empty)
        self.acknowledge(point)
        explained = self.record()
        self.assertEqual(explained["version"], 1)
        self.assertEqual(len(explained["concepts"]), 1)
        concept = explained["concepts"][0]
        self.assertEqual((concept["name"], concept["status"], concept["explained_in"], concept["where"]),
                         ("First foundation", "explained", point["id"], "S1.p1"))
        self.acknowledge(point)
        self.assertEqual(self.record(), explained)

        feedback = {"explanation": point["id"], "kind": "unclear", "note": "请换一种讲法。"}
        uncertain = self.json_request("POST", paper_path + "/feedback", feedback)
        self.assertEqual(uncertain["version"], 2)
        self.assertEqual(uncertain["concepts"][0]["status"], "unsure")
        self.assertEqual(uncertain["concepts"][0]["note"], feedback["note"])
        self.assertEqual(self.json_request("POST", paper_path + "/feedback", feedback), uncertain)
        self.assertEqual(self.record(), uncertain)

        replacement = {"version": 1, "concepts": [
            {"name": "First foundation", "status": "known", "note": "现在理解了。"}]}
        self.json_request("PUT", paper_path + "/record", replacement, expected=409)
        replacement["version"] = 2
        status, _, body = self.request("PUT", paper_path + "/record", replacement,
                                       headers={"Origin": "https://attacker.example"})
        self.assertEqual(status, 403)
        self.assertIn("error", json.loads(body))
        self.assertEqual(self.record(), uncertain)
        self.json_request("POST", paper_path + "/explain",
                          {**request, "record_version": 1}, expected=409)
        self.assertEqual(len(self.app.tasks), 2)
        self.assertEqual(len(self.backend.prompts), 2)

        confirmed = self.json_request("PUT", paper_path + "/record", replacement)
        self.assertEqual(confirmed["version"], 3)
        self.assertEqual(confirmed["concepts"][0]["status"], "known")
        self.assertEqual(confirmed["concepts"][0]["note"], "现在理解了。")
        self.assertEqual(confirmed["concepts"][0]["explained_in"], point["id"])
        self.assertEqual(confirmed["concepts"][0]["where"], "S1.p1")
        self.assertEqual(self.record(), confirmed)
        self.assertEqual(self.client.attempts, [])


if __name__ == "__main__":
    unittest.main()
