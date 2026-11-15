"""Bounded, non-interactive model adapters using only the Python standard library.

The CLI flags disable model-accessible tools; an empty cwd or a prompt is not a
security boundary. CLI authentication/state and administrator-managed policy are
owned by the installed CLI. These adapters do not claim an OS sandbox around the
CLI itself. The API adapter sends no tools and is independent of CLI settings.
"""

from contextlib import contextmanager
import http.client
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
import time
from urllib import error, request

from .config import Config, is_loopback_url

MAX_OUTPUT_BYTES = 2 * 1024 * 1024
MAX_PROMPT_BYTES = 2 * 1024 * 1024
FIXED_REASONING_EFFORT = "high"

# Both public generate and the global CLI runner retain their test interfaces.
_TASK_CONTEXT = threading.local()

CANCELLED_MESSAGE = "已停止这一轮。"
TIMEOUT_MESSAGE = "模型生成超时，请重试、选择更快的模型，或在本机配置中增加 timeout_seconds。"

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


class TaskCancellation:
    """Own only one task's processes/sockets, including resources started later."""

    def __init__(self):
        self.event = threading.Event()
        self._lock = threading.RLock()
        self._resources = {}

    def is_set(self):
        return self.event.is_set()

    def check(self):
        if self.is_set():
            raise BackendError(CANCELLED_MESSAGE)

    def register(self, resource, stop):
        with self._lock:
            self._resources[resource] = stop
            if self.is_set():
                self.finish(resource)
                self.check()

    def has_resources(self):
        with self._lock:
            return bool(self._resources)

    def discard(self, resource):
        with self._lock:
            self._resources.pop(resource, None)

    def finish(self, resource):
        with self._lock:
            stop = self._resources.get(resource)
            if stop is not None:
                stop()
                self._resources.pop(resource, None)

    def start_process(self, *args, **kwargs):
        # Check, spawn and registration share the cancellation lock. A cancelled
        # queued task cannot start a process after cancel() has returned.
        with self._lock:
            self.check()
            process = subprocess.Popen(*args, **kwargs)
            self._resources[process] = lambda: _stop_process_group(process)
            return process

    def cancel(self):
        # Keep cleanup under the lock so concurrent cancel calls also wait for
        # process-group termination; no callback acquires the application lock.
        with self._lock:
            self.event.set()
            failures = []
            for resource in tuple(self._resources):
                try:
                    self.finish(resource)
                except (OSError, subprocess.SubprocessError) as exc:
                    failures.append(exc)
            if failures:
                raise BackendError("模型进程未能完全停止，请关闭启动窗口后重新打开。") from None


@contextmanager
def cancellation_context(cancellation):
    """Keep generate(prompt, schema) compatible with existing injected backends."""
    previous = getattr(_TASK_CONTEXT, "cancellation", None)
    _TASK_CONTEXT.cancellation = cancellation
    try:
        cancellation.check()
        yield cancellation
    finally:
        _TASK_CONTEXT.cancellation = previous


@contextmanager
def _backend_scope(backend):
    cancellation = getattr(_TASK_CONTEXT, "cancellation", None) or TaskCancellation()
    with backend._scope_lock:
        if backend.cancelled.is_set():
            raise BackendError(CANCELLED_MESSAGE)
        backend._scopes[cancellation] = backend._scopes.get(cancellation, 0) + 1
    try:
        with cancellation_context(cancellation):
            yield cancellation
            cancellation.check()
    finally:
        with backend._scope_lock:
            count = backend._scopes[cancellation] - 1
            if count or cancellation.has_resources():
                backend._scopes[cancellation] = count
            else:
                del backend._scopes[cancellation]


def _close_backend(backend):
    with backend._scope_lock:
        backend.cancelled.set()
        scopes = tuple(backend._scopes)
    failed = False
    for cancellation in scopes:
        try:
            cancellation.cancel()
        except BackendError:
            failed = True
        finally:
            with backend._scope_lock:
                if backend._scopes.get(cancellation) == 0 and not cancellation.has_resources():
                    backend._scopes.pop(cancellation, None)
    if failed:
        raise BackendError("模型进程未能完全停止，请关闭启动窗口后重新打开。")


def _cli_failure(output, name="模型命令行程序"):
    """Classify a bounded diagnostic; never return raw provider text or values."""
    if isinstance(output, bytes):
        output = output.decode("utf-8", errors="replace")
    text = str(output)[:65_536].lower()
    if "requires a newer version of codex" in text:
        return "当前 Codex 命令行版本过旧，所选模型需要更新版本。请升级 Codex 命令行程序，或暂时选择跟随默认模型。"
    if any(term in text for term in ("invalid model", "model not found", "model_not_found", "unknown model",
                                     "model does not exist", "model is not available", "unsupported model",
                                     "invalid_model", "does not support", "invalid model name")):
        return "模型名可能不对，或当前账号无权使用；请在设置中换一个模型，或恢复默认模型。"
    if any(term in text for term in ("not logged in", "not authenticated", "authentication", "unauthorized",
                                     "invalid api key", "invalid_api_key", "login required", "please log in",
                                     "please login", "token expired", "401", "403")):
        return f"{name}尚未登录、登录已过期或无访问权限；请先在本机完成登录并检查账号权限，再重试。"
    if any(term in text for term in ("unknown option", "unrecognized option", "unexpected argument", "unknown flag")):
        return f"{name}版本太旧或不支持所需参数，请更新后重试。"
    if any(term in text for term in ("timed out", "timeout")):
        return TIMEOUT_MESSAGE
    return f"{name}生成失败，请检查本机登录和网络；若指定了模型，模型名可能不对，请换一个或恢复默认。"


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
        raise BackendError("模型返回的 JSON 格式不正确，请重试或更换模型。") from None
    if not isinstance(result, dict):
        raise BackendError("模型返回的内容不是 JSON 对象，请重试或更换模型。")
    return result


def _check_input(prompt: str, schema: dict) -> None:
    if not isinstance(prompt, str) or not isinstance(schema, dict):
        raise BackendError("模型请求格式不正确，请重新打开论文后重试。")
    try:
        too_large = len(prompt.encode("utf-8")) > MAX_PROMPT_BYTES
        json.dumps(schema, allow_nan=False)
    except (UnicodeError, TypeError, ValueError, RecursionError):
        raise BackendError("模型请求格式不正确，请重新打开论文后重试。") from None
    if too_large:
        raise BackendError("发给模型的内容过长，请缩小本轮讲解范围后重试。")


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
        raise BackendError("命令行模型后端目前需要 macOS 或 Linux；Windows 请切换到 API 后端。")
    process = None
    cancellation = getattr(_TASK_CONTEXT, "cancellation", None) or TaskCancellation()
    try:
        # Files keep both the prompt off argv and response memory bounded.
        with tempfile.TemporaryFile() as stdin, tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
            stdin.write(prompt.encode("utf-8"))
            stdin.seek(0)
            process = cancellation.start_process(
                command, stdin=stdin, stdout=stdout, stderr=stderr,
                cwd=cwd, shell=False, start_new_session=True,
                env={key: value for key, value in os.environ.items() if key in _CLI_ENV_KEYS},
            )
            deadline = time.monotonic() + timeout
            while True:
                cancellation.check()
                if max(os.fstat(stdout.fileno()).st_size, os.fstat(stderr.fileno()).st_size) > MAX_OUTPUT_BYTES:
                    raise BackendError("命令行模型的输出过长，已停止本轮；请缩小讲解范围后重试。")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise BackendError(TIMEOUT_MESSAGE)
                try:
                    process.wait(timeout=min(remaining, 0.1))
                    break
                except subprocess.TimeoutExpired:
                    continue
            cancellation.check()
            if process.returncode != 0:
                stderr.seek(0)
                stdout.seek(0)
                raise BackendError(_cli_failure(stderr.read(32_768) + b"\n" + stdout.read(32_768)))
            stdout.seek(0)
            output = stdout.read(MAX_OUTPUT_BYTES + 1)
            if len(output) > MAX_OUTPUT_BYTES:
                raise BackendError("命令行模型的输出过长，已停止本轮；请缩小讲解范围后重试。")
            return output
    except (OSError, ValueError, UnicodeError):
        cancellation.check()
        raise BackendError("无法启动模型命令行程序，请检查是否已安装，以及本机配置中的程序路径。") from None
    finally:
        if process is not None:
            try:
                cancellation.finish(process)
            except (OSError, subprocess.SubprocessError):
                raise BackendError("模型进程未能完全退出，请关闭启动窗口后重新打开。") from None


class CLIBackend:
    def __init__(self, config: Config, name: str):
        self.config = config
        self.name = name
        self.settings = getattr(config, name)
        self.cancelled = threading.Event()
        self._scope_lock = threading.Lock()
        self._scopes = {}

    def close(self):
        _close_backend(self)

    def _run_cli(self, command, prompt, cwd, timeout):
        with _backend_scope(self):
            return _run_cli(command, prompt, cwd, timeout)

    def generate(self, prompt: str, schema: dict) -> dict:
        with _backend_scope(self):
            return self._generate(prompt, schema)

    def _generate(self, prompt: str, schema: dict) -> dict:
        _check_input(prompt, schema)
        executable = shutil.which(self.settings.executable)
        if not executable:
            name = "Claude Code" if self.name == "claude" else "Codex"
            raise BackendError(f"未找到 {name} 命令行程序，请先安装并在本机登录，或切换到其他后端。")
        deadline = time.monotonic() + self.config.timeout_seconds
        try:
            with tempfile.TemporaryDirectory(prefix="paperread-model-") as directory:
                root = Path(directory)
                cwd = root / "work"
                cwd.mkdir()
                def probe(arguments):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise BackendError(TIMEOUT_MESSAGE)
                    return self._run_cli([executable, *arguments], "", cwd, min(10, remaining))
                if self.name == "claude":
                    help_text = probe(["--help"])
                    required = (b"--tools", b"--safe-mode", b"--strict-mcp-config", b"--json-schema",
                                b"--disable-slash-commands", b"--no-session-persistence", b"--effort")
                    if not all(flag in help_text for flag in required):
                        raise BackendError("Claude Code 版本太旧，缺少所需的隔离或推理强度参数；请更新后重试。")
                    command = self._claude_command(executable, schema)
                else:
                    help_text = probe(["exec", "--help"])
                    required = (b"--ignore-user-config", b"--ignore-rules", b"--ephemeral", b"--output-schema")
                    if not all(flag in help_text for flag in required):
                        raise BackendError("Codex 版本太旧，缺少所需的隔离参数；请更新后重试。")
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
                        raise BackendError("Codex 缺少所需的工具禁用功能，或被本机策略强制开启；请更新 Codex 或检查本机策略。")
                if self.settings.model:
                    command.extend(["--model", self.settings.model])
                if self.name == "codex":
                    command.append("-")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise BackendError(TIMEOUT_MESSAGE)
                output = self._run_cli(command, prompt, cwd, remaining)
                return self._parse_output(output)
        except OSError:
            raise BackendError("无法创建模型的临时工作目录，请检查本机磁盘空间和临时目录权限。") from None

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
            "--effort", FIXED_REASONING_EFFORT,
        ]

    @staticmethod
    def _codex_command(executable: str, schema_path: Path) -> list[str]:
        # read-only alone still permits reading user files. Disable the tool
        # surfaces separately, and ignore customization/rules from user config.
        overrides = {
            "model_reasoning_effort": json.dumps(FIXED_REASONING_EFFORT),
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
                raise BackendError(_cli_failure(json.dumps(response), "Claude Code"))
            result = response.get("structured_output")
            if isinstance(result, dict):
                return result
            if isinstance(response.get("result"), str):
                return _json_object(response["result"])
            raise BackendError("Claude Code 未返回完整的结构化讲解，请重试或更换模型。")
        result = None
        completed = False
        try:
            for line in output.splitlines():
                if not line.strip():
                    continue
                event = _json_object(line)
                if event.get("type") in ("error", "turn.failed"):
                    raise BackendError(_cli_failure(json.dumps(event), "Codex"))
                if event.get("type") == "turn.completed":
                    completed = True
                item = event.get("item", {})
                if (event.get("type") == "item.completed" and isinstance(item, dict)
                        and item.get("type") == "agent_message" and isinstance(item.get("text"), str)):
                    result = item["text"]
        except (TypeError, ValueError):
            raise BackendError("Codex 返回的事件格式不正确，请更新后重试或更换模型。") from None
        if result is None or not completed:
            raise BackendError("Codex 未返回完整的结构化讲解，请重试或更换模型。")
        return _json_object(result)


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never resend the Authorization header or paper text at a redirected URL.
        return None


def _shutdown_socket(sock):
    if sock is not None:
        try:
            # Wake blocked HTTP reads before buffered readers are closed by the
            # worker itself. Use the base socket operation for TLS too: changing
            # SSLSocket._sslobj or closing its fd while a handshake is running
            # can race that worker. Shutdown disconnects the peer immediately.
            socket.socket.shutdown(sock, socket.SHUT_RDWR)
        except OSError:
            # An unconnected socket cannot be shut down; close a pending connect.
            sock.close()


def _shutdown_response(response):
    raw = getattr(getattr(response, "fp", None), "raw", None)
    _shutdown_socket(getattr(raw, "_sock", None))


class _CancellableConnection:
    """Expose connecting/header-reading sockets before urllib returns a body."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._task = getattr(_TASK_CONTEXT, "cancellation", None)
        self._connecting_socket = None
        self._create_connection = self._connect_socket
        if self._task is not None:
            self._task.register(self, self._abort)

    def _abort(self):
        _shutdown_socket(self.sock)
        if self._connecting_socket is not self.sock:
            _shutdown_socket(self._connecting_socket)

    def _connect_socket(self, address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None):
        last_error = None
        for family, kind, proto, _, target in socket.getaddrinfo(*address, 0, socket.SOCK_STREAM):
            sock = socket.socket(family, kind, proto)
            try:
                if self._task is not None:
                    with self._task._lock:
                        self._task.check()
                        self._connecting_socket = sock
                if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
                    sock.settimeout(timeout)
                if source_address:
                    sock.bind(source_address)
                sock.connect(target)
                if self._task is not None:
                    self._task.check()
                return sock
            except OSError as exc:
                last_error = exc
                sock.close()
                if self._task is not None:
                    self._task.check()
            except BaseException:
                sock.close()
                raise
        if last_error is not None:
            raise last_error
        raise OSError("No connection address")


class _CancellableHTTPConnection(_CancellableConnection, http.client.HTTPConnection):
    pass


class _CancellableHTTPSConnection(_CancellableConnection, http.client.HTTPSConnection):
    def connect(self):
        # Publish the TLS socket before the handshake, which may itself stall.
        http.client.HTTPConnection.connect(self)
        hostname = self._tunnel_host or self.host
        if self._task is None:
            self.sock = self._context.wrap_socket(self.sock, server_hostname=hostname)
            return
        with self._task._lock:
            self._task.check()
            self.sock = self._context.wrap_socket(self.sock, server_hostname=hostname, do_handshake_on_connect=False)
            # HTTPConnection may transfer ownership to a response and clear
            # self.sock; keep the actual TLS socket reachable during handoff.
            self._connecting_socket = self.sock
        self.sock.do_handshake()
        self._task.check()


class _CancellableHandler:
    def _open(self, connection_class, req, **kwargs):
        connection = None
        def factory(*args, **connection_kwargs):
            nonlocal connection
            connection = connection_class(*args, **connection_kwargs)
            return connection
        try:
            response = self.do_open(factory, req, **kwargs)
            if connection._task is not None:
                try:
                    # Register the body before discarding its connection, so
                    # cancellation cannot slip between those ownership steps.
                    connection._task.register(response, lambda: _shutdown_response(response))
                except BaseException:
                    response.close()
                    raise
            return response
        finally:
            if connection is not None and connection._task is not None:
                connection._task.discard(connection)


class _CancellableHTTPHandler(_CancellableHandler, request.HTTPHandler):
    def http_open(self, req):
        return self._open(_CancellableHTTPConnection, req)


class _CancellableHTTPSHandler(_CancellableHandler, request.HTTPSHandler):
    def https_open(self, req):
        return self._open(_CancellableHTTPSConnection, req, context=self._context)


def _api_failure(code):
    if code in (401, 403):
        return f"API 认证失败或没有访问权限（HTTP {code}），请检查本机配置中的 API 密钥和账号权限。"
    if code in (400, 404, 422):
        return (f"API 请求未被接受（HTTP {code}）；模型名可能不对，请检查模型、API 地址、响应格式，"
                "以及模型是否支持固定的 high reasoning_effort。")
    if code in (408, 504):
        return TIMEOUT_MESSAGE
    if code == 429:
        return "API 请求过于频繁或额度不足（HTTP 429），请稍后重试并检查账号额度。"
    if 300 <= code <= 399:
        return "API 地址发生跳转，已停止发送；请在本机配置中填写最终的 API 地址后重试。"
    return f"模型 API 暂时无法完成请求（HTTP {code}），请稍后重试或更换后端。"


class APIBackend:
    def __init__(self, config: Config):
        self.config = config
        self.settings = config.api
        self.opener = request.build_opener(_NoRedirect(), _CancellableHTTPHandler(), _CancellableHTTPSHandler())
        self.cancelled = threading.Event()
        self._scope_lock = threading.Lock()
        self._scopes = {}
        self._response_lock = threading.Lock()
        self._responses = set()

    def close(self):
        _close_backend(self)

    def generate(self, prompt: str, schema: dict) -> dict:
        with _backend_scope(self) as cancellation:
            return self._generate(prompt, schema, cancellation)

    def _generate(self, prompt: str, schema: dict, cancellation: TaskCancellation) -> dict:
        _check_input(prompt, schema)
        settings = self.settings
        if not settings.base_url or not settings.model or not (settings.api_key or is_loopback_url(settings.base_url)):
            raise BackendError("请先在本机 config.json 中填写 API 的 base_url、model 和 api_key，再重试。")
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
            "reasoning_effort": FIXED_REASONING_EFFORT,
            "stream": False,
        }
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if settings.api_key:
            headers["Authorization"] = "Bearer " + settings.api_key
        endpoint = settings.base_url.rstrip("/") + "/chat/completions"
        req = request.Request(endpoint, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
        deadline = time.monotonic() + self.config.timeout_seconds
        for attempt in range(self.config.max_retries + 1):
            cancellation.check()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise BackendError(TIMEOUT_MESSAGE)
            try:
                with self.opener.open(req, timeout=remaining) as response:
                    cancellation.register(response, lambda: _shutdown_response(response))
                    with self._response_lock:
                        self._responses.add(response)
                    try:
                        output = self._read_response(response, deadline)
                    finally:
                        cancellation.discard(response)
                        with self._response_lock:
                            self._responses.discard(response)
                cancellation.check()
                return self._parse_response(_json_object(output))
            except error.HTTPError as exc:
                code = exc.code
                cancellation.discard(exc.fp)
                exc.close()
                cancellation.check()
                retryable = code in (408, 429) or 500 <= code <= 599
                message = _api_failure(code)
            except (error.URLError, TimeoutError, OSError, http.client.HTTPException) as exc:
                cancellation.check()
                retryable = True
                reason = getattr(exc, "reason", exc)
                message = (TIMEOUT_MESSAGE if isinstance(reason, TimeoutError) else
                           "无法连接模型 API，请检查本机网络、代理和 API 地址后重试。")
            except (ValueError, UnicodeError, RecursionError):
                cancellation.check()
                raise BackendError("模型 API 返回的内容格式不正确，请重试或检查响应格式配置。") from None
            if not retryable or attempt >= self.config.max_retries:
                raise BackendError(message) from None
            delay = min(0.25 * (2 ** attempt), max(0, deadline - time.monotonic()))
            cancellation.event.wait(delay)
        raise BackendError("模型 API 请求失败，请稍后重试或更换后端。")

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
                raise BackendError(TIMEOUT_MESSAGE)
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
                raise BackendError("API 返回的内容过长，已停止本轮；请缩小讲解范围后重试。")
            chunks.append(chunk)

    @staticmethod
    def _parse_response(response: dict) -> dict:
        try:
            choice = response["choices"][0]
            message = choice["message"]
            if message.get("refusal") or choice.get("finish_reason") not in (None, "stop"):
                raise BackendError("模型 API 未完成可用的讲解，请重试或更换模型。")
            content = message["content"]
            if isinstance(content, list):
                content = "".join(part["text"] for part in content if part.get("type") == "text")
            if not isinstance(content, str):
                raise BackendError("模型 API 未返回 JSON 文本，请检查响应格式配置或更换模型。")
            return _json_object(content)
        except (KeyError, IndexError, TypeError, AttributeError):
            raise BackendError("模型 API 返回的响应结构不正确，请检查 API 是否兼容或更换后端。") from None


def create_backend(config: Config):
    if config.backend == "api":
        return APIBackend(config)
    if config.backend in ("claude", "codex"):
        return CLIBackend(config, config.backend)
    raise BackendError("无法识别所选的模型后端，请重新选择。")
