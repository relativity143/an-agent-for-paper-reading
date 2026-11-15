"""Small, local-only helpers shared by the command-line and Finder launcher."""
import errno
import http.client
import json
import os
from pathlib import Path
import re
import secrets
import stat
import tempfile
import webbrowser

from . import __version__
from .http import create_server

DEFAULT_PORT = 8765
MAX_HEALTH_BYTES = 8192
SERVICE_RECORD = ".server.json"
INSTANCE_HEADER = "X-PaperReading-Instance"


def existing_service(port, *, instance=None):
    """Recognize this server, never a random listener or the development fixture.

    A direct connection deliberately ignores proxy settings and redirects.
    This is a local identity check, not authentication against hostile software.
    """
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=0.5)
    try:
        connection.request("GET", "/api/health", headers={"Accept": "application/json"})
        response = connection.getresponse()
        if (response.status != 200
                or response.getheader("Server", "").split() != ["PaperReading"]
                or response.getheader("Content-Type", "").split(";", 1)[0] != "application/json"):
            return None
        if instance is not None and response.getheader(INSTANCE_HEADER, "") != instance:
            return None
        body = response.read(MAX_HEALTH_BYTES + 1)
        if len(body) > MAX_HEALTH_BYTES:
            return None
        info = json.loads(body)
        if (not isinstance(info, dict) or info.get("version") != __version__
                or info.get("backend") not in ("claude", "codex", "api")
                or not isinstance(info.get("backends"), list)):
            return None
        return info
    except (OSError, http.client.HTTPException, ValueError, UnicodeError, RecursionError):
        return None
    finally:
        connection.close()


def _read_local_file(path, limit=4096):
    """Read bounded regular files without following a replaced metadata symlink."""
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(descriptor, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("Not a regular file")
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError("Local record is too large")
    return data


def recorded_service(home):
    """Match this home's live lock, discovery record, and actual HTTP listener."""
    home = Path(home).resolve()
    try:
        record = json.loads(_read_local_file(home / SERVICE_RECORD))
        if (not isinstance(record, dict) or record.get("version") != __version__
                or record.get("home") != str(home)
                or type(record.get("pid")) is not int or record["pid"] <= 0
                or not isinstance(record.get("instance"), str)
                or re.fullmatch(r"[0-9a-f]{32}", record["instance"]) is None
                or not isinstance(record.get("url"), str)):
            return None
        match = re.fullmatch(r"http://127\.0\.0\.1:([1-9][0-9]{0,4})", record["url"])
        if match is None or int(match[1]) > 65535:
            return None
        lock = home / ".server.lock"
        if _read_local_file(lock, 32).strip() != str(record["pid"]).encode("ascii"):
            return None
        # ESRCH means the lock/record survived a crash. Never signal or remove
        # that process or its lock here; PID reuse still requires the nonce.
        os.kill(record["pid"], 0)
        info = existing_service(int(match[1]), instance=record["instance"])
        if info is not None and _read_local_file(lock, 32).strip() == str(record["pid"]).encode("ascii"):
            return record["url"], info
    except (OSError, ValueError, OverflowError, UnicodeError, RecursionError):
        pass
    return None


def find_existing_service(home, port, *, allow_legacy=False):
    found = recorded_service(home)
    if found is not None:
        return found
    if allow_legacy:
        # Older launchers have no discovery record/header. Preserve their
        # default-home behavior, but never borrow a marked different home or
        # an unmarked listener when PAPERREAD_HOME explicitly selects a home.
        info = existing_service(port, instance="")
        if info is not None:
            return f"http://127.0.0.1:{port}", info
    return None


def publish_service(server, home):
    """Publish only while holding this home's lock; never expose its path in HTTP."""
    home = Path(home).resolve()
    instance = secrets.token_hex(16)
    record = {"version": __version__, "pid": os.getpid(), "home": str(home),
              "url": f"http://127.0.0.1:{server.server_port}", "instance": instance}
    original_handler = server.RequestHandlerClass

    class DiscoveryHandler(original_handler):
        def end_headers(self):
            self.send_header(INSTANCE_HEADER, instance)
            super().end_headers()

    server.RequestHandlerClass = DiscoveryHandler
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=home, prefix=".server-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write((json.dumps(record) + "\n").encode("utf-8"))
        os.replace(temporary, home / SERVICE_RECORD)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return record


def remove_service(home, record):
    """A terminating launcher may remove only its own unchanged discovery record."""
    if record is None:
        return
    path = Path(home) / SERVICE_RECORD
    try:
        if json.loads(_read_local_file(path)) == record:
            path.unlink()
    except (OSError, ValueError, UnicodeError, RecursionError):
        pass


def bind_server(app, port):
    """Let the OS reserve an unused port if another application owns this one."""
    try:
        return create_server(app, port=port)
    except OSError as error:
        if error.errno != errno.EADDRINUSE:
            raise
        return create_server(app, port=0)


def open_page(url):
    try:
        opened = webbrowser.open(url)
    except (OSError, webbrowser.Error):
        opened = False
    if not opened:
        print(f"浏览器未能自动打开，请手动访问：{url}", flush=True)
