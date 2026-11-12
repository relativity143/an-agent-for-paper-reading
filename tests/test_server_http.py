"""Exercise the local HTTP boundary using loopback and an entirely fake app."""
from contextlib import redirect_stderr
import http.client
import io
import json
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest.mock import patch

from server.http import MAX_BODY_BYTES, create_server


class FakeApp:
    def __init__(self, root):
        self.calls = []
        self.pdf = root / "private-paper.pdf"
        self.pdf.write_bytes(b"%PDF-1.7\nfixture only\n")
        self.events = [None, (1, "status", "Reading\nChecking"),
                       (2, "result", {"id": "explanation-1", "intro": "A result"}),
                       (3, "result", {"id": "must-not-be-sent"})]
        self.error = None

    def health(self):
        return {"version": "test", "backend": "fake", "backends": ["fake"]}

    def import_paper(self, source):
        self.calls.append(("import", source))
        if self.error is not None:
            raise self.error
        return {"paper": "paper-1", "title": "Test paper"}

    def metadata(self, paper):
        return {"paper": paper, "outline": None}

    def content(self, paper):
        return {"paper": paper, "sections": []}

    def pdf_path(self, paper):
        return self.pdf

    def history(self, paper):
        return [{"id": "earlier", "paper": paper}]

    def explain(self, paper, request):
        self.calls.append(("explain", paper, request))
        return "task-1"

    def acknowledge(self, paper, explanation):
        self.calls.append(("displayed", paper, explanation))
        return {"id": explanation, "displayed": True}

    def get_task(self, task):
        if task != "task-1":
            raise FileNotFoundError("/private/task-cache")
        return object()

    def task_events(self, task, after=0):
        self.calls.append(("events", task, after))
        for event in self.events:
            if isinstance(event, Exception):
                raise event
            if event is None or event[0] > after:
                yield event


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.web = root / "web"
        self.web.mkdir()
        (self.web / "index.html").write_text("<h1>Local reader</h1>")
        (self.web / "app.js").write_text("export const local = true;")
        (self.web / "mock").mkdir()
        (self.web / "mock" / "fixture.json").write_text('{"mock":true}')
        self.app = FakeApp(root)
        self.server = create_server(self.app, port=0, web_root=self.web)
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

    def post(self, path, body, headers=None):
        return self.request("POST", path, json.dumps(body).encode(),
                            {"Content-Type": "application/json", **(headers or {})})

    def test_loopback_and_public_health(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")
        status, headers, body = self.request("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), self.app.health())
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")

    def test_json_routes_and_explicit_display_acknowledgement(self):
        self.assertEqual(self.post("/api/papers", {"input": "paper-1"})[0], 200)
        self.assertEqual(self.app.calls, [("import", "paper-1")])
        for path, field in (("/api/papers/paper-1", "outline"),
                            ("/api/papers/paper-1/content", "sections")):
            status, _, data = self.request("GET", path)
            self.assertEqual(status, 200)
            self.assertIn(field, json.loads(data))
        self.assertIsInstance(json.loads(self.request("GET", "/api/papers/paper-1/explanations")[2]), list)
        status, _, data = self.post("/api/papers/paper-1/explain", {"mode": "continue"})
        self.assertEqual((status, json.loads(data)), (202, {"task": "task-1"}))
        self.assertFalse(any(call[0] == "displayed" for call in self.app.calls))
        status, _, data = self.post("/api/papers/paper-1/explanations/explanation-1/displayed", {})
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(data)["displayed"])
        self.assertEqual(self.app.calls[-1], ("displayed", "paper-1", "explanation-1"))

    def test_origin_and_dns_rebinding_defenses_precede_mutations(self):
        forbidden = [
            {"Host": "attacker.example"}, {"Host": "127.0.0.1:1"},
            {"Origin": "https://attacker.example"}, {"Origin": "null"},
            {"Origin": f"http://localhost:{self.server.server_port}"},
            {"Sec-Fetch-Site": "cross-site"}, {"Sec-Fetch-Site": "same-site"},
            {"Referer": "https://attacker.example/page"},
        ]
        for headers in forbidden:
            with self.subTest(headers=headers):
                self.assertEqual(self.post("/api/papers", {"input": "paper-1"}, headers)[0], 403)
        self.assertEqual(self.app.calls, [])
        allowed = {"Origin": f"http://{self.host}", "Sec-Fetch-Site": "same-origin",
                   "Referer": f"http://{self.host}/"}
        self.assertEqual(self.post("/api/papers", {"input": "paper-1"}, allowed)[0], 200)
        self.assertEqual(self.request("GET", "/api/health", headers={"Host": f"localhost:{self.server.server_port}"})[0], 200)

    def test_duplicate_security_headers_are_rejected(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
        try:
            connection.putrequest("GET", "/api/health")
            connection.putheader("Host", "attacker.example")
            connection.endheaders()
            response = connection.getresponse()
            self.assertEqual(response.status, 400)
            response.read()
        finally:
            connection.close()

    def test_json_shape_size_and_framing_are_bounded(self):
        cases = [
            (b"{}", {"Content-Type": "text/plain"}, 415),
            (b"broken", {"Content-Type": "application/json"}, 400),
            (b"\xff", {"Content-Type": "application/json"}, 400),
            (b"[]", {"Content-Type": "application/json"}, 400),
            (b'{"input": NaN}', {"Content-Type": "application/json"}, 400),
            (b"{}", {"Content-Type": "application/json", "Content-Length": str(MAX_BODY_BYTES + 1)}, 413),
            (b"{}", {"Content-Type": "application/json", "Transfer-Encoding": "chunked"}, 400),
            (b"{}", {"Content-Type": "application/json", "Content-Length": "-1"}, 400),
        ]
        for body, headers, status in cases:
            with self.subTest(headers=headers, body=body):
                self.assertEqual(self.request("POST", "/api/papers", body, headers)[0], status)
        for body in ({}, {"input": 1}, {"input": "  "}):
            self.assertEqual(self.post("/api/papers", body)[0], 400)
        self.assertEqual(self.app.calls, [])

    def test_partial_request_body_times_out(self):
        with patch("server.http.SOCKET_TIMEOUT", 0.1):
            with socket.create_connection(self.server.server_address, timeout=2) as connection:
                request = (f"POST /api/papers HTTP/1.1\r\nHost: {self.host}\r\n"
                           "Content-Type: application/json\r\nContent-Length: 100\r\n\r\n{}")
                connection.sendall(request.encode())
                response = http.client.HTTPResponse(connection)
                response.begin()
                self.assertEqual(response.status, 408)
                response.read()

    def test_sse_framing_heartbeat_multiline_and_one_terminal_result(self):
        status, headers, body = self.request("GET", "/api/tasks/task-1/events")
        self.assertEqual(status, 200)
        self.assertIn("text/event-stream", headers["Content-Type"])
        self.assertIn(b": heartbeat\n\n", body)
        self.assertIn(b"id: 1\nevent: status\ndata: Reading\ndata: Checking\n\n", body)
        self.assertEqual(body.count(b"event: result"), 1)
        self.assertNotIn(b"must-not-be-sent", body)
        self.assertFalse(any(call[0] == "displayed" for call in self.app.calls))

    def test_sse_resume_and_missing_task_before_headers(self):
        status, _, body = self.request("GET", "/api/tasks/task-1/events", headers={"Last-Event-ID": "1"})
        self.assertEqual(status, 200)
        self.assertNotIn(b"event: status", body)
        self.assertIn(("events", "task-1", 1), self.app.calls)
        status, headers, body = self.request("GET", "/api/tasks/missing/events")
        self.assertEqual(status, 404)
        self.assertIn("application/json", headers["Content-Type"])
        self.assertNotIn(b"private", body)
        self.assertEqual(self.request("GET", "/api/tasks/task-1/events", headers={"Last-Event-ID": "-1"})[0], 400)

    def test_sse_error_is_terminal_and_unexpected_error_is_redacted(self):
        self.app.events = [(1, "error", "Please retry"), (2, "result", {})]
        body = self.request("GET", "/api/tasks/task-1/events")[2]
        self.assertIn(b"event: error\ndata: Please retry", body)
        self.assertNotIn(b"event: result", body)
        self.app.events = [Exception("token-secret /private/model-config")]
        body = self.request("GET", "/api/tasks/task-1/events")[2]
        self.assertIn(b"event: error", body)
        self.assertNotIn(b"token-secret", body)
        self.assertNotIn(b"model-config", body)

    def test_static_pdf_and_no_private_file_access(self):
        self.assertIn(b"Local reader", self.request("GET", "/")[2])
        status, headers, body = self.request("GET", "/app.js")
        self.assertEqual(status, 200)
        self.assertIn("javascript", headers["Content-Type"])
        self.assertEqual(self.request("GET", "/mock/fixture.json")[0], 200)
        status, headers, body = self.request("GET", "/api/papers/paper-1/pdf")
        self.assertEqual((status, headers["Content-Type"]), (200, "application/pdf"))
        self.assertEqual(body, self.app.pdf.read_bytes())
        outside = self.web.parent / "secret.js"
        outside.write_text("PRIVATE")
        (self.web / "escape.js").symlink_to(outside)
        for name in (".env", "private.py", "config.json", "paper.pdf"):
            (self.web / name).write_text("PRIVATE")
        for path in ("/../secret.js", "/%2e%2e/secret.js", "/%2e%2e%2fsecret.js",
                     "/escape.js", "/.env", "/private.py", "/config.json", "/paper.pdf",
                     "/.git/config", "/%5csecret.js", "/app.js%00"):
            with self.subTest(path=path):
                status, _, body = self.request("GET", path)
                self.assertEqual(status, 404)
                self.assertNotIn(b"PRIVATE", body)
        self.assertEqual(self.request("HEAD", "/app.js")[2], b"")

    def test_missing_web_shows_startup_hint(self):
        (self.web / "index.html").unlink()
        status, _, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"server is running", body)
        self.assertNotIn(str(self.web).encode(), body)

    def test_error_mapping_hides_unexpected_details_and_request_logs(self):
        errors = [(ValueError("Invalid paper"), 400),
                  (FileNotFoundError("/private/paper"), 404),
                  (RuntimeError("Already running"), 409),
                  (Exception("token-secret /private/config"), 500)]
        log = io.StringIO()
        with redirect_stderr(log):
            for error, code in errors:
                self.app.error = error
                status, headers, body = self.post("/api/papers", {"input": "private-quote"})
                self.assertEqual(status, code)
                self.assertIn("application/json", headers["Content-Type"])
                self.assertNotIn(b"/private", body)
                self.assertNotIn(b"token-secret", body)
        self.assertEqual(log.getvalue(), "")
        self.assertEqual(self.request("GET", "/api/no-such-route")[0], 404)
        self.assertEqual(self.request("OPTIONS", "/api/health")[0], 405)


if __name__ == "__main__":
    unittest.main()
