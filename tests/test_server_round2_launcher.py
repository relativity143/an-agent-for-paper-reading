"""Offline discovery regressions, including real isolated launcher lifecycles."""
import contextlib
import io
import json
import os
from pathlib import Path
import re
import select
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from server import __version__, launcher
from server.__main__ import main
from server.http import create_server

ROOT = Path(__file__).resolve().parents[1]


class HealthApp:
    def health(self):
        return {"version": __version__, "backend": "claude", "backends": []}


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="paperread-discovery-")
        self.addCleanup(self.directory.cleanup)
        self.home = Path(self.directory.name).resolve()
        self.lock = self.home / ".server.lock"
        self.lock.write_text(str(os.getpid()))
        self.server = create_server(HealthApp(), port=0)
        self.record = launcher.publish_service(self.server, self.home)
        self.path = self.home / launcher.SERVICE_RECORD
        thread = threading.Thread(target=self.server.serve_forever,
                                  kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()

        def stop():
            self.server.shutdown()
            self.server.server_close()
            thread.join(timeout=2)

        self.addCleanup(stop)

    def change_record(self, **fields):
        record = dict(self.record, **fields)
        self.path.write_text(json.dumps(record))

    def test_discovers_actual_address_with_private_metadata_and_health_identity(self):
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(launcher.recorded_service(self.home),
                         (self.record["url"], HealthApp().health()))
        self.assertEqual(launcher.existing_service(self.server.server_port), HealthApp().health())

    def test_second_launch_opens_actual_address_without_creating_application(self):
        with mock.patch.dict(os.environ, {"PAPERREAD_HOME": str(self.home)}), \
                mock.patch("server.__main__.Application", side_effect=AssertionError("duplicate")), \
                mock.patch("server.__main__.open_page") as opened, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["--port", "1", "--open"]), 0)
        opened.assert_called_once_with(self.record["url"])
        self.assertIn("服务已在运行", output.getvalue())
        self.assertEqual(json.loads(self.path.read_text()), self.record)

    def test_record_must_match_home_pid_lock_and_version(self):
        for fields in ({"home": str(self.home / "other")}, {"pid": os.getpid() + 1},
                       {"pid": True}, {"pid": 0}, {"version": "other"},
                       {"instance": "bad"}, {"url": None}):
            with self.subTest(fields=fields):
                self.change_record(**fields)
                with mock.patch.object(launcher, "existing_service") as health:
                    self.assertIsNone(launcher.recorded_service(self.home))
                health.assert_not_called()

    def test_untrusted_urls_are_never_requested_or_opened(self):
        for url in ("https://example.com/", "http://localhost:8765", "file:///tmp/page",
                    "http://127.0.0.1:8765@evil.invalid", "http://127.0.0.1:8765/path",
                    "http://127.0.0.1:8765?redirect=evil", "http://127.0.0.1:8765#x",
                    "http://127.0.0.1:65536", "http://127.0.0.1:0",
                    "http://127.0.0.1:8765\n", "http://[::1]:8765"):
            with self.subTest(url=url):
                self.change_record(url=url)
                with mock.patch.object(launcher, "existing_service") as health, \
                        mock.patch.object(launcher, "open_page") as opened:
                    self.assertIsNone(launcher.recorded_service(self.home))
                health.assert_not_called()
                opened.assert_not_called()

    def test_dead_pid_is_not_reused_or_signalled_beyond_liveness_probe(self):
        with mock.patch.object(launcher.os, "kill", side_effect=ProcessLookupError) as kill, \
                mock.patch.object(launcher, "existing_service") as health:
            self.assertIsNone(launcher.recorded_service(self.home))
        kill.assert_called_once_with(os.getpid(), 0)
        health.assert_not_called()
        self.assertTrue(self.lock.exists())
        self.assertTrue(self.path.exists())

    def test_missing_lock_makes_record_stale(self):
        self.lock.unlink()
        self.assertIsNone(launcher.recorded_service(self.home))

    def test_out_of_range_pid_record_is_ignored(self):
        self.change_record(pid=10 ** 24)
        self.lock.write_text(str(10 ** 24))
        self.assertIsNone(launcher.recorded_service(self.home))

    def test_pid_reuse_or_wrong_listener_nonce_does_not_open_a_page(self):
        self.change_record(instance="0" * 32)
        self.assertIsNone(launcher.recorded_service(self.home))

    def test_different_home_cannot_reuse_a_marked_default_port(self):
        other = self.home / "other"
        other.mkdir()
        for allow_legacy in (False, True):
            with self.subTest(allow_legacy=allow_legacy):
                self.assertIsNone(launcher.find_existing_service(
                    other, self.server.server_port, allow_legacy=allow_legacy))

    def test_explicit_home_does_not_reuse_an_unregistered_listener(self):
        with mock.patch.object(launcher, "recorded_service", return_value=None), \
                mock.patch.object(launcher, "existing_service", return_value=HealthApp().health()) as health:
            self.assertIsNone(launcher.find_existing_service(self.home, 8765))
        health.assert_not_called()

    def test_invalid_and_oversized_records_are_ignored(self):
        for body in ("not json", "[]", "{}", " " * 4097, "[" * 2000):
            with self.subTest(body=body[:16]):
                self.path.write_text(body)
                self.assertIsNone(launcher.recorded_service(self.home))

    def test_symlink_record_is_not_followed_or_removed(self):
        target = self.home / "other-record"
        self.path.replace(target)
        self.path.symlink_to(target)
        self.assertIsNone(launcher.recorded_service(self.home))
        launcher.remove_service(self.home, self.record)
        self.assertTrue(self.path.is_symlink())
        self.assertTrue(target.exists())

    @unittest.skipUnless(hasattr(os, "mkfifo"), "Requires POSIX FIFO")
    def test_nonregular_record_is_ignored_without_blocking(self):
        self.path.unlink()
        os.mkfifo(self.path)
        self.assertIsNone(launcher.recorded_service(self.home))

    def test_cleanup_removes_only_unchanged_owned_metadata(self):
        self.change_record(instance="0" * 32)
        launcher.remove_service(self.home, self.record)
        self.assertTrue(self.path.exists())
        self.change_record()
        launcher.remove_service(self.home, self.record)
        self.assertFalse(self.path.exists())
        self.assertTrue(self.lock.exists())
        launcher.remove_service(self.home, self.record)


@unittest.skipUnless(os.name == "posix", "Requires foreground POSIX launcher signals")
class DiscoveryLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="paperread-discovery-process-")
        self.addCleanup(self.directory.cleanup)
        self.home = Path(self.directory.name).resolve()
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

    def command(self, home, port):
        code = (
            "import os\n"
            "from pathlib import Path\n"
            "from server import launcher\n"
            "from server.__main__ import main\n"
            "def opened(url):\n"
            " (Path(os.environ['PAPERREAD_HOME']) / ('opened-' + str(os.getpid()))).write_text(url)\n"
            " return True\n"
            "launcher.webbrowser.open = opened\n"
            f"raise SystemExit(main(['--port', '{port}', '--open'], allow_force_exit=True))\n"
        )
        return [sys.executable, "-c", code], dict(
            os.environ, PAPERREAD_HOME=str(home), PYTHONDONTWRITEBYTECODE="1")

    def start(self, home, port):
        command, environment = self.command(home, port)
        process = subprocess.Popen(command, env=environment, cwd=ROOT,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   start_new_session=True)
        self.processes.append(process)
        output = b""
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline and b"Ctrl+C" not in output:
            readable, _, _ = select.select([process.stdout], [], [], 0.1)
            if readable:
                data = os.read(process.stdout.fileno(), 4096)
                if not data:
                    break
                output += data
        message = output.decode()
        self.assertIn("服务已启动", message, message)
        url = re.search(r"http://127\.0\.0\.1:\d+", message).group()
        deadline = time.monotonic() + 2
        while launcher.recorded_service(home) is None and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(launcher.recorded_service(home)[0], url)
        return process, url

    def stop(self, process, home, url):
        process.send_signal(signal.SIGHUP)
        output, _ = process.communicate(timeout=6)
        self.assertEqual(process.returncode, 0, output.decode())
        self.assertFalse((home / launcher.SERVICE_RECORD).exists())
        self.assertFalse((home / ".server.lock").exists())
        with socket.socket() as probe:
            self.assertNotEqual(probe.connect_ex(("127.0.0.1", int(url.rsplit(":", 1)[1]))), 0)

    def test_fallback_relaunch_reuses_listener_and_shutdown_removes_metadata(self):
        with socket.socket() as occupied:
            occupied.bind(("127.0.0.1", 0))
            occupied.listen()
            port = occupied.getsockname()[1]
            # A record without a live lock may remain after an interrupted
            # cleanup. Startup replaces it only after acquiring its own lock.
            (self.home / launcher.SERVICE_RECORD).write_text('{"stale": true}')
            process, url = self.start(self.home, port)
            self.assertNotEqual(url, f"http://127.0.0.1:{port}")
            record = (self.home / launcher.SERVICE_RECORD).read_bytes()
            command, environment = self.command(self.home, port)
            second = subprocess.run(command, cwd=ROOT, env=environment,
                                    text=True, capture_output=True, timeout=6)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertIn("服务已在运行：" + url, second.stdout)
            self.assertNotIn("服务已启动", second.stdout)
            self.assertEqual((self.home / launcher.SERVICE_RECORD).read_bytes(), record)
            self.assertEqual([p.read_text() for p in self.home.glob("opened-*")], [url, url])
            self.assertIsNone(process.poll())
            self.stop(process, self.home, url)
            self.assertEqual(occupied.getsockname()[1], port)

    def test_different_home_starts_own_service_and_leaves_first_running(self):
        with socket.socket() as reserve:
            reserve.bind(("127.0.0.1", 0))
            port = reserve.getsockname()[1]
        first, first_url = self.start(self.home, port)
        other_home = self.home / "other"
        other_home.mkdir()
        second, second_url = self.start(other_home, port)
        self.assertNotEqual(first_url, second_url)
        self.stop(second, other_home, second_url)
        self.assertIsNone(first.poll())
        self.assertEqual(launcher.recorded_service(self.home)[0], first_url)
        self.stop(first, self.home, first_url)


if __name__ == "__main__":
    unittest.main()
