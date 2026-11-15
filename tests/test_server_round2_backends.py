"""Per-task cancellation and safe, actionable Chinese adapter errors; offline."""

import http.server
import io
import json
import os
from pathlib import Path
import socketserver
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from urllib import error

from server import backends
from server.config import APIConfig, Config


SECRET = "sk-secret /Users/private/paper.pdf https://secret.example/token"


class RoundTwoBackendTests(unittest.TestCase):
    def worker(self, token, operation):
        results, failures = [], []
        def run():
            try:
                with backends.cancellation_context(token):
                    results.append(operation())
            except backends.BackendError as exc:
                failures.append(str(exc))
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return thread, results, failures

    def wait_for(self, condition, timeout=3):
        deadline = time.monotonic() + timeout
        while not condition() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(condition())

    def test_pre_cancelled_scope_never_starts_generation(self):
        token = backends.TaskCancellation()
        token.cancel()
        with mock.patch.object(backends.subprocess, "Popen") as spawn:
            with self.assertRaisesRegex(backends.BackendError, "已停止这一轮"):
                with backends.cancellation_context(token):
                    backends.CLIBackend(Config(home=Path("/unused")), "claude").generate("fixture", {})
        spawn.assert_not_called()
        token.cancel()  # Repeated cancellation is harmless.

    def test_failed_cleanup_is_retained_and_retried(self):
        token = backends.TaskCancellation()
        resource = object()
        stop = mock.Mock(side_effect=[OSError("synthetic cleanup failure"), None])
        token.register(resource, stop)
        with self.assertRaisesRegex(backends.BackendError, "未能完全停止"):
            token.cancel()
        self.assertIn(resource, token._resources)
        token.cancel()
        self.assertNotIn(resource, token._resources)
        self.assertEqual(stop.call_count, 2)

    def test_backend_close_retries_cleanup_after_worker_scope_has_exited(self):
        backend = backends.CLIBackend(Config(home=Path("/unused")), "claude")
        token = backends.TaskCancellation()
        stop = mock.Mock(side_effect=[OSError("synthetic cleanup failure"), None])
        with backends.cancellation_context(token):
            with backends._backend_scope(backend):
                token.register(object(), stop)
        with self.assertRaisesRegex(backends.BackendError, "未能完全停止"):
            backend.close()
        self.assertIn(token, backend._scopes)
        backend.close()
        self.assertFalse(backend._scopes)
        self.assertEqual(stop.call_count, 2)

    @unittest.skipUnless(os.name == "posix", "POSIX subprocess group cleanup")
    def test_cancelling_one_task_kills_its_grandchild_and_keeps_shared_backend_usable(self):
        code = (
            "import os,signal,time\n"
            "child=os.fork()\n"
            "if child == 0:\n"
            " signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            " while True:\n"
            "  with open('heartbeat', 'a') as f: f.write('x'); f.flush()\n"
            "  time.sleep(0.02)\n"
            "else: time.sleep(30)\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backend = backends.CLIBackend(Config(home=root), "claude")
            first, second = backends.TaskCancellation(), backends.TaskCancellation()
            one, two = root / "one", root / "two"
            one.mkdir()
            two.mkdir()
            worker1 = self.worker(first, lambda: backend._run_cli([sys.executable, "-c", code], "", one, 10))
            worker2 = self.worker(second, lambda: backend._run_cli([sys.executable, "-c", code], "", two, 10))
            try:
                self.wait_for(lambda: (one / "heartbeat").exists() and (two / "heartbeat").exists())
                started = time.monotonic()
                first.cancel()
                self.assertLess(time.monotonic() - started, 2)
                worker1[0].join(timeout=2)
                self.assertFalse(worker1[0].is_alive())
                self.assertEqual(worker1[2], ["已停止这一轮。"])
                size1, size2 = (one / "heartbeat").stat().st_size, (two / "heartbeat").stat().st_size
                time.sleep(0.15)
                self.assertEqual((one / "heartbeat").stat().st_size, size1)
                self.assertGreater((two / "heartbeat").stat().st_size, size2)
                self.assertFalse(backend.cancelled.is_set())
                self.assertEqual(backend._run_cli([sys.executable, "-c", "print('ok')"], "", root, 3), b"ok\n")
            finally:
                backend.close()
                worker1[0].join(timeout=2)
                worker2[0].join(timeout=2)
            self.assertFalse(worker2[0].is_alive())
            self.assertFalse(first._resources)
            self.assertFalse(second._resources)

    def test_api_cancel_before_headers_and_during_body_does_not_stop_other_task(self):
        for stage in ("headers", "body"):
            with self.subTest(stage=stage):
                arrived = {name: threading.Event() for name in ("one", "two")}
                release = threading.Event()
                body = json.dumps({"choices": [{"message": {"content": '{"ok": true}'}, "finish_reason": "stop"}]}).encode()
                class Handler(http.server.BaseHTTPRequestHandler):
                    def do_POST(self):
                        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                        name = payload["messages"][1]["content"]
                        try:
                            if stage == "headers":
                                arrived[name].set()
                                release.wait(timeout=5)
                            self.send_response(200)
                            self.send_header("Content-Length", str(len(body)))
                            self.end_headers()
                            if stage == "body":
                                self.wfile.write(body[:1])
                                self.wfile.flush()
                                arrived[name].set()
                                release.wait(timeout=5)
                                self.wfile.write(body[1:])
                            else:
                                self.wfile.write(body)
                        except OSError:
                            pass
                    def log_message(self, *args):
                        pass
                server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
                serving = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
                serving.start()
                backend = backends.APIBackend(Config(home=Path("/unused"), backend="api", timeout_seconds=20,
                    api=APIConfig(f"http://127.0.0.1:{server.server_port}", "offline")))
                first, second = backends.TaskCancellation(), backends.TaskCancellation()
                worker1 = self.worker(first, lambda: backend.generate("one", {}))
                worker2 = self.worker(second, lambda: backend.generate("two", {}))
                try:
                    self.assertTrue(arrived["one"].wait(timeout=3))
                    self.assertTrue(arrived["two"].wait(timeout=3))
                    started = time.monotonic()
                    first.cancel()
                    worker1[0].join(timeout=2)
                    self.assertLess(time.monotonic() - started, 2)
                    self.assertFalse(worker1[0].is_alive())
                    self.assertEqual(worker1[2], ["已停止这一轮。"])
                    self.assertTrue(worker2[0].is_alive())
                    self.assertFalse(backend.cancelled.is_set())
                    release.set()
                    worker2[0].join(timeout=2)
                    self.assertEqual(worker2[1], [{"ok": True}])
                    self.assertEqual(worker2[2], [])
                finally:
                    release.set()
                    backend.close()
                    server.shutdown()
                    server.server_close()
                    serving.join(timeout=2)
                    worker1[0].join(timeout=2)
                    worker2[0].join(timeout=2)
                self.assertFalse(first._resources)
                self.assertFalse(second._resources)

    def test_api_cancel_interrupts_tls_handshake(self):
        arrived, release = threading.Event(), threading.Event()
        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                self.request.recv(4096)
                arrived.set()
                release.wait(timeout=5)
        server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        serving = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        serving.start()
        backend = backends.APIBackend(Config(home=Path("/unused"), backend="api", timeout_seconds=20,
            api=APIConfig(f"https://127.0.0.1:{server.server_address[1]}", "offline")))
        token = backends.TaskCancellation()
        worker = self.worker(token, lambda: backend.generate("fixture", {}))
        try:
            self.assertTrue(arrived.wait(timeout=3))
            token.cancel()
            worker[0].join(timeout=2)
            self.assertFalse(worker[0].is_alive())
            self.assertEqual(worker[2], ["已停止这一轮。"])
        finally:
            release.set()
            backend.close()
            server.shutdown()
            server.server_close()
            serving.join(timeout=2)
            worker[0].join(timeout=2)

    def test_api_response_ownership_has_no_gap_before_body_registration(self):
        opened, resume, disconnected = threading.Event(), threading.Event(), threading.Event()
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                self.send_response(200)
                self.send_header("Content-Length", "10000")
                self.end_headers()
                self.wfile.write(b"{")
                self.wfile.flush()
                self.connection.settimeout(3)
                try:
                    if not self.connection.recv(1):
                        disconnected.set()
                except OSError:
                    pass
            def log_message(self, *args):
                pass
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        serving = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        serving.start()
        backend = backends.APIBackend(Config(home=Path("/unused"), backend="api", timeout_seconds=20,
            api=APIConfig(f"http://127.0.0.1:{server.server_port}", "offline")))
        real_open = backend.opener.open
        def delayed_open(*args, **kwargs):
            response = real_open(*args, **kwargs)
            opened.set()
            resume.wait(timeout=3)
            return response
        token = backends.TaskCancellation()
        with mock.patch.object(backend.opener, "open", side_effect=delayed_open):
            worker = self.worker(token, lambda: backend.generate("fixture", {}))
            try:
                self.assertTrue(opened.wait(timeout=3))
                token.cancel()
                # The body reader is deliberately blocked before registering
                # the response; cancel must already have shut down its socket.
                self.assertTrue(disconnected.wait(timeout=1))
                resume.set()
                worker[0].join(timeout=2)
                self.assertFalse(worker[0].is_alive())
                self.assertEqual(worker[2], ["已停止这一轮。"])
                self.assertFalse(token.has_resources())
            finally:
                resume.set()
                backend.close()
                server.shutdown()
                server.server_close()
                serving.join(timeout=2)
                worker[0].join(timeout=2)
    def test_actionable_cli_error_classification_never_echoes_diagnostics(self):
        cases = (("Invalid model: ", "模型名可能不对"), ("Not logged in: ", "登录"),
                 ("unexpected argument: ", "版本太旧"), ("timed out: ", "超时"),
                 ("unknown error: ", "检查本机登录"))
        with tempfile.TemporaryDirectory() as directory:
            for prefix, expected in cases:
                with self.subTest(prefix=prefix):
                    code = f"import sys; sys.stderr.write({(prefix + SECRET)!r}); sys.exit(1)"
                    with self.assertRaisesRegex(backends.BackendError, expected) as caught:
                        backends._run_cli([sys.executable, "-c", code], "", Path(directory), 3)
                    self.assertNotIn(SECRET, str(caught.exception))
                    self.assertNotIn("/Users/", str(caught.exception))
                    self.assertNotIn("https://", str(caught.exception))

    def test_actionable_api_and_missing_cli_errors(self):
        backend = backends.APIBackend(Config(home=Path("/unused"), backend="api", max_retries=0,
            api=APIConfig("https://example.invalid/v1", "fixture", SECRET)))
        backend.opener = mock.Mock()
        for code, expected in ((401, "认证失败"), (403, "权限"), (404, "模型名可能不对"), (408, "超时")):
            with self.subTest(code=code):
                backend.opener.open.side_effect = error.HTTPError(SECRET, code, SECRET, {}, io.BytesIO(SECRET.encode()))
                with self.assertRaisesRegex(backends.BackendError, expected) as caught:
                    backend.generate("fixture", {})
                self.assertNotIn(SECRET, str(caught.exception))
        with mock.patch.object(backends.shutil, "which", return_value=None):
            with self.assertRaisesRegex(backends.BackendError, "先安装"):
                backends.CLIBackend(Config(home=Path("/unused")), "claude").generate("fixture", {})


if __name__ == "__main__":
    unittest.main()
