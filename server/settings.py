"""Safe settings views and atomic, local-only backend selection changes."""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import tempfile
import time
from urllib.parse import unquote, urlsplit

from .backends import backend_summary
from .config import Config, ConfigError, config_from_data, read_config


def public_settings(config: Config, selected: str | None = None) -> dict:
    """Expose only the agreed public fields; never serialize Config itself."""
    summary = backend_summary(config)
    return {
        "backend": config.backend if selected is None else selected,
        "backends": summary["backends"],
        "api": {"base_url": _public_api_text(config.api.base_url, config.api.api_key),
                "model": _public_api_text(config.api.model, config.api.api_key),
                "key_configured": bool(config.api.api_key)},
    }


def _public_api_text(value: str, secret: str) -> str:
    # Model names can themselves be configured as local file paths, and some
    # providers put credentials in URL path segments. Neither belongs in HTTP.
    decoded = unquote(value)
    if ((secret and (secret in value or secret in decoded))
            or re.match(r"^(?:/|~[/\\]|[A-Za-z]:[/\\]|\\\\)", decoded)):
        return ""
    try:
        parsed = urlsplit(value)
        if parsed.username is not None or parsed.password is not None or parsed.scheme == "file":
            return ""
    except ValueError:
        return ""
    return value


@contextmanager
def _config_lock(home: Path, timeout: float = 10):
    """Coordinate cooperating processes using the same scheme as profile writes."""
    path = home / ".config.lock"
    try:
        home.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + timeout
        while True:
            try:
                descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                break
            except FileExistsError:
                if time.monotonic() >= deadline:
                    raise ConfigError("设置正在保存，请稍后重试。") from None
                time.sleep(0.05)
    except OSError:
        raise ConfigError("无法准备本机设置，请检查配置文件的访问权限。") from None
    try:
        try:
            with os.fdopen(descriptor, "w") as stream:
                stream.write(str(os.getpid()))
        except OSError:
            raise ConfigError("无法锁定本机设置，请稍后重试。") from None
        yield
    finally:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            # Replacement may already have succeeded; never report a failed
            # transaction after committing the file. A stale lock is bounded
            # by the next request's timeout and never contains configuration.
            pass


def _atomic_write(home: Path, raw: dict) -> None:
    temporary = None
    try:
        encoded = (json.dumps(raw, ensure_ascii=False, allow_nan=False, indent=2) + "\n").encode("utf-8")
        if len(encoded) > 65_536:
            raise ConfigError("配置文件过大，无法保存设置。")
        with tempfile.NamedTemporaryFile(dir=home, prefix=".config.json.", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, home / "config.json")
    except (OSError, ValueError, UnicodeError, RecursionError):
        raise ConfigError("无法保存本机设置，请检查配置文件的内容和访问权限。") from None
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


@contextmanager
def prepare_backend_change(home: Path, name: str):
    """Yield a candidate under lock; persist only if runtime preparation succeeds.

    Build the replacement backend inside this context, then publish it only
    after the context exits successfully. No model process or request is run.
    """
    if name not in ("claude", "codex", "api"):
        raise ConfigError("请选择 Claude、Codex 或 API 后端。")
    home = Path(home).expanduser().resolve()
    with _config_lock(home):
        try:
            raw = read_config(home)
            # Existing invalid known fields must not be repaired implicitly.
            config_from_data(home, raw)
            updated = {**raw, "backend": name}
            candidate = config_from_data(home, updated)
        except ConfigError:
            raise ConfigError("本机配置文件内容不合法，请修正后重试。") from None
        selected = next(item for item in backend_summary(candidate)["backends"] if item["name"] == name)
        if not selected["available"]:
            if name == "api":
                raise ConfigError("API 尚未在本机配置文件中填写完整。")
            label = "Claude" if name == "claude" else "Codex"
            raise ConfigError(f"没有找到 {label} 的命令行程序，请先安装后重试。")
        yield candidate
        _atomic_write(home, updated)


def write_backend(home: Path, name: str) -> Config:
    """Validate availability and preserve every other field while saving."""
    with prepare_backend_change(home, name) as config:
        pass
    return config
