"""Model and timeout contracts; all homes and availability checks are isolated."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from server import backends, settings
from server.config import ConfigError, DEFAULT_TIMEOUT_SECONDS, load_config


class RoundTwoSettingsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.home = Path(self.directory.name)
        self.path = self.home / "config.json"
        patch = mock.patch.object(backends.shutil, "which", return_value="/private/model-cli")
        patch.start()
        self.addCleanup(patch.stop)
        for target in (mock.patch.object(backends.subprocess, "Popen", side_effect=AssertionError("model call")),
                       mock.patch.object(backends.request, "urlopen", side_effect=AssertionError("network"))):
            target.start()
            self.addCleanup(target.stop)

    def write(self, raw):
        self.path.write_text(json.dumps(raw, ensure_ascii=False) + "\n", encoding="utf-8")
        return self.path.read_bytes()

    def change(self, body, current="claude"):
        with settings.prepare_settings_change(self.home, body, current) as candidate:
            pass
        return candidate

    def test_defaults_and_custom_suggestions_are_public_without_paths_or_secrets(self):
        defaults = settings.public_settings(load_config(self.home))
        self.assertGreaterEqual(defaults["timeout_seconds"], 300)
        self.assertEqual(defaults["timeout_seconds"], DEFAULT_TIMEOUT_SECONDS)
        self.assertTrue(all(item["model_suggestions"] for item in defaults["backends"][:2]))
        self.assertFalse(self.path.exists())
        self.write({"claude": {"model": "custom-v1[fast]", "model_suggestions": ["custom-v1[fast]"]},
                    "codex": {"model": None, "model_suggestions": []}, "timeout_seconds": 45,
                    "api": {"api_key": "secret-private"}, "extension": "/private/sensitive"})
        public = settings.public_settings(load_config(self.home))
        self.assertEqual(public["backends"][0]["model"], "custom-v1[fast]")
        self.assertEqual(public["backends"][0]["model_suggestions"], ["custom-v1[fast]"])
        self.assertIsNone(public["backends"][1]["model"])
        self.assertEqual(public["backends"][1]["model_suggestions"], [])
        self.assertNotIn("model", public["backends"][2])
        self.assertNotIn("model_suggestions", public["backends"][2])
        self.assertEqual(public["timeout_seconds"], 45)
        encoded = json.dumps(public)
        for value in ("secret-private", "/private/", "executable", "api_key", "extension"):
            self.assertNotIn(value, encoded)

    def test_cli_fields_do_not_repeat_an_embedded_api_key(self):
        self.write({"api": {"api_key": "secret-private"},
                    "claude": {"model": "secret-private", "model_suggestions": ["sonnet", "secret-private"]}})
        public = settings.public_settings(load_config(self.home))
        self.assertIsNone(public["backends"][0]["model"])
        self.assertEqual(public["backends"][0]["model_suggestions"], ["sonnet"])
        self.assertNotIn("secret-private", json.dumps(public))

    def test_model_only_change_targets_running_backend_and_preserves_extensions(self):
        raw = {"backend": "claude", "timeout_seconds": 45, "future": {"deep": [1, True, "中文"]},
               "claude": {"model": "old", "model_suggestions": [], "future": {"private": True}},
               "codex": {"model": "keep", "custom_flag": "value"},
               "api": {"api_key": "secret-private", "custom": "keep"}}
        before = self.write(raw)
        with settings.prepare_settings_change(self.home, {"model": "new-1.2_x:y[z]"}, "claude") as candidate:
            self.assertEqual(candidate.claude.model, "new-1.2_x:y[z]")
            self.assertEqual(candidate.codex.model, "keep")
            self.assertEqual(self.path.read_bytes(), before)
        expected = {**raw, "claude": {**raw["claude"], "model": "new-1.2_x:y[z]"}}
        self.assertEqual(json.loads(self.path.read_text()), expected)
        self.assertEqual(load_config(self.home), candidate)

    def test_backend_and_null_model_and_timeout_commit_together(self):
        self.write({"codex": {"model": "old", "reader_note": "keep"}})
        candidate = self.change({"backend": "codex", "model": None, "timeout_seconds": 600})
        self.assertEqual(candidate.backend, "codex")
        self.assertIsNone(candidate.codex.model)
        self.assertEqual(candidate.timeout_seconds, 600)
        self.assertEqual(json.loads(self.path.read_text()), {
            "backend": "codex", "codex": {"model": None, "reader_note": "keep"}, "timeout_seconds": 600})

    def test_running_backend_wins_over_a_manual_file_edit_when_backend_omitted(self):
        self.write({"backend": "codex", "codex": {"model": "keep"}})
        candidate = self.change({"model": "sonnet"}, current="claude")
        self.assertEqual(candidate.backend, "claude")
        self.assertEqual(candidate.claude.model, "sonnet")
        self.assertEqual(candidate.codex.model, "keep")
        self.assertEqual(load_config(self.home), candidate)

    def test_omitted_runtime_backend_uses_persisted_backend(self):
        self.write({"backend": "codex"})
        candidate = self.change({"model": "custom"}, current=None)
        self.assertEqual(candidate.codex.model, "custom")
        self.assertIsNone(candidate.claude.model)

    def test_same_model_validation_for_requests_and_handwritten_config(self):
        valid = (None, "a", "0", "A" * 100, "gpt-6.1_x:y[z]")
        invalid = ("", "-model", " model", "model ", "two words", "a/b", "a\\b", "a\n", "[model]",
                   "模型", "A" * 101, [], {}, True, 12)
        for value in valid:
            with self.subTest(valid=value):
                self.write({})
                self.assertEqual(self.change({"model": value}).claude.model, value)
                self.write({"codex": {"model": value}})
                self.assertEqual(load_config(self.home).codex.model, value)
        for value in invalid:
            with self.subTest(invalid=value):
                before = self.write({"claude": {"model": "keep"}})
                with self.assertRaises(ConfigError):
                    self.change({"model": value})
                self.assertEqual(self.path.read_bytes(), before)
                self.assertFalse((self.home / ".config.lock").exists())
                self.write({"codex": {"model": value}})
                with self.assertRaises(ConfigError):
                    load_config(self.home)

    def test_suggestions_are_a_list_of_valid_model_names(self):
        for value in (None, "sonnet", {}, [None], ["-flag"], ["a b"], ["a" * 101]):
            with self.subTest(value=value):
                self.write({"claude": {"model_suggestions": value}})
                with self.assertRaises(ConfigError):
                    load_config(self.home)

    def test_timeout_is_an_integer_between_30_and_600_everywhere(self):
        for value in (30, 300, 600):
            with self.subTest(valid=value):
                self.write({})
                self.assertEqual(self.change({"timeout_seconds": value}).timeout_seconds, value)
                self.assertEqual(load_config(self.home).timeout_seconds, value)
        for value in (None, True, False, 20, 29, 601, 30.0, "300", [], {}):
            with self.subTest(invalid=value):
                before = self.write({})
                with self.assertRaises(ConfigError):
                    self.change({"timeout_seconds": value})
                self.assertEqual(self.path.read_bytes(), before)
                self.write({"timeout_seconds": value})
                with self.assertRaises(ConfigError):
                    load_config(self.home)

    def test_empty_extra_and_api_model_requests_do_not_change_configuration(self):
        for body in ({}, None, [], {"unknown": "x"}, {"backend": "codex", "api_key": "x"},
                     {"backend": "api", "model": "x"}, {"backend": "api", "model": None}):
            with self.subTest(body=body):
                before = self.write({})
                with self.assertRaises(ConfigError):
                    self.change(body)
                self.assertEqual(self.path.read_bytes(), before)
        before = self.write({"backend": "api"})
        with self.assertRaises(ConfigError):
            self.change({"model": "x"}, current="api")
        self.assertEqual(self.path.read_bytes(), before)

    def test_runtime_failure_rolls_back_all_requested_fields_and_releases_lock(self):
        before = self.write({"backend": "claude", "codex": {"model": "old"}, "timeout_seconds": 45})
        with self.assertRaisesRegex(RuntimeError, "preparation failed"):
            with settings.prepare_settings_change(self.home, {
                    "backend": "codex", "model": "new", "timeout_seconds": 300}, "claude"):
                raise RuntimeError("preparation failed")
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse((self.home / ".config.lock").exists())

    def test_saved_model_reset_and_timeout_reach_the_next_cli_generation(self):
        result = {"point": "Offline command contract"}
        schema = {"type": "object", "properties": {"point": {"type": "string"}}, "required": ["point"]}
        help_text = {
            "claude": b"--tools --safe-mode --strict-mcp-config --json-schema --disable-slash-commands --no-session-persistence --effort",
            "codex": b"--ignore-user-config --ignore-rules --ephemeral --output-schema",
        }
        for name, probe_count in (("claude", 1), ("codex", 2)):
            self.write({"backend": name, name: {"model": "old-model"}})
            for model, timeout in (("selected-v1.2[fast]", 450), (None, 90)):
                with self.subTest(backend=name, model=model):
                    changes = {"model": model, "timeout_seconds": timeout}
                    with settings.prepare_settings_change(self.home, changes, name) as config:
                        backend = backends.create_backend(config)
                    self.assertEqual(getattr(load_config(self.home), name).model, model)
                    observed, now = [], [100.0]

                    def run(command, prompt, cwd, remaining):
                        observed.append((command, prompt, remaining))
                        if "--help" in command:
                            now[0] += 3  # Capability checks consume the same generation deadline.
                            return help_text[name]
                        if "features" in command:
                            now[0] += 3
                            features = [part.split("=", 1)[0].split(".", 1)[1]
                                        for part in command if part.startswith("features.")]
                            return "\n".join(feature + " stable false" for feature in features).encode()
                        if name == "claude":
                            return json.dumps({"structured_output": result}).encode()
                        return b"\n".join(json.dumps(event).encode() for event in (
                            {"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(result)}},
                            {"type": "turn.completed"},
                        ))

                    try:
                        with mock.patch.object(backends, "_run_cli", side_effect=run), \
                                mock.patch.object(backends.time, "monotonic", side_effect=lambda: now[0]):
                            self.assertEqual(backend.generate("offline prompt", schema), result)
                    finally:
                        backend.close()
                    self.assertEqual(len(observed), probe_count + 1)
                    for command, prompt, remaining in observed[:-1]:
                        self.assertEqual(prompt, "")
                        self.assertEqual(remaining, 10)
                        self.assertNotIn("--model", command)
                    command, prompt, remaining = observed[-1]
                    if name == "claude":
                        self.assertEqual(command[command.index("--effort") + 1], "high")
                    else:
                        self.assertIn('model_reasoning_effort="high"', command)
                    self.assertEqual(prompt, "offline prompt")
                    self.assertEqual(remaining, timeout - 3 * probe_count)
                    if model is None:
                        self.assertNotIn("--model", command)
                    else:
                        self.assertEqual(command.count("--model"), 1)
                        self.assertEqual(command[command.index("--model") + 1], model)


if __name__ == "__main__":
    unittest.main()
