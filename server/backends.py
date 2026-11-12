"""Bounded, non-interactive model adapters using only the Python standard library.

The CLI flags disable model-accessible tools; an empty cwd or a prompt is not a
security boundary. CLI authentication/state and administrator-managed policy are
owned by the installed CLI. These adapters do not claim an OS sandbox around the
CLI itself. The API adapter sends no tools and is independent of CLI settings.
"""

import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import time
from urllib import error, request

from .config import Config, is_loopback_url

MAX_OUTPUT_BYTES = 2 * 1024 * 1024
MAX_PROMPT_BYTES = 2 * 1024 * 1024

# Preserve login and ordinary connectivity settings, not runtime preload hooks,
# parent-agent session identifiers, or arbitrary application secrets.
_CLI_ENV_KEYS = {
    "HOME", "PATH", "TMPDIR", "TEMP", "TMP", "LANG", "LC_ALL", "LC_CTYPE",
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "all_proxy", "no_proxy",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS",
    "OPENAI_API_KEY", "OPENAI_BASE_URL", "CODEX_API_KEY", "CODEX_HOME",
    "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CONFIG_DIR",
}


class BackendError(RuntimeError):
    """Safe to display: never contains provider output, prompts or credentials."""


def backend_summary(config: Config) -> dict:
    """Local installation/configuration checks only, with no network or auth probe."""
    api_ready = bool(
        config.api.base_url and config.api.model
        and (config.api.api_key or is_loopback_url(config.api.base_url))
    )
    return {
        "selected": config.backend,
        "backends": [
            {"name": name, "available": bool(shutil.which(getattr(config, name).executable)),
             "configured": True, "authenticated": None}
            for name in ("claude", "codex")
        ] + [{"name": "api", "available": api_ready, "configured": api_ready, "authenticated": None}],
    }


def _json_object(value: str | bytes) -> dict:
    try:
        def reject_constant(_value):
            raise ValueError("Non-finite JSON number")
        result = json.loads(value, parse_constant=reject_constant)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise BackendError("The model returned invalid JSON.") from None
    if not isinstance(result, dict):
        raise BackendError("The model response must be a JSON object.")
    return result


def _check_input(prompt: str, schema: dict) -> None:
    if not isinstance(prompt, str) or not isinstance(schema, dict):
        raise BackendError("Invalid model request.")
    try:
        too_large = len(prompt.encode("utf-8")) > MAX_PROMPT_BYTES
        json.dumps(schema, allow_nan=False)
    except (UnicodeError, TypeError, ValueError, RecursionError):
        raise BackendError("Invalid model request.") from None
    if too_large:
        raise BackendError("The model request is too large.")


def _stop_process_group(process: subprocess.Popen) -> None:
    """The child was started as its own session; terminate only that session group."""
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=0.2)
        except subprocess.TimeoutExpired:
            pass
        # The leader may exit before a grandchild that ignored SIGTERM.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    else:
        process.kill()
    process.wait(timeout=2)


def _run_cli(command: list[str], prompt: str, cwd: Path, timeout: float) -> bytes:
    if os.name != "posix":
        # Windows requires a Job Object for reliable descendant cleanup.
        raise BackendError("CLI backends currently require macOS or Linux; use the API backend on Windows.")
    process = None
    try:
        # Files keep both the prompt off argv and response memory bounded.
        with tempfile.TemporaryFile() as stdin, tempfile.TemporaryFile() as stdout:
            stdin.write(prompt.encode("utf-8"))
            stdin.seek(0)
            process = subprocess.Popen(
                command, stdin=stdin, stdout=stdout, stderr=subprocess.DEVNULL,
                cwd=cwd, shell=False, start_new_session=True,
                env={key: value for key, value in os.environ.items() if key in _CLI_ENV_KEYS},
            )
            deadline = time.monotonic() + timeout
            while True:
                if os.fstat(stdout.fileno()).st_size > MAX_OUTPUT_BYTES:
                    raise BackendError("The CLI response exceeded the output limit.")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise BackendError("The model request timed out.")
                try:
                    process.wait(timeout=min(remaining, 0.1))
                    break
                except subprocess.TimeoutExpired:
                    continue
            if process.returncode != 0:
                raise BackendError("The model CLI failed. Check its local installation and login.")
            stdout.seek(0)
            output = stdout.read(MAX_OUTPUT_BYTES + 1)
            if len(output) > MAX_OUTPUT_BYTES:
                raise BackendError("The CLI response exceeded the output limit.")
            return output
    except (OSError, ValueError, UnicodeError):
        raise BackendError("Cannot run the configured model CLI.") from None
    finally:
        if process is not None:
            try:
                _stop_process_group(process)
            except (OSError, subprocess.SubprocessError):
                raise BackendError("Cannot finish cleaning up the model CLI processes.") from None


class CLIBackend:
    def __init__(self, config: Config, name: str):
        self.config = config
        self.name = name
        self.settings = getattr(config, name)

    def generate(self, prompt: str, schema: dict) -> dict:
        _check_input(prompt, schema)
        executable = shutil.which(self.settings.executable)
        if not executable:
            raise BackendError("The selected model CLI is not installed.")
        deadline = time.monotonic() + self.config.timeout_seconds
        try:
            with tempfile.TemporaryDirectory(prefix="paperread-model-") as directory:
                root = Path(directory)
                cwd = root / "work"
                cwd.mkdir()
                def probe(arguments):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise BackendError("The model request timed out.")
                    return _run_cli([executable, *arguments], "", cwd, min(10, remaining))
                if self.name == "claude":
                    help_text = probe(["--help"])
                    required = (b"--tools", b"--safe-mode", b"--strict-mcp-config", b"--json-schema",
                                b"--disable-slash-commands", b"--no-session-persistence")
                    if not all(flag in help_text for flag in required):
                        raise BackendError("Update Claude Code: required isolation flags are unavailable.")
                    command = self._claude_command(executable, schema)
                else:
                    help_text = probe(["exec", "--help"])
                    required = (b"--ignore-user-config", b"--ignore-rules", b"--ephemeral", b"--output-schema")
                    if not all(flag in help_text for flag in required):
                        raise BackendError("Update Codex: required isolation flags are unavailable.")
                    schema_path = root / "schema.json"
                    schema_path.write_text(json.dumps(schema), encoding="utf-8")
                    command = self._codex_command(executable, schema_path)
                    overrides = [part for index, argument in enumerate(command) if argument == "-c"
                                 for part in command[index:index + 2]]
                    feature_output = probe([*overrides, "features", "list"])
                    features = {line.split()[0]: line.split()[-1] for line in feature_output.splitlines() if line.split()}
                    required_features = {part.split("=")[0].split(".", 1)[1].encode()
                                         for part in overrides if part.startswith("features.")}
                    if any(features.get(name) != b"false" for name in required_features):
                        raise BackendError("Required Codex tool controls are unavailable or enforced by local policy.")
                if self.settings.model:
                    command.extend(["--model", self.settings.model])
                if self.name == "codex":
                    command.append("-")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise BackendError("The model request timed out.")
                output = _run_cli(command, prompt, cwd, remaining)
                return self._parse_output(output)
        except OSError:
            raise BackendError("Cannot prepare the temporary model workspace.") from None

    @staticmethod
    def _claude_command(executable: str, schema: dict) -> list[str]:
        # Safe mode disables executable customizations while preserving Claude's
        # existing authentication/model setup. An empty --setting-sources also
        # removes apiKeyHelper and incorrectly makes an authenticated CLI fail.
        return [
            executable, "--print", "--output-format", "json", "--json-schema", json.dumps(schema),
            "--tools", "", "--safe-mode", "--disable-slash-commands", "--no-session-persistence",
            "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
            "--permission-mode", "dontAsk", "--no-chrome",
        ]

    @staticmethod
    def _codex_command(executable: str, schema_path: Path) -> list[str]:
        # read-only alone still permits reading user files. Disable the tool
        # surfaces separately, and ignore customization/rules from user config.
        overrides = {
            "approval_policy": '"never"', "web_search": '"disabled"',
            "features.shell_tool": "false", "features.view_image": "false",
            "features.shell_snapshot": "false", "features.code_mode": "false",
            "features.code_mode_host": "false", "features.code_mode_only": "false",
            "features.browser_use": "false", "features.browser_use_external": "false",
            "features.browser_use_full_cdp_access": "false", "features.in_app_browser": "false",
            "features.computer_use": "false", "features.image_generation": "false",
            "features.apps": "false", "features.plugins": "false",
            "features.remote_plugin": "false", "features.recommended_plugins": "false",
            "features.enable_mcp_apps": "false",
            "features.multi_agent": "false", "features.multi_agent_v2": "false",
            "features.hooks": "false", "features.memories": "false",
            "features.skill_search": "false", "features.skill_mcp_dependency_install": "false",
            "features.request_permissions_tool": "false", "skills.bundled.enabled": "false",
            "skills.include_instructions": "false", "project_doc_max_bytes": "0",
            "mcp_servers": "{}", "history.persistence": '"none"',
        }
        command = [executable, "exec", "--ignore-user-config", "--ignore-rules", "--ephemeral",
                   "--skip-git-repo-check", "--sandbox", "read-only", "--color", "never",
                   "--json", "--output-schema", str(schema_path)]
        for key, value in overrides.items():
            command.extend(["-c", f"{key}={value}"])
        return command

    def _parse_output(self, output: bytes) -> dict:
        if self.name == "claude":
            response = _json_object(output)
            if response.get("is_error") or response.get("subtype", "success") != "success":
                raise BackendError("Claude Code did not complete a structured response.")
            result = response.get("structured_output")
            if isinstance(result, dict):
                return result
            if isinstance(response.get("result"), str):
                return _json_object(response["result"])
            raise BackendError("Claude Code returned no structured response.")
        result = None
        completed = False
        try:
            for line in output.splitlines():
                if not line.strip():
                    continue
                event = _json_object(line)
                if event.get("type") in ("error", "turn.failed"):
                    raise BackendError("Codex did not complete a structured response.")
                if event.get("type") == "turn.completed":
                    completed = True
                item = event.get("item", {})
                if (event.get("type") == "item.completed" and isinstance(item, dict)
                        and item.get("type") == "agent_message" and isinstance(item.get("text"), str)):
                    result = item["text"]
        except (TypeError, ValueError):
            raise BackendError("Codex returned invalid response events.") from None
        if result is None or not completed:
            raise BackendError("Codex returned no completed structured response.")
        return _json_object(result)


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never resend the Authorization header or paper text at a redirected URL.
        return None


class APIBackend:
    def __init__(self, config: Config):
        self.config = config
        self.settings = config.api
        self.opener = request.build_opener(_NoRedirect())

    def generate(self, prompt: str, schema: dict) -> dict:
        _check_input(prompt, schema)
        settings = self.settings
        if not settings.base_url or not settings.model or not (settings.api_key or is_loopback_url(settings.base_url)):
            raise BackendError("Configure the API base_url, model and api_key in local config.json.")
        response_format = {"type": settings.response_format}
        if settings.response_format == "json_schema":
            response_format["json_schema"] = {"name": "paperread_response", "strict": True, "schema": schema}
        payload = {
            "model": settings.model,
            "messages": [
                {"role": "system", "content": "Return only a JSON object matching this JSON Schema: " + json.dumps(schema)},
                {"role": "user", "content": prompt},
            ],
            "response_format": response_format,
            "stream": False,
        }
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if settings.api_key:
            headers["Authorization"] = "Bearer " + settings.api_key
        endpoint = settings.base_url.rstrip("/") + "/chat/completions"
        req = request.Request(endpoint, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
        deadline = time.monotonic() + self.config.timeout_seconds
        for attempt in range(self.config.max_retries + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise BackendError("The model request timed out.")
            try:
                with self.opener.open(req, timeout=remaining) as response:
                    output = self._read_response(response, deadline)
                return self._parse_response(_json_object(output))
            except error.HTTPError as exc:
                code = exc.code
                exc.close()
                retryable = code in (408, 429) or 500 <= code <= 599
                message = f"The model API request failed (HTTP {code})."
            except (error.URLError, TimeoutError, OSError):
                retryable = True
                message = "Cannot reach the model API within the time limit."
            except (ValueError, UnicodeError, RecursionError):
                raise BackendError("The model API returned an invalid response.") from None
            if not retryable or attempt >= self.config.max_retries:
                raise BackendError(message) from None
            delay = min(0.25 * (2 ** attempt), max(0, deadline - time.monotonic()))
            time.sleep(delay)
        raise BackendError("The model API request failed.")

    @staticmethod
    def _read_response(response, deadline: float) -> bytes:
        chunks = []
        length = 0
        # HTTPResponse.read1 returns after one underlying read, allowing a
        # deadline check even when a peer slowly trickles out response bytes.
        read = getattr(response, "read1", response.read)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise BackendError("The model request timed out.")
            # urllib does not expose a public way to reduce the read timeout;
            # tighten it when using its standard HTTPResponse socket wrapper.
            raw = getattr(getattr(response, "fp", None), "raw", None)
            sock = getattr(raw, "_sock", None)
            if sock is not None:
                sock.settimeout(remaining)
            chunk = read(min(65_536, MAX_OUTPUT_BYTES + 1 - length))
            if not chunk:
                return b"".join(chunks)
            length += len(chunk)
            if length > MAX_OUTPUT_BYTES:
                raise BackendError("The API response exceeded the output limit.")
            chunks.append(chunk)

    @staticmethod
    def _parse_response(response: dict) -> dict:
        try:
            choice = response["choices"][0]
            message = choice["message"]
            if message.get("refusal") or choice.get("finish_reason") not in (None, "stop"):
                raise BackendError("The model API did not complete a usable response.")
            content = message["content"]
            if isinstance(content, list):
                content = "".join(part["text"] for part in content if part.get("type") == "text")
            if not isinstance(content, str):
                raise BackendError("The model API returned no JSON text.")
            return _json_object(content)
        except (KeyError, IndexError, TypeError, AttributeError):
            raise BackendError("The model API returned an invalid response envelope.") from None


def create_backend(config: Config):
    if config.backend == "api":
        return APIBackend(config)
    if config.backend in ("claude", "codex"):
        return CLIBackend(config, config.backend)
    raise BackendError("Unknown model backend.")
