"""Local-only model configuration. Never serialize Config into HTTP responses."""

from dataclasses import dataclass, field
import ipaddress
import json
from pathlib import Path
from urllib.parse import urlsplit


class ConfigError(ValueError):
    """A safe, user-visible configuration error (no values or secrets)."""


@dataclass(frozen=True)
class CLIConfig:
    executable: str
    model: str | None = None


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
    timeout_seconds: float = 120.0
    max_retries: int = 1
    claude: CLIConfig = field(default_factory=lambda: CLIConfig("claude"))
    codex: CLIConfig = field(default_factory=lambda: CLIConfig("codex"))
    api: APIConfig = field(default_factory=APIConfig)


def is_loopback_url(value: str) -> bool:
    try:
        hostname = urlsplit(value).hostname
        return hostname == "localhost" or ipaddress.ip_address(hostname).is_loopback
    except (TypeError, ValueError):
        return False


def _object(value: object, fields: set[str], label: str) -> dict:
    if not isinstance(value, dict) or set(value) - fields:
        raise ConfigError(f"Invalid {label} configuration fields.")
    return value


def _string(value: object, label: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise ConfigError(f"Invalid {label} configuration.")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ConfigError(f"Invalid {label} configuration.")
    return value


def _cli(raw: object, name: str) -> CLIConfig:
    data = _object(raw, {"executable", "model"}, name)
    executable = _string(data.get("executable", name), "CLI executable")
    model = data.get("model")
    if model is not None:
        model = _string(model, "CLI model")
    return CLIConfig(executable=executable, model=model)


def _api(raw: object) -> APIConfig:
    data = _object(raw, {"base_url", "model", "api_key", "response_format"}, "API")
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
            raise ConfigError("API base_url must be HTTPS, or loopback HTTP, without credentials or a query.")
    response_format = data.get("response_format", "json_schema")
    if response_format not in ("json_schema", "json_object"):
        raise ConfigError("API response_format must be json_schema or json_object.")
    return APIConfig(**{**values, "base_url": base_url}, response_format=response_format)


def load_config(home: Path) -> Config:
    """Read only ``home/config.json``; missing files select Claude with defaults.

    ``home`` is resolved by the application from PAPERREAD_HOME (or ~/.paperread).
    Credentials are never fetched from a browser, request body, or environment.
    """
    home = Path(home).expanduser().resolve()
    try:
        path = home / "config.json"
        with path.open("rb") as handle:
            encoded = handle.read(65_537)
        if len(encoded) > 65_536:
            raise ConfigError("Configuration file is too large.")
        raw = json.loads(encoded)
    except FileNotFoundError:
        return Config(home=home)
    except (OSError, ValueError, UnicodeError, RecursionError):
        raise ConfigError("Cannot read a valid local config.json file.") from None
    raw = _object(raw, {"backend", "timeout_seconds", "max_retries", "claude", "codex", "api"}, "model")
    backend = raw.get("backend", "claude")
    if backend not in ("claude", "codex", "api"):
        raise ConfigError("backend must be claude, codex, or api.")
    timeout = raw.get("timeout_seconds", 120)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 1 <= timeout <= 600:
        raise ConfigError("timeout_seconds must be between 1 and 600.")
    retries = raw.get("max_retries", 1)
    if isinstance(retries, bool) or not isinstance(retries, int) or not 0 <= retries <= 2:
        raise ConfigError("max_retries must be an integer between 0 and 2.")
    return Config(
        home=home,
        backend=backend,
        timeout_seconds=float(timeout),
        max_retries=retries,
        claude=_cli(raw.get("claude", {}), "claude"),
        codex=_cli(raw.get("codex", {}), "codex"),
        api=_api(raw.get("api", {})),
    )
