"""M3 map routes use the existing same-origin, JSON, and error boundary."""
import json
from pathlib import Path
import tempfile
import threading
import unittest

from server.http import MAX_BODY_BYTES, create_server
import test_server_integration as integration
import test_server_m2_http as m2_http


class MapApp(m2_http.RecordApp):
    """Record map calls without loading any paper or model."""

    def __init__(self):
        super().__init__()
        self.result = {"version": 0, "nodes": [], "notes": []}

    def map_layout(self, paper):
        return self._call("map_layout", paper)

    def update_map_layout(self, paper, body):
        return self._call("update_map_layout", paper, body)


class M3HttpTests(unittest.TestCase):
    map_path = "/api/papers/paper-1/map"

    # Reuse transport and cleanup helpers without rediscovering M2 tests.
    close_server = m2_http.M2HttpTests.close_server
    request = m2_http.M2HttpTests.request
    json_request = m2_http.M2HttpTests.json_request
    raw_request = m2_http.M2HttpTests.raw_request

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="paperread-m3-http-")
        self.addCleanup(self.directory.cleanup)
        self.app = MapApp()
        self.server = create_server(self.app, port=0, web_root=Path(self.directory.name))
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)
        self.host = f"127.0.0.1:{self.server.server_port}"

    def test_get_and_head_return_map_with_private_cache_headers(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")
        status, headers, body = self.request("GET", self.map_path)
        self.assertEqual((status, json.loads(body)), (200, self.app.result))
        self.assertEqual(self.app.calls, [("map_layout", "paper-1")])
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        status, head_headers, body = self.request("HEAD", self.map_path)
        self.assertEqual((status, body), (200, b""))
        self.assertEqual(head_headers["Content-Length"], headers["Content-Length"])
        self.assertEqual(self.app.calls[-1], ("map_layout", "paper-1"))

    def test_put_forwards_complete_replacement_and_returns_saved_map(self):
        payload = {"version": 7, "nodes": [{"name": "绕数", "x": 880, "y": 790}],
                   "notes": [{"id": "n-1", "x": 652, "y": 520, "text": "回头再看。"}]}
        self.app.result = {**payload, "version": 8}
        status, _, body = self.json_request("PUT", self.map_path, payload)
        self.assertEqual((status, json.loads(body)), (200, self.app.result))
        self.assertEqual(self.app.calls, [("update_map_layout", "paper-1", payload)])
        self.assertIn("绕数".encode("utf-8"), body)
        empty = {"version": 8, "nodes": [], "notes": []}
        self.app.result = {**empty, "version": 9}
        status, _, body = self.json_request("PUT", self.map_path, empty)
        self.assertEqual((status, json.loads(body)), (200, self.app.result))
        self.assertEqual(self.app.calls[-1], ("update_map_layout", "paper-1", empty))

    def test_validation_conflict_and_missing_paper_errors_keep_statuses(self):
        for error, expected in ((ValueError("地图字段不合格。"), 400),
                                (RuntimeError("地图已变更，请重新读取。"), 409),
                                (FileNotFoundError("/private/map.json"), 404)):
            self.app.error = error
            for method in ("GET", "PUT"):
                with self.subTest(error=type(error).__name__, method=method):
                    status, headers, body = self.json_request(
                        method, self.map_path, {} if method == "PUT" else None)
                    self.assertEqual(status, expected)
                    self.assertIn("application/json", headers["Content-Type"])
                    self.assertEqual(set(json.loads(body)), {"error"})
                    if expected != 404:
                        self.assertEqual(json.loads(body)["error"], str(error))
                    self.assertNotIn(b"/private", body)

    def test_unexpected_errors_are_redacted_and_head_has_no_body(self):
        self.app.error = Exception("private-secret /Users/private/map.json")
        for method in ("GET", "HEAD", "PUT"):
            with self.subTest(method=method):
                status, _, body = self.json_request(
                    method, self.map_path, {} if method == "PUT" else None)
                self.assertEqual(status, 500)
                self.assertNotIn(b"private-secret", body)
                self.assertNotIn(b"/Users", body)
                if method == "HEAD":
                    self.assertEqual(body, b"")

    def test_origin_checks_run_before_reading_or_saving_map(self):
        forbidden = [
            {"Host": "attacker.example"}, {"Host": "127.0.0.1:1"},
            {"Origin": "https://attacker.example"}, {"Origin": "null"},
            {"Origin": f"http://localhost:{self.server.server_port}"},
            {"Sec-Fetch-Site": "cross-site"}, {"Sec-Fetch-Site": "same-site"},
            {"Referer": "https://attacker.example/page"},
        ]
        for method in ("GET", "HEAD", "PUT"):
            for headers in forbidden:
                with self.subTest(method=method, headers=headers):
                    status, _, _ = self.json_request(
                        method, self.map_path, {} if method == "PUT" else None, headers)
                    self.assertEqual(status, 403)
        self.assertEqual(self.app.calls, [])
        allowed = {"Origin": f"http://{self.host}", "Sec-Fetch-Site": "same-origin",
                   "Referer": f"http://{self.host}/"}
        for method in ("GET", "HEAD", "PUT"):
            self.assertEqual(self.json_request(
                method, self.map_path, {} if method == "PUT" else None, allowed)[0], 200)

    def test_invalid_json_and_body_limits_do_not_dispatch(self):
        cases = [
            (b"invalid", {}, 400), (b"\xff", {}, 400), (b"[]", {}, 400),
            (b"null", {}, 400), (b"{}{}", {}, 400),
            (b'{"version": NaN}', {}, 400), (b'{"version": Infinity}', {}, 400),
            (b'{"version": -Infinity}', {}, 400),
            (b"{}", {"Content-Type": "text/plain"}, 415),
            (b"{}", {"Content-Length": str(MAX_BODY_BYTES + 1)}, 413),
            (b"{}", {"Transfer-Encoding": "chunked"}, 400),
        ]
        for body, headers, expected in cases:
            with self.subTest(body=body, headers=headers):
                status, _, _ = self.request("PUT", self.map_path, body,
                                           {"Content-Type": "application/json", **headers})
                self.assertEqual(status, expected)
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
                status, _ = self.raw_request("PUT", self.map_path, headers)
                self.assertEqual(status, expected)
        status, _ = self.raw_request("PUT", self.map_path, "Content-Length: 100\r\n",
                                     b"{}", close_write=True)
        self.assertEqual(status, 400)
        self.assertEqual(self.app.calls, [])

    def test_unsupported_methods_paths_and_traversal_do_not_dispatch(self):
        cases = [
            ("DELETE", self.map_path, 405), ("PATCH", self.map_path, 405),
            ("OPTIONS", self.map_path, 405), ("POST", self.map_path, 404),
            ("GET", "/api/papers/%2e%2e/map", 404),
            ("PUT", "/api/papers/%2e%2e/map", 404),
            ("GET", self.map_path + "/extra", 404),
            ("PUT", self.map_path + "/extra", 405),
        ]
        for method, path, expected in cases:
            with self.subTest(method=method, path=path):
                self.assertEqual(self.json_request(method, path, {})[0], expected)
        self.assertEqual(self.app.calls, [])


class M3HttpIntegrationTests(unittest.TestCase):
    # Reuse the isolated real-Application fixture, never the integration tests.
    setUp = integration.ServerIntegrationTests.setUp
    start = integration.ServerIntegrationTests.start
    stop = integration.ServerIntegrationTests.stop
    stop_all = integration.ServerIntegrationTests.stop_all
    request = integration.ServerIntegrationTests.request
    json_request = integration.ServerIntegrationTests.json_request

    def test_real_application_map_round_trip_conflict_and_same_origin(self):
        path = f"/api/papers/{integration.PAPER}/map"
        empty = {"version": 0, "nodes": [], "notes": []}
        self.assertEqual(self.json_request("GET", path), empty)
        payload = {"version": 0, "nodes": [{"name": "绕数", "x": 880, "y": 790}],
                   "notes": [{"id": "n-1", "x": 652, "y": 520, "text": "回头再看。"}]}
        saved = self.json_request("PUT", path, payload)
        self.assertEqual(saved, {**payload, "version": 1})
        self.assertEqual(self.json_request("GET", path), saved)
        self.assertEqual(self.json_request("PUT", path, saved), saved)
        self.json_request("PUT", path, payload, expected=409)
        self.assertEqual(self.json_request("GET", path), saved)

        forbidden = {"version": 1, "nodes": [], "notes": []}
        status, _, body = self.request("PUT", path, forbidden,
                                       headers={"Origin": "https://attacker.example"})
        self.assertEqual(status, 403)
        self.assertIn("error", json.loads(body))
        self.assertEqual(self.json_request("GET", path), saved)
        # Unknown papers retain the existing Application._load validation status.
        self.json_request("GET", "/api/papers/missing/map", expected=400)
        self.json_request("PUT", "/api/papers/missing/map", empty, expected=400)
        self.assertEqual(self.backend.prompts, [])
        self.assertEqual(self.app.tasks, {})
        self.assertEqual(self.client.attempts, [])

        self.stop(self.resources[0])
        restarted_backend = integration.FakeBackend()
        self.start(restarted_backend)
        self.assertEqual(self.json_request("GET", path), saved)
        self.assertEqual(restarted_backend.prompts, [])
        self.assertEqual(self.client.attempts, [])


if __name__ == "__main__":
    unittest.main()
