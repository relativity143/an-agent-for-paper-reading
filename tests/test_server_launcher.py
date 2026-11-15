"""Offline launcher checks: loopback sockets, mocked browsers, owned subprocesses."""
import contextlib
import errno
import http.server
import io
import json
import os
from pathlib import Path
import re
import select
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from server import __version__, launcher
from server.__main__ import main
from server.backends import APIBackend, BackendError, CLIBackend
from server.config import APIConfig, Config
from server.http import create_server

ROOT = Path(__file__).resolve().parents[1]


class HealthApp:
    info = {"version": __version__, "backend": "claude", "backends": []}

    def health(self):
        return dict(self.info)


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="paperread-launch-tests-")
        self.addCleanup(self.directory.cleanup)
        self.home = Path(self.directory.name)

    def run_http(self, app):
        server = create_server(app, port=0)
        thread = threading.Thread(target=server.serve_forever,
                                  kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()

        def close():
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.addCleanup(close)
        return server

    def test_health_recognizes_our_server_without_using_proxy(self):
        server = self.run_http(HealthApp())
        with mock.patch.dict(os.environ, {"HTTP_PROXY": "http://127.0.0.1:1", "NO_PROXY": ""}):
            self.assertEqual(launcher.existing_service(server.server_port), HealthApp.info)

    def test_health_rejects_fixtures_and_unknown_versions(self):
        app = HealthApp()
        server = self.run_http(app)
        for info in ({"version": __version__, "backend": "fixture", "backends": []},
                     {"version": "unknown", "backend": "claude", "backends": []},
                     {"version": __version__, "backend": "claude"}):
            with self.subTest(info=info):
                app.info = info
                self.assertIsNone(launcher.existing_service(server.server_port))

    def test_health_rejects_an_unbranded_json_server(self):
        class OtherHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = json.dumps(HealthApp.info).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), OtherHandler)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        try:
            self.assertIsNone(launcher.existing_service(server.server_port))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_reuses_running_service_without_constructing_an_application(self):
        server = self.run_http(HealthApp())
        with mock.patch("server.__main__.Application", side_effect=AssertionError("No second service")), \
                mock.patch("server.__main__.open_page") as opened, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["--port", str(server.server_port), "--open"]), 0)
        opened.assert_called_once_with(f"http://127.0.0.1:{server.server_port}")
        self.assertIn("原来的服务窗口", output.getvalue())
        self.assertIn("claude", output.getvalue())

    def test_unknown_listener_is_left_alone_and_another_port_is_reserved(self):
        with socket.socket() as other:
            other.bind(("127.0.0.1", 0))
            other.listen()
            port = other.getsockname()[1]
            self.assertIsNone(launcher.existing_service(port))
            server = launcher.bind_server(HealthApp(), port)
            try:
                self.assertNotEqual(server.server_port, port)
                self.assertEqual(other.getsockname()[1], port)
                with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                    pass
            finally:
                server.server_close()

    def test_non_port_errors_are_not_hidden_by_a_retry(self):
        with mock.patch.object(launcher, "create_server", side_effect=PermissionError(errno.EACCES, "denied")) as bind:
            with self.assertRaises(PermissionError):
                launcher.bind_server(HealthApp(), 8765)
        self.assertEqual(bind.call_count, 1)

    def test_browser_failure_reports_the_actual_url(self):
        with mock.patch.object(launcher.webbrowser, "open", return_value=False), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            launcher.open_page("http://127.0.0.1:12345")
        self.assertIn("请手动访问：http://127.0.0.1:12345", output.getvalue())

    def test_finder_script_changes_directory_and_clears_inherited_test_home(self):
        root = self.home / "repository with spaces"
        root.mkdir()
        script = root / "start.command"
        script.write_text((ROOT / "start.command").read_text())
        script.chmod(0o755)
        python = root / ".venv" / "bin" / "python3"
        python.parent.mkdir(parents=True)
        python.write_text('#!/bin/bash\nif [ "$1" = -c ]; then exit 0; fi\nprintf "%s\\n" "$PWD" "${PAPERREAD_HOME-unset}" "$*"\n')
        python.chmod(0o755)
        environment = dict(os.environ, PAPERREAD_HOME="/must-not-use-test-home")
        completed = subprocess.run([str(script)], cwd=self.home, env=environment,
                                   text=True, capture_output=True, timeout=5)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.splitlines(), [str(root), "unset", "-m server --open"])
        self.assertTrue(os.access(ROOT / "start.command", os.X_OK))

    def test_finder_script_skips_an_unsuitable_python(self):
        root = self.home / "repository"
        root.mkdir()
        script = root / "start.command"
        script.write_text((ROOT / "start.command").read_text())
        script.chmod(0o755)
        old = root / ".venv" / "bin" / "python3"
        old.parent.mkdir(parents=True)
        old.write_text("#!/bin/bash\nexit 1\n")
        old.chmod(0o755)
        usable = self.home / "bin" / "python3"
        usable.parent.mkdir()
        usable.write_text('#!/bin/bash\nif [ "$1" = -c ]; then exit 0; fi\nprintf "suitable-python\\n"\n')
        usable.chmod(0o755)
        completed = subprocess.run([str(script)], cwd=self.home,
                                   env=dict(os.environ, PATH=str(usable.parent)),
                                   text=True, capture_output=True, timeout=5)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), "suitable-python")


@unittest.skipUnless(os.name == "posix", "Terminal signals and process groups require POSIX")
class LauncherProcessTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="paperread-launch-process-")
        self.addCleanup(self.directory.cleanup)
        self.home = Path(self.directory.name)
        self.processes = []
        self.addCleanup(self.stop_processes)

    def stop_processes(self):
        for process in self.processes:
            if process.poll() is None:
                process.terminate()
            try:
                process.communicate(timeout=6)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=3)

    @staticmethod
    def free_port():
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]

    def start(self, setup="", port=None):
        port = port or self.free_port()
        code = (
            "import os,sys,threading,time\n"
            "from pathlib import Path\n"
            "from server import launcher\n"
            "from server.__main__ import Application, main\n"
            "def open_browser(url):\n"
            " target = Path(os.environ['PAPERREAD_HOME']) / 'opened-url'\n"
            " temporary = target.with_suffix('.tmp')\n"
            " temporary.write_text(url)\n"
            " temporary.replace(target)\n"
            " return True\n"
            "launcher.webbrowser.open = open_browser\n"
            + setup
            + f"\nraise SystemExit(main(['--port', '{port}', '--open'], allow_force_exit=True))\n"
        )
        process = subprocess.Popen([sys.executable, "-c", code], cwd=ROOT,
                                   env=dict(os.environ, PAPERREAD_HOME=str(self.home), PYTHONDONTWRITEBYTECODE="1"),
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
        self.processes.append(process)
        output = b""
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline and b"Ctrl+C" not in output:
            ready, _, _ = select.select([process.stdout], [], [], 0.1)
            if ready:
                chunk = os.read(process.stdout.fileno(), 4096)
                if not chunk:
                    break
                output += chunk
            if process.poll() is not None:
                break
        message = output.decode()
        self.assertIn("服务已启动", message, message)
        url = re.search(r"http://127\.0\.0\.1:(\d+)", message).group(0)
        self.assertIn("当前模型后端：claude", message)
        self.assertIn("关掉这个窗口", message)
        deadline = time.monotonic() + 2
        while not (self.home / "opened-url").exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual((self.home / "opened-url").read_text(), url)
        return process, url

    def assert_stopped(self, process, url, signum):
        process.send_signal(signum)
        output, _ = process.communicate(timeout=6)
        self.assertEqual(process.returncode, 0, output.decode())
        self.assertFalse((self.home / ".server.lock").exists())
        with socket.socket() as sock:
            self.assertNotEqual(sock.connect_ex(("127.0.0.1", int(url.rsplit(":", 1)[1]))), 0)

    def test_terminal_and_interrupt_signals_release_port_and_data_lock(self):
        for signum in (signal.SIGHUP, signal.SIGTERM, signal.SIGINT):
            with self.subTest(signal=signum):
                (self.home / "opened-url").unlink(missing_ok=True)
                process, url = self.start()
                self.assert_stopped(process, url, signum)

    def test_an_occupied_port_prints_and_opens_the_actual_fallback_address(self):
        with socket.socket() as other:
            other.bind(("127.0.0.1", 0))
            other.listen()
            port = other.getsockname()[1]
            process, url = self.start(port=port)
            self.assertNotEqual(int(url.rsplit(":", 1)[1]), port)
            self.assert_stopped(process, url, signal.SIGHUP)
            self.assertEqual(other.getsockname()[1], port)

    def test_window_close_stops_a_model_group_including_its_grandchild(self):
        child_code = (
            "import os,signal,time\n"
            "if os.fork() == 0:\n"
            " signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            " while True:\n"
            "  with open('heartbeat', 'a') as f: f.write('x'); f.flush()\n"
            "  time.sleep(0.02)\n"
            "else:\n"
            " time.sleep(60)\n"
        )
        setup = (
            "original_init = Application.__init__\n"
            "def initialize(self, *args, **kwargs):\n"
            " original_init(self, *args, **kwargs)\n"
            f" self.futures.add(self.pool.submit(self.backend._run_cli, [sys.executable, '-c', {child_code!r}], '', self.store.home, 60))\n"
            "Application.__init__ = initialize\n"
        )
        process, url = self.start(setup)
        heartbeat = self.home / "heartbeat"
        deadline = time.monotonic() + 3
        while not heartbeat.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(heartbeat.exists())
        self.assert_stopped(process, url, signal.SIGHUP)
        length = heartbeat.stat().st_size
        time.sleep(0.15)
        self.assertEqual(heartbeat.stat().st_size, length)

    def test_stalled_worker_cannot_keep_standalone_server_alive_after_window_close(self):
        setup = (
            "original_init = Application.__init__\n"
            "def initialize(self, *args, **kwargs):\n"
            " original_init(self, *args, **kwargs)\n"
            " self.futures.add(self.pool.submit(threading.Event().wait))\n"
            "Application.__init__ = initialize\n"
        )
        process, url = self.start(setup)
        started = time.monotonic()
        self.assert_stopped(process, url, signal.SIGHUP)
        self.assertLess(time.monotonic() - started, 5)


class BackendCancellationTests(unittest.TestCase):
    def test_cancelling_one_backend_does_not_cancel_another(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Config(home=Path(directory))
            closed = CLIBackend(config, "claude")
            other = CLIBackend(config, "claude")
            closed.close()
            with self.assertRaisesRegex(BackendError, "已停止这一轮"):
                closed.generate("prompt", {})
            self.assertEqual(other._run_cli([sys.executable, "-c", "print('ok')"], "", config.home, 3), b"ok\n")
            other.close()

    def test_api_close_unblocks_a_response_without_waiting_for_the_model_timeout(self):
        release = threading.Event()

        class StalledResponse(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                self.send_response(200)
                self.send_header("Content-Length", "10000")
                self.end_headers()
                self.wfile.write(b"{")
                self.wfile.flush()
                release.wait(timeout=5)

            def log_message(self, *args):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), StalledResponse)
        serving = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        serving.start()
        config = Config(home=Path("/unused"), backend="api", timeout_seconds=30,
                        api=APIConfig(f"http://127.0.0.1:{server.server_port}", "offline-fixture"))
        backend = APIBackend(config)
        failures = []

        def generate():
            try:
                backend.generate("synthetic prompt", {})
            except BackendError as error:
                failures.append(str(error))

        worker = threading.Thread(target=generate, daemon=True)
        worker.start()
        try:
            deadline = time.monotonic() + 2
            while not backend._responses and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(backend._responses)
            backend.close()
            worker.join(timeout=2)
            self.assertFalse(worker.is_alive())
            self.assertTrue(failures)
        finally:
            release.set()
            backend.close()
            server.shutdown()
            server.server_close()
            serving.join(timeout=2)
            worker.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
