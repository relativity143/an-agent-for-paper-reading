"""Offline adapter contracts: no installed CLI generation or paid API calls."""

from dataclasses import replace
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock
from urllib import error, request

from server import backends
from server.config import APIConfig, CLIConfig, Config, ConfigError, load_config


SCHEMA = {"type": "object", "properties": {"point": {"type": "string"}},
          "required": ["point"], "additionalProperties": False}
RESULT = {"point": "Synthetic fixture"}
SECRET = "secret-never-display"


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.home = Path(self.directory.name)

    def write(self, value):
        (self.home / "config.json").write_text(json.dumps(value), encoding="utf-8")

    def test_missing_file_has_defaults_without_creating_state(self):
        config = load_config(self.home)
        self.assertEqual(config.backend, "claude")
        self.assertEqual(config.claude.executable, "claude")
        self.assertEqual(list(self.home.iterdir()), [])

    def test_local_config_and_safe_summary(self):
        self.write({"backend": "api", "timeout_seconds": 30, "max_retries": 2,
                    "api": {"base_url": "https://example.org/v1/", "model": "test-model", "api_key": SECRET}})
        config = load_config(self.home)
        self.assertEqual(config.api.base_url, "https://example.org/v1")
        self.assertNotIn(SECRET, repr(config))
        with mock.patch.object(backends.shutil, "which", return_value="/private/cli"):
            summary = backends.backend_summary(config)
        self.assertEqual(summary["selected"], "api")
        self.assertTrue(summary["backends"][2]["available"])
        encoded = json.dumps(summary)
        for sensitive in (SECRET, "example.org", "test-model", "/private/cli"):
            self.assertNotIn(sensitive, encoded)
        self.assertIsNone(summary["backends"][2]["authenticated"])

    def test_invalid_config_values_never_echo_values(self):
        invalid = [
            {"backend": SECRET}, {"backend": []}, {"extra": SECRET},
            {"timeout_seconds": True}, {"timeout_seconds": 0}, {"timeout_seconds": float("nan")},
            {"max_retries": 3}, {"max_retries": True}, {"claude": {"args": [SECRET]}},
            {"api": {"base_url": "https://user:" + SECRET + "@example.org"}},
            {"api": {"base_url": "http://example.org/v1"}},
            {"api": {"base_url": "https://example.org/?key=" + SECRET}},
            {"api": {"api_key": SECRET + "\r\nInjected: yes"}},
        ]
        for value in invalid:
            with self.subTest(value=list(value)):
                self.write(value)
                with self.assertRaises(ConfigError) as caught:
                    load_config(self.home)
                self.assertNotIn(SECRET, str(caught.exception))

    def test_invalid_json_is_sanitized(self):
        (self.home / "config.json").write_text('{"api_key": "' + SECRET, encoding="utf-8")
        with self.assertRaises(ConfigError) as caught:
            load_config(self.home)
        self.assertNotIn(SECRET, str(caught.exception))

    def test_loopback_api_may_omit_key(self):
        self.write({"backend": "api", "api": {"base_url": "http://127.0.0.1:8123/v1", "model": "local"}})
        config = load_config(self.home)
        self.assertTrue(backends.backend_summary(config)["backends"][2]["available"])


class APIBackendTests(unittest.TestCase):
    def setUp(self):
        self.config = Config(home=Path("/unused"), backend="api", max_retries=1,
                             api=APIConfig("https://example.org/v1", "test-model", SECRET))
        self.backend = backends.create_backend(self.config)
        self.backend.opener = mock.Mock()

    @staticmethod
    def response(content=None, **choice_fields):
        return io.BytesIO(json.dumps({"choices": [{"message": {"content": json.dumps(RESULT) if content is None else content},
                                                   "finish_reason": "stop", **choice_fields}]}).encode())

    def test_request_and_structured_response(self):
        self.backend.opener.open.return_value = self.response()
        self.assertEqual(self.backend.generate("Synthetic prompt", SCHEMA), RESULT)
        req = self.backend.opener.open.call_args.args[0]
        body = json.loads(req.data)
        self.assertEqual(req.full_url, "https://example.org/v1/chat/completions")
        self.assertEqual(req.get_header("Authorization"), "Bearer " + SECRET)
        self.assertEqual(body["response_format"]["json_schema"]["schema"], SCHEMA)
        self.assertFalse(body["stream"])
        self.assertNotIn("tools", body)

    def test_json_object_compatibility_is_explicit(self):
        self.backend.settings = replace(self.config.api, response_format="json_object")
        self.backend.opener.open.return_value = self.response()
        self.backend.generate("prompt", SCHEMA)
        body = json.loads(self.backend.opener.open.call_args.args[0].data)
        self.assertEqual(body["response_format"], {"type": "json_object"})
        self.assertIn(json.dumps(SCHEMA), body["messages"][0]["content"])

    def test_transient_retry_is_bounded_and_errors_are_sanitized(self):
        errors = [error.HTTPError("https://" + SECRET, 429, SECRET, {}, io.BytesIO(SECRET.encode())) for _ in range(2)]
        self.backend.opener.open.side_effect = errors
        with mock.patch.object(backends.time, "sleep"), self.assertRaises(backends.BackendError) as caught:
            self.backend.generate("prompt", SCHEMA)
        self.assertEqual(self.backend.opener.open.call_count, 2)
        self.assertNotIn(SECRET, str(caught.exception))
        self.assertIn("429", str(caught.exception))

    def test_auth_failure_and_redirect_are_not_retried(self):
        for code in (401, 302):
            with self.subTest(code=code):
                self.backend.opener.open.reset_mock()
                self.backend.opener.open.side_effect = error.HTTPError(SECRET, code, SECRET, {}, io.BytesIO())
                with self.assertRaises(backends.BackendError):
                    self.backend.generate("prompt", SCHEMA)
                self.assertEqual(self.backend.opener.open.call_count, 1)
        handler = backends._NoRedirect()
        self.assertIsNone(handler.redirect_request(request.Request("https://example.org"), None, 302, "", {}, "https://elsewhere.org"))

    def test_invalid_or_partial_response_is_not_implicitly_retried(self):
        values = [self.response("secret-never-display"), self.response(finish_reason="length"),
                  io.BytesIO(b"{}"), io.BytesIO(b"not JSON " + SECRET.encode()),
                  self.response('{"point": NaN}')]
        for response in values:
            self.backend.opener.open.reset_mock()
            self.backend.opener.open.return_value = response
            with self.assertRaises(backends.BackendError) as caught:
                self.backend.generate("prompt", SCHEMA)
            self.assertEqual(self.backend.opener.open.call_count, 1)
            self.assertNotIn(SECRET, str(caught.exception))

    def test_oversized_response_is_rejected(self):
        self.backend.opener.open.return_value = io.BytesIO(b"x" * (backends.MAX_OUTPUT_BYTES + 1))
        with self.assertRaisesRegex(backends.BackendError, "output limit"):
            self.backend.generate("prompt", SCHEMA)

    def test_total_deadline_applies_while_reading_response(self):
        response = io.BytesIO(b"partial JSON")
        with mock.patch.object(backends.time, "monotonic", side_effect=[1, 3]):
            with self.assertRaisesRegex(backends.BackendError, "timed out"):
                self.backend._read_response(response, 2)


class CLIBackendTests(unittest.TestCase):
    @staticmethod
    def backend(name):
        return backends.create_backend(Config(home=Path("/unused"), backend=name))

    def test_claude_is_tool_free_and_uses_stdin_in_empty_temporary_cwd(self):
        backend = self.backend("claude")
        help_text = b"--tools --safe-mode --strict-mcp-config --json-schema --disable-slash-commands --no-session-persistence"
        observed = []
        def run(command, prompt, cwd, timeout):
            self.assertEqual(list(cwd.iterdir()), [])
            observed.append((command, prompt, cwd))
            return help_text if command[-1] == "--help" else json.dumps({"structured_output": RESULT}).encode()
        with mock.patch.object(backends.shutil, "which", return_value="/cli/claude"), mock.patch.object(backends, "_run_cli", side_effect=run):
            self.assertEqual(backend.generate("PRIVATE_PROMPT", SCHEMA), RESULT)
        command, prompt, cwd = observed[-1]
        self.assertNotIn("PRIVATE_PROMPT", command)
        self.assertEqual(prompt, "PRIVATE_PROMPT")
        self.assertEqual(command[command.index("--tools") + 1], "")
        self.assertEqual(command[command.index("--mcp-config") + 1], '{"mcpServers":{}}')
        self.assertIn("--safe-mode", command)
        self.assertNotIn("--setting-sources", command)
        self.assertFalse(cwd.exists())

    def test_codex_flags_and_event_envelope(self):
        backend = self.backend("codex")
        events = b'\n'.join(json.dumps(value).encode() for value in (
            {"type": "thread.started"},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "Preparing the answer"}},
            {"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(RESULT)}},
            {"type": "turn.completed"},
        ))
        help_text = b"--ignore-user-config --ignore-rules --ephemeral --output-schema"
        observed = []
        def run(command, prompt, cwd, timeout):
            self.assertEqual(list(cwd.iterdir()), [])
            observed.append(command)
            if "--help" in command:
                return help_text
            if "features" in command:
                names = [part.split("=")[0].split(".", 1)[1] for part in command if part.startswith("features.")]
                return "\n".join(name + " stable false" for name in names).encode()
            schema_path = Path(command[command.index("--output-schema") + 1])
            self.assertEqual(json.loads(schema_path.read_text()), SCHEMA)
            self.assertNotEqual(schema_path.parent, cwd)
            return events
        with mock.patch.object(backends.shutil, "which", return_value="/cli/codex"), mock.patch.object(backends, "_run_cli", side_effect=run):
            self.assertEqual(backend.generate("prompt", SCHEMA), RESULT)
        command = observed[-1]
        for flag in ("features.shell_tool=false", "features.view_image=false", "features.hooks=false",
                     "features.apps=false", "features.plugins=false", "skills.include_instructions=false", "mcp_servers={}",
                     "features.browser_use=false", "features.computer_use=false", "features.code_mode_host=false"):
            self.assertIn(flag, command)
        self.assertEqual(command[-1], "-")
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", command)

    def test_missing_isolation_flags_fail_before_generation(self):
        with mock.patch.object(backends.shutil, "which", return_value="/cli/codex"), mock.patch.object(backends, "_run_cli", return_value=b"old CLI") as run:
            with self.assertRaisesRegex(backends.BackendError, "Update Codex"):
                self.backend("codex").generate("prompt", SCHEMA)
            self.assertEqual(run.call_count, 1)

    def test_cli_error_payloads_are_not_exposed(self):
        for name, output in (("claude", json.dumps({"is_error": True, "result": SECRET}).encode()),
                             ("codex", json.dumps({"type": "error", "message": SECRET}).encode())):
            with self.subTest(name=name), self.assertRaises(backends.BackendError) as caught:
                self.backend(name)._parse_output(output)
            self.assertNotIn(SECRET, str(caught.exception))

    def test_json_non_object_and_wrong_types_fail_safely(self):
        for value in ({"private": SECRET}, [SECRET], b"[]", b"null", b"true", 4):
            with self.subTest(kind=type(value).__name__), self.assertRaises(backends.BackendError) as caught:
                backends._json_object(value)
            self.assertNotIn(SECRET, str(caught.exception))

    def test_missing_tool_disable_features_fail_before_generation(self):
        help_text = b"--ignore-user-config --ignore-rules --ephemeral --output-schema"
        with mock.patch.object(backends.shutil, "which", return_value="/cli/codex"), mock.patch.object(backends, "_run_cli", side_effect=[help_text, b"shell_tool stable true"]) as run:
            with self.assertRaisesRegex(backends.BackendError, "tool controls"):
                self.backend("codex").generate("prompt", SCHEMA)
            self.assertEqual(run.call_count, 2)

    @unittest.skipUnless(os.name == "posix", "POSIX subprocess group cleanup")
    def test_real_subprocess_uses_stdin_and_drops_runtime_hooks(self):
        code = "import os,sys; print(sys.stdin.read()); print(os.environ.get('NODE_OPTIONS', 'absent'))"
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ, {"NODE_OPTIONS": SECRET}):
            output = backends._run_cli([sys.executable, "-c", code], "fixture input", Path(directory), 3)
        self.assertEqual(output, b"fixture input\nabsent\n")

    @unittest.skipUnless(os.name == "posix", "POSIX subprocess group cleanup")
    def test_timeout_kills_grandchild_even_when_it_ignores_sigterm(self):
        code = (
            "import os,signal,time\n"
            "child=os.fork()\n"
            "if child == 0:\n"
            " signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            " while True:\n"
            "  with open('heartbeat', 'a') as f: f.write('x'); f.flush()\n"
            "  time.sleep(0.03)\n"
            "else:\n"
            " time.sleep(30)\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            cwd = Path(directory)
            started = time.monotonic()
            with self.assertRaisesRegex(backends.BackendError, "timed out"):
                backends._run_cli([sys.executable, "-c", code], "", cwd, 0.4)
            self.assertLess(time.monotonic() - started, 3)
            heartbeat = cwd / "heartbeat"
            self.assertTrue(heartbeat.exists())
            size = heartbeat.stat().st_size
            time.sleep(0.15)
            self.assertEqual(heartbeat.stat().st_size, size)


if __name__ == "__main__":
    unittest.main()
