"""Small, local-only helpers shared by the command-line and Finder launcher."""
import errno
import http.client
import json
import webbrowser

from . import __version__
from .http import create_server

DEFAULT_PORT = 8765
MAX_HEALTH_BYTES = 8192


def existing_service(port):
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
