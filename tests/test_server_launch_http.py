"""HTTP contracts for settings, saved papers, and uploads, using a fake app."""
from contextlib import contextmanager
import http.client
import json
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import quote

from server.http import MAX_BODY_BYTES, MAX_UPLOAD_BYTES, create_server


class LaunchApp:
    def __init__(self):
        self.backend = "claude"
        self.calls = []
        self.error = None
        self.papers = [{"paper": "paper-1", "title": "A saved paper", "authors": [],
                        "source": "arxiv_html", "explanations": 2, "last_read": None}]

    def health(self):
        return {"version": "test", "backend": self.backend}

    def settings(self):
        self.calls.append(("settings",))
        if self.error:
            raise self.error
        return {"backend": self.backend, "backends": [
            {"name": name, "available": True, "configured": True, "authenticated": None}
            for name in ("claude", "codex", "api")
        ], "api": {"base_url": "https://example.test/v1", "model": "fake",
                   "key_configured": True}}

    def update_settings(self, body):
        self.calls.append(("update", body))
        if self.error:
            raise self.error
        if set(body) != {"backend"} or body["backend"] not in {"claude", "codex", "api"}:
            raise ValueError("Choose a supported backend and no other settings.")
        self.backend = body["backend"]
        return self.settings()

    def list_papers(self):
        self.calls.append(("papers",))
        if self.error:
            raise self.error
        return {"papers": self.papers}

    def import_uploaded_pdf(self, path, filename=None):
        self.calls.append(("upload", path, filename, path.read_bytes()))
        if self.error:
            raise self.error
        return {"paper": "uploaded-paper", "title": "Uploaded paper", "source": "pdf", "source_url": None}


class LaunchHttpTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.upload_root = Path(self.directory.name) / "temporary-uploads"
        self.upload_root.mkdir()
        self.upload_created = threading.Event()
        self.upload_cleaned = threading.Event()
        temporary_directory = tempfile.TemporaryDirectory

        @contextmanager
        def upload_directory(**kwargs):
            try:
                with temporary_directory(dir=self.upload_root, **kwargs) as directory:
                    self.upload_created.set()
                    yield directory
            finally:
                self.upload_cleaned.set()

        temporary_patch = patch("server.http.tempfile.TemporaryDirectory", side_effect=upload_directory)
        temporary_patch.start()
        self.addCleanup(temporary_patch.stop)
        self.app = LaunchApp()
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

    def put(self, body, headers=None):
        return self.request("PUT", "/api/settings", json.dumps(body).encode(),
                            {"Content-Type": "application/json", **(headers or {})})

    def upload(self, body=b"%PDF-1.7\nfixture\n", headers=None):
        return self.request("POST", "/api/papers/upload", body,
                            {"Content-Type": "application/pdf", **(headers or {})})

    def raw_headers(self, *, path="/api/papers/upload", length=100, extra="", method="POST",
                    content_type="application/pdf"):
        length_header = f"Content-Length: {length}\r\n" if length is not None else ""
        return (f"{method} {path} HTTP/1.1\r\nHost: {self.host}\r\n"
                f"Content-Type: {content_type}\r\n{length_header}{extra}\r\n").encode()

    def test_settings_list_and_backend_switch_are_routed(self):
        status, headers, body = self.request("GET", "/api/settings")
        self.assertEqual(status, 200)
        settings = json.loads(body)
        self.assertEqual(settings["backend"], "claude")
        self.assertEqual([entry["name"] for entry in settings["backends"]], ["claude", "codex", "api"])
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(self.put({"backend": "codex"})[0], 200)
        self.assertEqual(json.loads(self.request("GET", "/api/health")[2])["backend"], "codex")
        status, _, body = self.request("GET", "/api/papers")
        self.assertEqual((status, json.loads(body)), (200, {"papers": self.app.papers}))

    def test_put_preserves_service_validation_and_conflict_statuses(self):
        for payload in ({}, {"backend": "invalid"}, {"backend": "api", "api_key": "private-secret"}):
            with self.subTest(payload=payload):
                status, _, body = self.put(payload)
                self.assertEqual(status, 400)
                self.assertNotIn(b"private-secret", body)
        self.assertEqual(self.app.backend, "claude")
        for error, expected in ((ValueError("Codex is not available."), 400),
                                (RuntimeError("An explanation is being generated."), 409)):
            with self.subTest(error=error):
                self.app.error = error
                status, _, body = self.put({"backend": "codex"})
                self.assertEqual(status, expected)
                self.assertEqual(json.loads(body), {"error": str(error)})

    def test_put_is_same_origin_before_reading_body(self):
        denied = [
            {"Host": "attacker.example"}, {"Origin": "https://attacker.example"},
            {"Origin": "null"}, {"Sec-Fetch-Site": "cross-site"},
            {"Referer": "https://attacker.example/settings"},
        ]
        for headers in denied:
            with self.subTest(headers=headers):
                self.assertEqual(self.put({"backend": "codex"}, headers)[0], 403)
        with socket.create_connection(self.server.server_address, timeout=2) as connection:
            connection.sendall((f"PUT /api/settings HTTP/1.1\r\nHost: {self.host}\r\n"
                                "Origin: https://attacker.example\r\nContent-Length: 100\r\n"
                                "Content-Type: application/json\r\n\r\n").encode())
            response = http.client.HTTPResponse(connection)
            response.begin()
            self.assertEqual(response.status, 403)
            response.read()
        self.assertEqual(self.app.calls, [])
        allowed = {"Origin": f"http://{self.host}", "Sec-Fetch-Site": "same-origin"}
        self.assertEqual(self.put({"backend": "api"}, allowed)[0], 200)

    def test_new_get_routes_enforce_origin_and_head_has_no_body(self):
        for path in ("/api/settings", "/api/papers"):
            with self.subTest(path=path):
                self.assertEqual(self.request("GET", path, headers={"Origin": "null"})[0], 403)
                status, headers, body = self.request("HEAD", path)
                self.assertEqual((status, body), (200, b""))
                self.assertGreater(int(headers["Content-Length"]), 0)

    def test_put_retains_json_body_limits_and_object_requirement(self):
        cases = [
            (b"[]", {"Content-Type": "application/json"}, 400),
            (b"invalid", {"Content-Type": "application/json"}, 400),
            (b"{}", {"Content-Type": "application/pdf"}, 415),
            (b"{}", {"Content-Type": "application/json", "Content-Length": str(MAX_BODY_BYTES + 1)}, 413),
            (b"{}", {"Content-Type": "application/json", "Transfer-Encoding": "chunked"}, 400),
        ]
        for body, headers, expected in cases:
            with self.subTest(body=body, headers=headers):
                self.assertEqual(self.request("PUT", "/api/settings", body, headers)[0], expected)
        self.assertEqual(self.app.calls, [])

    def test_put_does_not_dispatch_other_read_routes(self):
        for path in ("/api/papers", "/api/health", "/api/papers/paper-1", "/index.html"):
            with self.subTest(path=path):
                self.assertEqual(self.request("PUT", path)[0], 405)
        self.assertEqual(self.app.calls, [])

    def test_new_routes_redact_unexpected_errors(self):
        self.app.error = Exception("private-secret /Users/private/config.json /usr/local/bin/codex")
        for method, path in (("GET", "/api/settings"), ("GET", "/api/papers"), ("PUT", "/api/settings")):
            with self.subTest(method=method, path=path):
                status, _, body = self.request(method, path, b'{"backend":"codex"}' if method == "PUT" else None,
                                               {"Content-Type": "application/json"})
                self.assertEqual(status, 500)
                for private in (b"private-secret", b"/Users", b"/usr/local"):
                    self.assertNotIn(private, body)

    def test_upload_imports_pdf_then_removes_temporary_file(self):
        data = b"%PDF-1.7\nfixture\n"
        status, _, response = self.upload(data)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(response)["paper"], "uploaded-paper")
        kind, path, filename, saved = self.app.calls[0]
        self.assertEqual((kind, filename, saved), ("upload", None, data))
        self.assertFalse(path.exists())
        self.assertEqual(list(self.upload_root.iterdir()), [])
        self.assertNotIn(str(path).encode(), response)

    def test_upload_decodes_utf8_name_and_discards_client_paths(self):
        for original in ("论文.pdf", "/Users/private/论文.pdf", r"C:\Users\private\论文.pdf", "../../论文.pdf"):
            with self.subTest(original=original):
                self.assertEqual(self.upload(headers={"X-Paper-Name": quote(original, safe="")})[0], 200)
                self.assertEqual(self.app.calls[-1][2], "论文.pdf")
                self.assertEqual(self.app.calls[-1][1].name, "source.pdf")
        self.assertEqual(list(self.upload_root.iterdir()), [])

    def test_upload_rejects_invalid_names_before_creating_files(self):
        for name in ("%FF.pdf", "%zz.pdf", "%", "%00secret.pdf", "..", "%2FUsers%2F", "%0Asecret.pdf"):
            with self.subTest(name=name):
                self.assertEqual(self.upload(headers={"X-Paper-Name": name})[0], 400)
        self.assertFalse(self.upload_created.is_set())
        self.assertEqual(self.app.calls, [])

    def test_upload_requires_pdf_type_and_strict_leading_signature(self):
        for data, headers in ((b"%PDF-1.7", {"Content-Type": "text/plain"}),
                              (b"%PDF-1.7", {"Content-Type": "application/json"}),
                              (b"%PDF", {}), (b"", {}), (b" %PDF-1.7", {}),
                              (b"\xef\xbb\xbf%PDF-1.7", {}), (b"not a pdf", {})):
            with self.subTest(data=data, headers=headers):
                status, _, body = self.upload(data, headers)
                self.assertEqual(status, 400)
                self.assertEqual(list(self.upload_root.iterdir()), [])
        self.assertEqual(self.app.calls, [])

    def test_upload_is_bounded_before_body_read_and_no_other_route_is_relaxed(self):
        cases = [("/api/papers/upload", MAX_UPLOAD_BYTES + 1, "", 413),
                 ("/api/papers/upload", None, "", 411),
                 ("/api/papers/upload", -1, "", 400),
                 ("/api/papers/upload", 100, "Transfer-Encoding: chunked\r\n", 400),
                 ("/api/papers/upload", 100, "Content-Length: 101\r\n", 400),
                 ("/api/papers", MAX_BODY_BYTES + 1, "", 413)]
        for path, length, extra, expected in cases:
            with self.subTest(path=path, length=length, extra=extra):
                with socket.create_connection(self.server.server_address, timeout=2) as connection:
                    connection.sendall(self.raw_headers(path=path, length=length, extra=extra))
                    response = http.client.HTTPResponse(connection)
                    response.begin()
                    self.assertEqual(response.status, expected)
                    response.read()
        self.assertFalse(self.upload_created.is_set())
        self.assertEqual(self.app.calls, [])

    def test_upload_larger_than_json_limit_streams_in_bounded_chunks(self):
        data = b"%PDF-1.7\n" + b"x" * MAX_BODY_BYTES
        reads = []
        handler = self.server.RequestHandlerClass
        setup = handler.setup

        class RecordingReader:
            def __init__(self, source):
                self.source = source

            def read(self, size=-1):
                reads.append(size)
                return self.source.read(size)

            def __getattr__(self, name):
                return getattr(self.source, name)

        def record_body_reads(request):
            setup(request)
            request.rfile = RecordingReader(request.rfile)

        with patch.object(handler, "setup", record_body_reads):
            self.assertEqual(self.upload(data)[0], 200)
        self.assertEqual(self.app.calls[0][3], data)
        self.assertEqual(sum(reads), len(data))
        self.assertTrue(all(0 < size <= 64 * 1024 for size in reads))
        self.assertEqual(list(self.upload_root.iterdir()), [])

    def test_upload_rejects_cross_origin_before_receiving_pdf(self):
        for extra in ("Origin: https://attacker.example\r\n", "Origin: null\r\n",
                      "Sec-Fetch-Site: cross-site\r\n", "Referer: https://attacker.example/\r\n"):
            with self.subTest(extra=extra):
                with socket.create_connection(self.server.server_address, timeout=2) as connection:
                    connection.sendall(self.raw_headers(extra=extra))
                    response = http.client.HTTPResponse(connection)
                    response.begin()
                    self.assertEqual(response.status, 403)
                    response.read()
        self.assertFalse(self.upload_created.is_set())
        self.assertEqual(self.app.calls, [])

    def test_upload_partial_eof_cleans_temporary_file(self):
        with socket.create_connection(self.server.server_address, timeout=2) as connection:
            connection.sendall(self.raw_headers() + b"%PDF-1.7\npartial")
            connection.shutdown(socket.SHUT_WR)
            response = http.client.HTTPResponse(connection)
            response.begin()
            self.assertEqual(response.status, 400)
            response.read()
        self.assertTrue(self.upload_cleaned.is_set())
        self.assertEqual(list(self.upload_root.iterdir()), [])
        self.assertEqual(self.app.calls, [])

    def test_upload_disconnection_cleans_temporary_file(self):
        with socket.create_connection(self.server.server_address, timeout=2) as connection:
            connection.sendall(self.raw_headers() + b"%PDF-1.7\npartial")
            self.assertTrue(self.upload_created.wait(timeout=1))
            connection.shutdown(socket.SHUT_RDWR)
        self.assertTrue(self.upload_cleaned.wait(timeout=1))
        self.assertEqual(list(self.upload_root.iterdir()), [])
        self.assertEqual(self.app.calls, [])

    def test_upload_timeout_cleans_temporary_file(self):
        with patch("server.http.SOCKET_TIMEOUT", 0.1):
            with socket.create_connection(self.server.server_address, timeout=2) as connection:
                connection.sendall(self.raw_headers() + b"%PDF-1.7\npartial")
                response = http.client.HTTPResponse(connection)
                response.begin()
                self.assertEqual(response.status, 408)
                response.read()
        self.assertTrue(self.upload_cleaned.is_set())
        self.assertEqual(list(self.upload_root.iterdir()), [])
        self.assertEqual(self.app.calls, [])

    def test_upload_import_failure_cleans_file_and_hides_details(self):
        self.app.error = Exception("private-secret /Users/private/upload.pdf")
        status, _, body = self.upload()
        self.assertEqual(status, 500)
        self.assertNotIn(b"private-secret", body)
        self.assertNotIn(b"/Users", body)
        self.assertEqual(list(self.upload_root.iterdir()), [])

    def test_upload_expect_continue_checks_origin_type_and_route_limit_first(self):
        cases = [("/api/papers/upload", MAX_UPLOAD_BYTES + 1, "", 413),
                 ("/api/papers/upload", 100, "Origin: null\r\n", 403),
                 ("/api/papers/upload", 100, "X-Paper-Name: %FF\r\n", 400),
                 ("/api/papers", MAX_BODY_BYTES + 1, "", 413)]
        for path, length, extra, expected in cases:
            with self.subTest(path=path, length=length, extra=extra):
                with socket.create_connection(self.server.server_address, timeout=2) as connection:
                    connection.sendall(self.raw_headers(path=path, length=length,
                                                        extra="Expect: 100-continue\r\n" + extra))
                    # Read the first status explicitly; HTTPResponse skips 100.
                    with connection.makefile("rb") as response:
                        self.assertIn(f" {expected} ".encode(), response.readline())
        with socket.create_connection(self.server.server_address, timeout=2) as connection:
            connection.sendall(self.raw_headers(content_type="application/json", extra="Expect: 100-continue\r\n"))
            with connection.makefile("rb") as response:
                self.assertIn(b" 400 ", response.readline())
        self.assertFalse(self.upload_created.is_set())
        self.assertEqual(self.app.calls, [])

    def test_upload_expect_continue_allows_pdf_over_json_limit(self):
        data = b"%PDF-1.7\n" + b"x" * MAX_BODY_BYTES
        with socket.create_connection(self.server.server_address, timeout=2) as connection:
            connection.sendall(self.raw_headers(length=len(data), extra="Expect: 100-continue\r\n"))
            with connection.makefile("rb") as interim:
                self.assertIn(b" 100 ", interim.readline())
                self.assertEqual(interim.readline(), b"\r\n")
            connection.sendall(data)
            response = http.client.HTTPResponse(connection)
            response.begin()
            self.assertEqual(response.status, 200)
            response.read()
        self.assertEqual(self.app.calls[0][3], data)
        self.assertEqual(list(self.upload_root.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
