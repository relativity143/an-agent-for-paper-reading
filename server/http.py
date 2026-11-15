"""Small same-origin HTTP boundary for the local paper-reading application."""
from __future__ import annotations

import json
import mimetypes
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import re
import shutil
import socket
import stat
import tempfile
from urllib.parse import unquote, urlsplit

MAX_BODY_BYTES = 1024 * 1024
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
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
            phrase = {400: "请求格式不正确，请刷新页面后重试。", 404: "没有找到请求的内容。",
                      408: "接收请求超时，请重试。", 414: "请求地址过长。",
                      431: "请求头过大，请缩短文件名后重试。",
                      501: "不支持这种请求方式，请刷新页面重试。",
                      505: "不支持此 HTTP 版本，请使用现代浏览器。"}.get(code, "请求未完成，请刷新页面重试。")
            self._error(code, phrase)

        def _single_header(self, name):
            values = self.headers.get_all(name, [])
            if len(values) > 1:
                raise _HTTPFailure(400, "请求头重复，请刷新页面后重试。")
            return values[0].strip() if values else None

        def _check_origin(self):
            host = self._single_header("Host")
            bound_port = self.server.server_port
            allowed = {f"127.0.0.1:{bound_port}", f"localhost:{bound_port}"}
            if bound_port == 80:
                allowed.update({"127.0.0.1", "localhost"})
            if host not in allowed:
                raise _HTTPFailure(403, "请从本机服务显示的地址打开页面。")
            origin = self._single_header("Origin")
            if origin is not None and origin != f"http://{host}":
                raise _HTTPFailure(403, "只接受来自本机阅读页面的同源请求。")
            fetch_site = self._single_header("Sec-Fetch-Site")
            if fetch_site is not None and fetch_site not in {"same-origin", "none"}:
                raise _HTTPFailure(403, "只接受来自本机阅读页面的同源请求。")
            referer = self._single_header("Referer")
            if referer:
                parsed = urlsplit(referer)
                if parsed.scheme != "http" or parsed.netloc != host:
                    raise _HTTPFailure(403, "只接受来自本机阅读页面的同源请求。")

        def _body_length(self, *, limit=MAX_BODY_BYTES, content_type="application/json", type_status=415):
            if self._single_header("Transfer-Encoding") is not None:
                raise _HTTPFailure(400, "请提供请求长度，不支持 Transfer-Encoding。")
            length = self._single_header("Content-Length")
            if length is None:
                raise _HTTPFailure(411, "缺少 Content-Length 请求长度。")
            if not length.isascii() or not length.isdigit() or len(length) > 12:
                raise _HTTPFailure(400, "Content-Length 请求长度不合法。")
            size = int(length)
            if size > limit:
                raise _HTTPFailure(413, "文件超过 50 MiB 上限，请换用较小的 PDF。" if limit == MAX_UPLOAD_BYTES
                                   else "请求超过 1 MiB 上限，请缩短输入。")
            received_type = self._single_header("Content-Type") or ""
            if received_type.split(";", 1)[0].strip().lower() != content_type:
                raise _HTTPFailure(type_status, "请上传 PDF 文件（application/pdf）。" if content_type == "application/pdf"
                                   else "请使用 application/json 发送请求。")
            return size

        def _upload_headers(self):
            length = self._body_length(limit=MAX_UPLOAD_BYTES, content_type="application/pdf", type_status=400)
            if length < 5:
                raise _HTTPFailure(400, "上传内容不是 PDF，请选择有效的 PDF 文件。")
            filename = self._single_header("X-Paper-Name")
            if filename is not None:
                try:
                    if re.search(r"%(?![0-9A-Fa-f]{2})", filename):
                        raise ValueError()
                    filename = unquote(filename, encoding="utf-8", errors="strict")
                except (UnicodeError, ValueError):
                    raise _HTTPFailure(400, "PDF 文件名格式不正确，请重命名后重试。") from None
                if any(ord(character) < 32 or ord(character) == 127 for character in filename):
                    raise _HTTPFailure(400, "PDF 文件名格式不正确，请重命名后重试。")
                # Browsers may include Unix or Windows paths. Only retain the name.
                filename = filename.replace("\\", "/").rsplit("/", 1)[-1].strip()
                if filename in {"", ".", ".."}:
                    raise _HTTPFailure(400, "PDF 文件名格式不正确，请重命名后重试。")
            return length, filename

        def handle_expect_100(self):
            try:
                self._check_origin()
                _, parts = self._path()
                if self.command == "POST" and parts == ["api", "papers", "upload"]:
                    self._upload_headers()
                else:
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
                raise _HTTPFailure(400, "请求内容未传输完整，请重新提交。")
            try:
                value = json.loads(data.decode("utf-8"), parse_constant=_reject_json_constant)
            except (UnicodeDecodeError, ValueError, RecursionError):
                raise _HTTPFailure(400, "JSON 请求格式不正确。") from None
            if not isinstance(value, dict):
                raise _HTTPFailure(400, "JSON 请求必须是对象。")
            return value

        def _upload_pdf(self):
            length, filename = self._upload_headers()
            with tempfile.TemporaryDirectory(prefix="paperread-upload-") as temporary:
                source = Path(temporary) / "source.pdf"
                with source.open("wb") as target:
                    header = self.rfile.read(5)
                    if len(header) != 5:
                        raise _HTTPFailure(400, "请求内容未传输完整，请重新提交。")
                    if header != b"%PDF-":
                        raise _HTTPFailure(400, "上传内容不是 PDF，请选择有效的 PDF 文件。")
                    target.write(header)
                    remaining = length - len(header)
                    while remaining:
                        chunk = self.rfile.read(min(64 * 1024, remaining))
                        if not chunk:
                            raise _HTTPFailure(400, "请求内容未传输完整，请重新提交。")
                        target.write(chunk)
                        remaining -= len(chunk)
                metadata = app.import_uploaded_pdf(source, filename)
            self._json(200, metadata)

        def _path(self):
            if not self.path.startswith("/") or self.path.startswith("//"):
                raise _HTTPFailure(400, "请求地址格式不正确。")
            try:
                path = unquote(urlsplit(self.path).path, errors="strict")
            except (UnicodeError, ValueError):
                raise _HTTPFailure(400, "请求地址格式不正确。") from None
            parts = path.split("/")[1:]
            if any(part in {".", ".."} or "\\" in part or
                   any(ord(character) < 32 or ord(character) == 127 for character in part)
                   for part in parts):
                raise _HTTPFailure(404, "没有找到请求的内容。")
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
                self._bytes(200, "论文阅读服务已启动，但页面文件尚未安装。请检查仓库的 web 目录。\n".encode("utf-8"),
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
                raise _HTTPFailure(400, "讲解事件游标不合法，请刷新页面恢复记录。")
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
                self.wfile.write("event: error\ndata: 讲解连接中断，请刷新页面恢复已保存的记录。\n\n".encode("utf-8"))
                self.wfile.flush()
            finally:
                close = getattr(events, "close", None)
                if close is not None:
                    close()

        def _route(self):
            self._check_origin()
            path, parts = self._path()
            method = self.command
            if method not in {"GET", "HEAD", "POST", "PUT"}:
                raise _HTTPFailure(405, "不支持这种请求方式。")
            if method == "PUT":
                if parts == ["api", "settings"]:
                    self._json(200, app.update_settings(self._read_json()))
                    return
                if len(parts) == 4 and parts[:2] == ["api", "papers"] and parts[3] == "record":
                    self._json(200, app.update_record(parts[2], self._read_json()))
                    return
                if len(parts) == 4 and parts[:2] == ["api", "papers"] and parts[3] == "map":
                    self._json(200, app.update_map_layout(parts[2], self._read_json()))
                    return
                raise _HTTPFailure(405, "不支持这种请求方式。")
            if method == "POST":
                if parts == ["api", "papers", "upload"]:
                    self._upload_pdf()
                    return
                body = self._read_json()
                if len(parts) == 4 and parts[:2] == ["api", "tasks"] and parts[3] == "cancel":
                    if body:
                        raise _HTTPFailure(400, "停止请求只接受空对象 {}。")
                    self._json(200, app.cancel_task(parts[2]))
                    return
                if parts == ["api", "papers"]:
                    source = body.get("input")
                    if not isinstance(source, str) or not source.strip():
                        raise _HTTPFailure(400, "请输入论文编号、链接或本地 PDF 路径。")
                    self._json(200, app.import_paper(source))
                    return
                if len(parts) == 4 and parts[:2] == ["api", "papers"] and parts[3] == "explain":
                    self._json(202, {"task": app.explain(parts[2], body)})
                    return
                if len(parts) == 4 and parts[:2] == ["api", "papers"] and parts[3] == "prerequisites":
                    if body:
                        raise _HTTPFailure(400, "基础清单请求只接受空对象 {}。")
                    self._json(202, {"task": app.prepare_prerequisites(parts[2])})
                    return
                if len(parts) == 4 and parts[:2] == ["api", "papers"] and parts[3] == "feedback":
                    self._json(200, app.feedback(parts[2], body))
                    return
                if len(parts) == 6 and parts[:2] == ["api", "papers"] and parts[3] == "explanations" and parts[5] == "displayed":
                    self._json(200, app.acknowledge(parts[2], parts[4]))
                    return
                raise FileNotFoundError()
            if parts == ["api", "settings"]:
                self._json(200, app.settings())
                return
            if parts == ["api", "papers"]:
                self._json(200, app.list_papers())
                return
            if parts == ["api", "health"]:
                self._json(200, app.health())
                return
            if len(parts) == 4 and parts[:2] == ["api", "tasks"] and parts[3] == "events":
                if method == "HEAD":
                    raise _HTTPFailure(405, "讲解事件流需要使用 GET 请求。")
                self._events(parts[2])
                return
            if len(parts) in {3, 4} and parts[:2] == ["api", "papers"]:
                paper = parts[2]
                resource = parts[3] if len(parts) == 4 else "metadata"
                if resource == "pdf":
                    self._file(app.pdf_path(paper), "application/pdf")
                    return
                if resource == "record":
                    self._json(200, app.record(paper))
                    return
                if resource == "map":
                    self._json(200, app.map_layout(paper))
                    return
                if resource == "prerequisites":
                    self._json(200, app.prerequisites(paper))
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
                self._error(404, "没有找到请求的内容。")
            except ValueError as error:
                self._error(400, str(error) or "请求内容不合法。")
            except RuntimeError as error:
                self._error(409, str(error) or "当前状态无法执行此请求，请稍后重试。")
            except socket.timeout:
                self._error(408, "接收请求超时，请重新提交。")
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception:
                self._error(500, "本机服务未能完成请求，请检查本机配置后重试。")

        do_GET = do_HEAD = do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _dispatch

    return _LocalServer(("127.0.0.1", port), Handler)
