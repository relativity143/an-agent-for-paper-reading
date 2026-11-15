"""Offline settings persistence and disclosure contracts using temporary homes."""

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import stat
import tempfile
import threading
import unittest
from unittest import mock

from server import backends, settings
from server.config import ConfigError, load_config


SECRET = "secret-only-in-local-config"


class SettingsConfigTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.home = Path(self.directory.name)
        self.path = self.home / "config.json"
        self.which = mock.patch.object(backends.shutil, "which", return_value="/private/tools/model")
        self.which.start()
        self.addCleanup(self.which.stop)

    def write(self, value):
        self.path.write_text(json.dumps(value, ensure_ascii=False) + "\n", encoding="utf-8")
        return self.path.read_bytes()

    def assert_private(self, value):
        encoded = json.dumps(value, ensure_ascii=False)
        for forbidden in (SECRET, str(self.home), "/private/tools", "api_key", "executable"):
            self.assertNotIn(forbidden, encoded)

    def test_public_settings_has_exact_fields_and_no_private_config(self):
        self.write({"backend": "api", "claude": {"executable": "/private/tools/claude"},
                    "api": {"base_url": "https://example.org/v1/", "model": "test-model", "api_key": SECRET},
                    "private_note": {"path": str(self.home)}})
        with mock.patch.object(backends.subprocess, "Popen", side_effect=AssertionError("model process")), \
                mock.patch.object(backends.request, "urlopen", side_effect=AssertionError("network request")):
            public = settings.public_settings(load_config(self.home))
        self.assertEqual(set(public), {"backend", "backends", "api", "timeout_seconds"})
        self.assertEqual(public["backend"], "api")
        self.assertEqual(public["api"], {"base_url": "https://example.org/v1", "model": "test-model",
                                         "key_configured": True})
        self.assertEqual([item["name"] for item in public["backends"]], ["claude", "codex", "api"])
        for backend in public["backends"]:
            fields = {"name", "available", "configured", "authenticated"}
            if backend["name"] in ("claude", "codex"):
                fields.update(("model", "model_suggestions"))
            self.assertEqual(set(backend), fields)
            self.assertTrue(backend["available"])
            self.assertIsNone(backend["authenticated"])
        self.assert_private(public)

    def test_public_settings_can_keep_running_backend_with_latest_local_api(self):
        self.write({"backend": "codex", "api": {"base_url": "http://127.0.0.1:8123/v1", "model": "local"}})
        public = settings.public_settings(load_config(self.home), selected="claude")
        self.assertEqual(public["backend"], "claude")
        self.assertTrue(public["backends"][2]["available"])
        self.assertFalse(public["api"]["key_configured"])

    def test_local_paths_and_keys_embedded_in_public_api_fields_are_hidden(self):
        for model in ("/Users/private/model", "~/private/model", r"C:\private\model", SECRET,
                      "file:///private/model", "https://user:password@example.org/model"):
            with self.subTest(model=model):
                self.write({"api": {"base_url": "https://example.org/" + SECRET,
                                    "model": model, "api_key": SECRET}})
                public = settings.public_settings(load_config(self.home))
                self.assertEqual(public["api"], {"base_url": "", "model": "", "key_configured": True})
                self.assert_private(public)

    def test_missing_read_is_side_effect_free_and_missing_write_creates_private_file(self):
        home = self.home / "new-home"
        self.assertEqual(settings.public_settings(load_config(home))["backend"], "claude")
        self.assertFalse(home.exists())
        config = settings.write_backend(home, "codex")
        path = home / "config.json"
        self.assertEqual(config.backend, "codex")
        self.assertEqual(json.loads(path.read_text()), {"backend": "codex"})
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual([entry.name for entry in home.iterdir()], ["config.json"])

    def test_switch_preserves_every_other_field_and_survives_reload(self):
        raw = {"backend": "claude", "timeout_seconds": 45, "max_retries": 2,
               "api": {"base_url": "https://example.org/v1/", "model": "m", "api_key": SECRET,
                       "reader_extension": {"nested": [1, "中文", True, None]}},
               "claude": {"executable": "/private/tools/claude", "args": ["unrecognized"]},
               "codex": {"model": "m", "future": {"enabled": False}},
               "my_notes": {"value": ["a", {"b": "c"}]}}
        self.write(raw)
        config = settings.write_backend(self.home, "api")
        self.assertEqual(config.backend, "api")
        self.assertEqual(json.loads(self.path.read_text()), {**raw, "backend": "api"})
        self.assertEqual(load_config(self.home), config)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_invalid_existing_content_is_unchanged(self):
        invalid = [b"", b"[]", b"null", b'{"backend":',
                   b'{"backend":"claude","backend":"api"}',
                   b'{"extension":{"a":1,"a":2}}', b'{"extension":NaN}',
                   b'{"extension":Infinity}', b'{"extension":1e999}', b'\xff',
                   json.dumps({"backend": []}).encode(),
                   json.dumps({"api": {"model": []}}).encode(),
                   json.dumps({"api": {"response_format": {}}}).encode(),
                   json.dumps({"claude": {"executable": [SECRET]}}).encode(),
                   json.dumps({"codex": None}).encode(), b" " * 65_537]
        for encoded in invalid:
            with self.subTest(encoded=encoded[:40]):
                self.path.write_bytes(encoded)
                with self.assertRaises(ConfigError) as caught:
                    settings.write_backend(self.home, "codex")
                self.assertEqual(self.path.read_bytes(), encoded)
                self.assert_private(str(caught.exception))
                self.assertEqual([entry.name for entry in self.home.iterdir()], ["config.json"])

    def test_known_fields_still_validated_when_unknown_fields_are_allowed(self):
        for raw in ({"backend": {}}, {"api": []}, {"claude": []},
                    {"api": {"api_key": {}}}, {"api": {"model": 5}},
                    {"timeout_seconds": "15"}, {"max_retries": False}):
            with self.subTest(raw=raw):
                self.write({"reader_extension": True, **raw})
                with self.assertRaises(ConfigError):
                    load_config(self.home)

    def test_unavailable_selection_does_not_write_or_echo_paths(self):
        before = self.write({"backend": "claude", "codex": {"executable": "/private/tools/missing"},
                             "api": {"api_key": SECRET}})
        with mock.patch.object(backends.shutil, "which", return_value=None):
            for name in ("claude", "codex", "api"):
                with self.subTest(name=name), self.assertRaises(ConfigError) as caught:
                    settings.write_backend(self.home, name)
                self.assert_private(str(caught.exception))
                self.assertEqual(self.path.read_bytes(), before)

    def test_invalid_requested_backend_never_changes_file(self):
        before = self.write({"backend": "claude"})
        for name in (None, [], {}, SECRET):
            with self.subTest(name=name), self.assertRaises(ConfigError) as caught:
                settings.write_backend(self.home, name)
            self.assert_private(str(caught.exception))
            self.assertEqual(self.path.read_bytes(), before)

    def test_prepare_failure_does_not_commit_and_releases_lock(self):
        before = self.write({"backend": "claude", "reader_extension": SECRET})
        with self.assertRaisesRegex(RuntimeError, "preparation failed"):
            with settings.prepare_backend_change(self.home, "codex") as candidate:
                self.assertEqual(candidate.backend, "codex")
                self.assertEqual(self.path.read_bytes(), before)
                raise RuntimeError("preparation failed")
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse((self.home / ".config.lock").exists())
        self.assertEqual(settings.write_backend(self.home, "codex").backend, "codex")

    def test_atomic_replace_failure_preserves_original_and_cleans_temporary_file(self):
        before = self.write({"backend": "claude", "api": {"api_key": SECRET}})
        with mock.patch.object(settings.os, "replace", side_effect=OSError("private " + SECRET)), \
                self.assertRaises(ConfigError) as caught:
            settings.write_backend(self.home, "codex")
        self.assert_private(str(caught.exception))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual([entry.name for entry in self.home.iterdir()], ["config.json"])

    def test_writer_reads_latest_config_after_waiting_for_lock(self):
        self.write({"backend": "claude", "reader_extension": "original"})
        started = threading.Event()

        def change():
            started.set()
            return settings.write_backend(self.home, "codex")

        with ThreadPoolExecutor(max_workers=1) as pool:
            with settings._config_lock(self.home):
                pending = pool.submit(change)
                self.assertTrue(started.wait(timeout=2))
                self.assertFalse(pending.done())
                self.write({"backend": "claude", "reader_extension": "new data"})
            self.assertEqual(pending.result(timeout=3).backend, "codex")
        self.assertEqual(json.loads(self.path.read_text()), {"backend": "codex", "reader_extension": "new data"})

    def test_lock_timeout_is_bounded_and_does_not_remove_another_lock(self):
        lock = self.home / ".config.lock"
        lock.write_text("other process")
        with self.assertRaises(ConfigError) as caught:
            with settings._config_lock(self.home, timeout=0):
                self.fail("locked configuration was entered")
        self.assert_private(str(caught.exception))
        self.assertEqual(lock.read_text(), "other process")


if __name__ == "__main__":
    unittest.main()
