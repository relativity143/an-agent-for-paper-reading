"""Local-only model configuration. Never serialize Config into HTTP responses."""

from dataclasses import dataclass, field
import ipaddress
import json
import math
from pathlib import Path
import re
from urllib.parse import urlsplit


class ConfigError(ValueError):
    """A safe, user-visible configuration error (no values or secrets)."""


# Verified locally from Claude Code --help and the Codex CLI model catalogue
# (2026-11-15). Readers may replace these lists in their own config.json.
DEFAULT_MODEL_SUGGESTIONS = {
    "claude": ("sonnet", "opus"),
    "codex": ("gpt-6.1-sol", "gpt-6-astra", "gpt-6.1-terra"),
}
DEFAULT_TIMEOUT_SECONDS = 600
_CLI_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:\[\]-]{0,99}")


def validate_cli_model(value: object) -> str | None:
    """Use the same safe CLI argument contract for requests and local files."""
    if value is None:
        return None
    if not isinstance(value, str) or _CLI_MODEL.fullmatch(value) is None:
        raise ConfigError("模型名须以字母或数字开头，只能含字母、数字和 . _ : - [ ]，最长 100 个字符；默认模型请用 null。")
    return value


def validate_timeout(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 30 <= value <= 600:
        raise ConfigError("讲解时限须为 30 到 600 秒的整数。")
    return value


@dataclass(frozen=True)
class CLIConfig:
    executable: str
    model: str | None = None
    model_suggestions: tuple[str, ...] = ()


@dataclass(frozen=True)
class APIConfig:
    base_url: str = ""
    model: str = ""
    api_key: str = field(default="", repr=False)
    response_format: str = "json_schema"


@dataclass(frozen=True)
class Config:
    home: Path = field(repr=False)
    backend: str = "claude"
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    max_retries: int = 1
    claude: CLIConfig = field(default_factory=lambda: CLIConfig("claude", model_suggestions=DEFAULT_MODEL_SUGGESTIONS["claude"]))
    codex: CLIConfig = field(default_factory=lambda: CLIConfig("codex", model_suggestions=DEFAULT_MODEL_SUGGESTIONS["codex"]))
    api: APIConfig = field(default_factory=APIConfig)


def is_loopback_url(value: str) -> bool:
    try:
        hostname = urlsplit(value).hostname
        return hostname == "localhost" or ipaddress.ip_address(hostname).is_loopback
    except (TypeError, ValueError):
        return False


def _object(value: object, label: str) -> dict:
    # Reader-added fields are allowed in local configuration. Known fields are
    # still validated below; writers must preserve the original JSON object.
    if not isinstance(value, dict):
        raise ConfigError(f"{label} 配置必须是 JSON 对象。")
    return value


def _string(value: object, label: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise ConfigError(f"{label} 配置必须是有效的字符串。")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ConfigError(f"{label} 配置不能含控制字符。")
    return value


def _cli(raw: object, name: str) -> CLIConfig:
    data = _object(raw, name)
    executable = _string(data.get("executable", name), "命令行程序")
    model = validate_cli_model(data.get("model"))
    suggestions = data.get("model_suggestions", list(DEFAULT_MODEL_SUGGESTIONS[name]))
    if not isinstance(suggestions, list) or any(not isinstance(item, str) for item in suggestions):
        raise ConfigError("模型建议列表必须是字符串数组。")
    models = tuple(validate_cli_model(item) for item in suggestions)
    return CLIConfig(executable=executable, model=model, model_suggestions=models)


def _api(raw: object) -> APIConfig:
    data = _object(raw, "API")
    values = {
        key: _string(data.get(key, ""), "API " + key, allow_empty=True)
        for key in ("base_url", "model", "api_key")
    }
    base_url = values["base_url"].rstrip("/")
    if base_url:
        try:
            parsed = urlsplit(base_url)
            valid = (
                bool(parsed.hostname)
                and parsed.username is None
                and parsed.password is None
                and not parsed.query
                and not parsed.fragment
                and (parsed.scheme == "https" or (parsed.scheme == "http" and is_loopback_url(base_url)))
            )
            # Accessing port performs urllib's syntax/range validation.
            parsed.port
        except ValueError:
            valid = False
        if not valid:
            raise ConfigError("API 地址须使用 HTTPS（本机回环地址可用 HTTP），且不能含用户名、密码、查询参数或片段。")
    response_format = data.get("response_format", "json_schema")
    if response_format not in ("json_schema", "json_object"):
        raise ConfigError("API 的 response_format 须为 json_schema 或 json_object。")
    return APIConfig(**{**values, "base_url": base_url}, response_format=response_format)


def read_config(home: Path) -> dict:
    """Read the original JSON object, including reader-added fields; never write."""
    home = Path(home).expanduser().resolve()

    def reject_constant(_value):
        raise ValueError("Non-finite JSON number")

    def finite_float(value):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("Non-finite JSON number")
        return number

    def unique_object(pairs):
        data = {}
        for key, value in pairs:
            if key in data:
                raise ValueError("Duplicate JSON key")
            data[key] = value
        return data

    try:
        path = home / "config.json"
        with path.open("rb") as handle:
            encoded = handle.read(65_537)
        if len(encoded) > 65_536:
            raise ConfigError("本机配置文件过大，请缩减到 64 KiB 以内。")
        raw = json.loads(encoded, parse_constant=reject_constant, parse_float=finite_float,
                         object_pairs_hook=unique_object)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, UnicodeError, RecursionError):
        raise ConfigError("无法读取有效的本机 config.json，请检查内容和访问权限。") from None
    return _object(raw, "模型")


def config_from_data(home: Path, raw: dict) -> Config:
    """Validate known fields without discarding or acting on reader-added data."""
    home = Path(home).expanduser().resolve()
    raw = _object(raw, "模型")
    backend = raw.get("backend", "claude")
    if backend not in ("claude", "codex", "api"):
        raise ConfigError("模型后端须为 claude、codex 或 api。")
    timeout = validate_timeout(raw.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS))
    retries = raw.get("max_retries", 1)
    if isinstance(retries, bool) or not isinstance(retries, int) or not 0 <= retries <= 2:
        raise ConfigError("max_retries 须为 0 到 2 的整数。")
    return Config(
        home=home,
        backend=backend,
        timeout_seconds=timeout,
        max_retries=retries,
        claude=_cli(raw.get("claude", {}), "claude"),
        codex=_cli(raw.get("codex", {}), "codex"),
        api=_api(raw.get("api", {})),
    )


def load_config(home: Path) -> Config:
    """Read only ``home/config.json``; missing files select Claude with defaults.

    ``home`` is resolved by the application from PAPERREAD_HOME (or ~/.paperread).
    Credentials are never fetched from a browser, request body, or environment.
    """
    return config_from_data(home, read_config(home))
