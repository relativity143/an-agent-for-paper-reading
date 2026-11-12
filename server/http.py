"""Small same-origin HTTP boundary for the local paper-reading application."""
from __future__ import annotations

import json
import mimetypes
import os
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import shutil
import socket
import stat
from urllib.parse import unquote, urlsplit

MAX_BODY_BYTES = 1024 * 1024
SOCKET_TIMEOUT = 10
_STATIC_TYPES = {
    ".html", ".css", ".js", ".mjs", ".svg", ".png", ".jpg", ".jpeg",
    ".gif", ".webp", ".ico", ".woff", ".woff2", ".ttf", ".json",
}


def _reject_json_constant(value):
    raise ValueError("Invalid JSON constant")


class _HTTPFailure(Exception):
    def __init__(self, status: int, message: str):
        self.status, self.message = status, message


class _LocalServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(SOCKET_TIMEOUT)
        return connection, address

    def handle_error(self, request, client_address):
        # Do not print request contents, private paths, or backend exceptions.
        pass


def create_server(app, *, port=8765, web_root=None) -> ThreadingHTTPServer:
    """Bind only IPv4 loopback; ``port=0`` is supported for isolated tests."""
    public_root = (Path(web_root) if web_root is not None else
                   Path(__file__).resolve().parents[1] / "web").resolve()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "PaperReading"
        sys_version = ""

        def log_message(self, format, *args):
            pass

        def _headers(self, status, content_type, length=None):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            if length is not None:
                self.send_header("Content-Length", str(length))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "SAMEORIGIN")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Connection", "close")
            self.end_headers()
            self._response_started = True
            self.close_connection = True

        def _bytes(self, status, body, content_type):
            self._headers(status, content_type, len(body))
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, status, body):
            data = json.dumps(body, ensure_ascii=False, allow_nan=False,
                              separators=(",", ":")).encode("utf-8")
            self._bytes(status, data, "application/json; charset=utf-8")

        def _error(self, status, message):
            # A streaming failure cannot become a second HTTP response.
            if not getattr(self, "_response_started", False):
                self._json(status, {"error": message})

        def send_error(self, code, message=None, explain=None):
            # BaseHTTPRequestHandler otherwise sends HTML and echoes messages.
            phrase = HTTPStatus(code).phrase if code in HTTPStatus._value2member_map_ else "Request failed"
            self._error(code, phrase)

        def _single_header(self, name):
            values = self.headers.get_all(name, [])
            if len(values) > 1:
                raise _HTTPFailure(400, "Duplicate request header.")
            return values[0].strip() if values else None

        def _check_origin(self):
            host = self._single_header("Host")
            bound_port = self.server.server_port
            allowed = {f"127.0.0.1:{bound_port}", f"localhost:{bound_port}"}
            if bound_port == 80:
                allowed.update({"127.0.0.1", "localhost"})
            if host not in allowed:
                raise _HTTPFailure(403, "Unrecognized local host.")
            origin = self._single_header("Origin")
            if origin is not None and origin != f"http://{host}":
                raise _HTTPFailure(403, "Only same-origin requests are accepted.")
            fetch_site = self._single_header("Sec-Fetch-Site")
            if fetch_site is not None and fetch_site not in {"same-origin", "none"}:
                raise _HTTPFailure(403, "Only same-origin requests are accepted.")
            referer = self._single_header("Referer")
            if referer:
                parsed = urlsplit(referer)
                if parsed.scheme != "http" or parsed.netloc != host:
                    raise _HTTPFailure(403, "Only same-origin requests are accepted.")

        def _body_length(self):
            if self._single_header("Transfer-Encoding") is not None:
                raise _HTTPFailure(400, "Transfer-Encoding is not supported.")
            length = self._single_header("Content-Length")
            if length is None:
                raise _HTTPFailure(411, "A Content-Length header is required.")
            if not length.isascii() or not length.isdigit() or len(length) > 12:
                raise _HTTPFailure(400, "Invalid Content-Length.")
            size = int(length)
            if size > MAX_BODY_BYTES:
                raise _HTTPFailure(413, "The request is too large.")
            content_type = self._single_header("Content-Type") or ""
            if content_type.split(";", 1)[0].strip().lower() != "application/json":
                raise _HTTPFailure(415, "Use application/json.")
            return size

        def handle_expect_100(self):
            try:
                self._check_origin()
                self._body_length()
            except _HTTPFailure as error:
                self._error(error.status, error.message)
                return False
            self.send_response_only(100)
            self.end_headers()
            return True

        def _read_json(self):
            length = self._body_length()
            data = self.rfile.read(length)
            if len(data) != length:
                raise _HTTPFailure(400, "Incomplete request body.")
            try:
                value = json.loads(data.decode("utf-8"), parse_constant=_reject_json_constant)
            except (UnicodeDecodeError, ValueError, RecursionError):
                raise _HTTPFailure(400, "Invalid JSON body.") from None
            if not isinstance(value, dict):
                raise _HTTPFailure(400, "The JSON body must be an object.")
            return value

        def _path(self):
            if not self.path.startswith("/") or self.path.startswith("//"):
                raise _HTTPFailure(400, "Invalid request path.")
            try:
                path = unquote(urlsplit(self.path).path, errors="strict")
            except (UnicodeError, ValueError):
                raise _HTTPFailure(400, "Invalid request path.") from None
            parts = path.split("/")[1:]
            if any(part in {".", ".."} or "\\" in part or
                   any(ord(character) < 32 or ord(character) == 127 for character in part)
                   for part in parts):
                raise _HTTPFailure(404, "Not found.")
            return path, parts

        def _file(self, path, content_type):
            # Open before headers so missing files still produce a JSON 404.
            with Path(path).open("rb") as source:
                info = os.fstat(source.fileno())
                if not stat.S_ISREG(info.st_mode):
                    raise FileNotFoundError()
                self._headers(200, content_type, info.st_size)
                if self.command != "HEAD":
                    shutil.copyfileobj(source, self.wfile, length=64 * 1024)

        def _static(self, path, parts):
            if path in {"/", "/index.html"}:
                parts = ["index.html"]
            if any(not part or part.startswith(".") for part in parts):
                raise FileNotFoundError()
            candidate = public_root.joinpath(*parts).resolve()
            if not candidate.is_relative_to(public_root):
                raise FileNotFoundError()
            if candidate.suffix.lower() not in _STATIC_TYPES:
                raise FileNotFoundError()
            if candidate.suffix.lower() == ".json" and parts[0] != "mock":
                raise FileNotFoundError()
            if parts == ["index.html"] and not candidate.exists():
                self._bytes(200, b"Paper Reading server is running. The web interface is not installed.\n",
                            "text/plain; charset=utf-8")
                return
            content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
            if candidate.suffix.lower() in {".js", ".mjs"}:
                content_type = "text/javascript"
            if content_type.startswith("text/"):
                content_type += "; charset=utf-8"
            self._file(candidate, content_type)

        def _events(self, task_id):
            # Resolve the task before sending 200, even if events is a generator.
            app.get_task(task_id)
            cursor = self._single_header("Last-Event-ID") or "0"
            if not cursor.isascii() or not cursor.isdigit() or len(cursor) > 19:
                raise _HTTPFailure(400, "Invalid Last-Event-ID.")
            last_id = int(cursor)
            events = app.task_events(task_id, after=last_id)
            self._headers(200, "text/event-stream; charset=utf-8")
            try:
                for event in events:
                    if event is None:
                        self.wfile.write(b": heartbeat\n\n")
                        self.wfile.flush()
                        continue
                    event_id, kind, payload = event
                    if not isinstance(event_id, int) or isinstance(event_id, bool) or event_id < 1:
                        raise ValueError("Invalid event ID")
                    if event_id <= last_id:
                        continue
                    if kind not in {"status", "result", "error"}:
                        raise ValueError("Invalid event type")
                    data = (json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
                            if kind == "result" else str(payload))
                    data = data.replace("\r\n", "\n").replace("\r", "\n")
                    frame = f"id: {event_id}\nevent: {kind}\n" + "".join(f"data: {line}\n" for line in data.split("\n")) + "\n"
                    self.wfile.write(frame.encode("utf-8"))
                    self.wfile.flush()
                    last_id = event_id
                    if kind in {"result", "error"}:
                        break
            except (BrokenPipeError, ConnectionResetError, socket.timeout):
                pass
            except Exception:
                self.wfile.write(b"event: error\ndata: The explanation stream failed.\n\n")
                self.wfile.flush()
            finally:
                close = getattr(events, "close", None)
                if close is not None:
                    close()

        def _route(self):
            self._check_origin()
            path, parts = self._path()
            method = self.command
            if method not in {"GET", "HEAD", "POST"}:
                raise _HTTPFailure(405, "Method not allowed.")
            if method == "POST":
                body = self._read_json()
                if parts == ["api", "papers"]:
                    source = body.get("input")
                    if not isinstance(source, str) or not source.strip():
                        raise _HTTPFailure(400, "A paper input is required.")
                    self._json(200, app.import_paper(source))
                    return
                if len(parts) == 4 and parts[:2] == ["api", "papers"] and parts[3] == "explain":
                    self._json(202, {"task": app.explain(parts[2], body)})
                    return
                if len(parts) == 6 and parts[:2] == ["api", "papers"] and parts[3] == "explanations" and parts[5] == "displayed":
                    self._json(200, app.acknowledge(parts[2], parts[4]))
                    return
                raise FileNotFoundError()
            if parts == ["api", "health"]:
                self._json(200, app.health())
                return
            if len(parts) == 4 and parts[:2] == ["api", "tasks"] and parts[3] == "events":
                if method == "HEAD":
                    raise _HTTPFailure(405, "Use GET for task events.")
                self._events(parts[2])
                return
            if len(parts) in {3, 4} and parts[:2] == ["api", "papers"]:
                paper = parts[2]
                resource = parts[3] if len(parts) == 4 else "metadata"
                if resource == "pdf":
                    self._file(app.pdf_path(paper), "application/pdf")
                    return
                actions = {"metadata": app.metadata, "content": app.content, "explanations": app.history}
                if resource in actions:
                    self._json(200, actions[resource](paper))
                    return
                raise FileNotFoundError()
            if parts[0] == "api":
                raise FileNotFoundError()
            self._static(path, parts)

        def _dispatch(self):
            try:
                self._route()
            except _HTTPFailure as error:
                self._error(error.status, error.message)
            except FileNotFoundError:
                self._error(404, "Not found.")
            except ValueError as error:
                self._error(400, str(error) or "Invalid request.")
            except RuntimeError as error:
                self._error(409, str(error) or "Request conflicts with the current state.")
            except socket.timeout:
                self._error(408, "Request timed out.")
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception:
                self._error(500, "The local service could not complete this request.")

        do_GET = do_HEAD = do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _dispatch

    return _LocalServer(("127.0.0.1", port), Handler)
