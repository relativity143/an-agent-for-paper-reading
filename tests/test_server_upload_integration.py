"""Offline PDF upload pipeline with real HTTP, Application, Store and persistence."""

import copy
import hashlib
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import quote

from server.http import create_server
from server.service import Application, core


def synthetic_pdf():
    """A self-contained one-page PDF; no parser or fixture server is required."""
    content = b"BT /F1 12 Tf 50 780 Td (Synthetic upload fixture.) Tj ET\n"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"endstream",
    ]
    document = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, value in enumerate(objects, 1):
        offsets.append(len(document))
        document.extend(f"{number} 0 obj\n".encode() + value + b"\nendobj\n")
    xref = len(document)
    document.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        document.extend(f"{offset:010d} 00000 n \n".encode())
    document.extend(f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return bytes(document)


def synthetic_overview():
    location = {"ids": [], "page": 1, "quote": "Synthetic upload fixture."}
    return {
        "type": "overview", "title": "Synthetic uploaded paper",
        "question": {"text": "What does this synthetic model predict?", "where": location},
        "model": {"summary": "A synthetic model", "items": []},
        "conclusions": {"text": "A synthetic result", "where": location},
        "foundations": [], "previously": [],
        "route": {"steps": [{"step": number, "name": f"Step {number}", "range": "PDF 第 1 页"}
                            for number in range(1, 4)], "skim": None},
        "next": {"text": "Read the first page", "where": location}, "highlights": [],
    }


class OfflineClient:
    def get(self, url):
        raise AssertionError("An upload must not fetch an external resource")


class FakeBackend:
    def __init__(self):
        self.prompts = []

    def generate(self, prompt, schema):
        self.prompts.append(prompt)
        return copy.deepcopy(synthetic_overview())


class UploadIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="paperread-upload-integration-")
        self.addCleanup(self.directory.cleanup)
        self.home = Path(self.directory.name).resolve()
        self.store = core.Store(self.home, client=OfflineClient())
        self.backend = FakeBackend()
        self.app = Application(self.store, self.backend)
        self.addCleanup(self.app.close)
        self.server = create_server(self.app, port=0, web_root=self.home / "no-web")
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.01},
                                       name="paperread-upload-integration-http", daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)
        self.pdf = synthetic_pdf()

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.app.close()
        self.assertFalse(self.thread.is_alive())
        self.assertTrue(all(not worker.is_alive() for worker in self.app.pool._threads))

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def json_request(self, method, path, body=None, expected=200):
        encoded = None if body is None else json.dumps(body).encode()
        status, headers, payload = self.request(method, path, encoded, {"Content-Type": "application/json"})
        self.assertEqual(status, expected, payload)
        self.assertIn("application/json", headers["Content-Type"])
        self.assertNotIn(str(self.home).encode(), payload)
        return json.loads(payload)

    def upload(self, name):
        status, headers, payload = self.request(
            "POST", "/api/papers/upload", self.pdf,
            {"Content-Type": "application/pdf", "X-Paper-Name": quote(name, safe="")},
        )
        self.assertEqual(status, 200, payload)
        self.assertIn("application/json", headers["Content-Type"])
        self.assertNotIn(b"ClientPrivate", payload)
        self.assertNotIn(str(self.home).encode(), payload)
        result = json.loads(payload)
        self.assertEqual(result["source"], "local_pdf")
        self.assertIsNone(result["source_url"])
        return result

    def test_upload_read_generate_display_and_duplicate_preserve_one_cache(self):
        metadata = self.upload("synthetic-paper.pdf")
        paper = metadata["paper"]
        resource = "/api/papers/" + quote(paper, safe="")
        cached = self.store.papers / paper
        self.assertEqual(metadata["title"], "synthetic-paper")
        self.assertEqual(self.json_request("GET", resource), metadata)
        self.assertEqual(self.json_request("GET", resource + "/content"), {"pdf": resource + "/pdf"})
        listing = self.json_request("GET", "/api/papers")["papers"]
        self.assertEqual([item["paper"] for item in listing], [paper])
        self.assertEqual(listing[0]["explanations"], 0)
        self.assertIsNone(listing[0]["last_read"])
        status, headers, downloaded = self.request("GET", resource + "/pdf")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "application/pdf")
        self.assertEqual(downloaded, self.pdf)
        self.assertEqual((cached / "paper.pdf").read_bytes(), self.pdf)
        saved_metadata = core.read_json(cached / "paper.json")
        self.assertIsNone(saved_metadata["source_url"])
        self.assertEqual(saved_metadata["source_sha256"], hashlib.sha256(self.pdf).hexdigest())

        with patch("server.service.extract_pdf", return_value=("[PDF 第 1 页]\nSynthetic upload fixture.", 1)) as extract:
            request = {"mode": "continue", "reading_context": "从基础讲起。",
                       "record_version": self.json_request("GET", resource + "/record")["version"]}
            task = self.json_request("POST", resource + "/explain", request, expected=202)["task"]
            status, headers, stream = self.request("GET", f"/api/tasks/{task}/events")
            self.assertEqual(status, 200)
            self.assertIn("text/event-stream", headers["Content-Type"])
            self.assertEqual(headers["Connection"], "close")
            frames = []
            for frame in stream.decode("utf-8").split("\n\n"):
                fields = dict(line.split(": ", 1) for line in frame.splitlines() if ": " in line)
                if "event" in fields:
                    frames.append(fields)
            self.assertTrue(frames, stream)
            self.assertEqual(frames[-1]["event"], "result", frames)
            self.assertNotIn("error", [frame["event"] for frame in frames])
            result = json.loads(frames[-1]["data"])
            self.assertTrue(self.app.wait_closed(2))
            extract.assert_called_once_with(cached / "paper.pdf")
        self.assertEqual(result["paper"], paper)
        self.assertEqual(result["type"], "overview")
        self.assertIsNone(result["asked"])
        self.assertEqual(len(self.backend.prompts), 1)
        self.assertNotIn("file:///", self.backend.prompts[0])
        self.assertNotIn(str(self.home), self.backend.prompts[0])
        self.assertEqual(self.json_request("GET", resource + "/explanations"), [result])
        listing = self.json_request("GET", "/api/papers")["papers"]
        self.assertEqual(listing[0]["explanations"], 1)
        self.assertIsNone(listing[0]["last_read"])
        displayed_at = "2026-11-15T04:05:06+00:00"
        with patch("server.service.core.now", return_value=displayed_at):
            receipt = self.json_request("POST", resource + f'/explanations/{result["id"]}/displayed', {})
        self.assertEqual(receipt, {"id": result["id"], "displayed": True})
        self.assertEqual(self.json_request("GET", "/api/papers")["papers"][0]["last_read"], displayed_at)
        self.assertEqual(len(self.store.log_show(paper, last=0)), 1)
        self.assertFalse((self.home / "profile.json").exists())

        before = {path.name: path.read_bytes() for path in cached.iterdir() if path.is_file()}
        repeated = self.upload("synthetic-paper.pdf")
        self.assertEqual(repeated["paper"], paper)
        self.assertEqual({path.name: path.read_bytes() for path in cached.iterdir() if path.is_file()}, before)
        self.assertEqual([path.name for path in self.store.papers.iterdir()], [paper])

    def test_unix_and_windows_upload_names_do_not_persist_client_paths(self):
        names = ("/Users/ClientPrivate/Research/上传论文.pdf", r"C:\Users\ClientPrivate\Research\上传论文.pdf")
        first = None
        for name in names:
            with self.subTest(name=name):
                metadata = self.upload(name)
                self.assertEqual(metadata["title"], "上传论文")
                if first is None:
                    first = metadata
                else:
                    self.assertEqual(metadata, first)
                saved = core.read_json(self.store.papers / metadata["paper"] / "paper.json")
                self.assertIsNone(saved["source_url"])
                listing = self.json_request("GET", "/api/papers")["papers"]
                self.assertEqual([item["paper"] for item in listing], [metadata["paper"]])
                self.assertNotIn("ClientPrivate", json.dumps(listing))
                for path in self.store.papers.rglob("*"):
                    self.assertNotIn("ClientPrivate", str(path.relative_to(self.store.papers)))
                    if path.is_file():
                        payload = path.read_bytes()
                        self.assertNotIn(b"ClientPrivate", payload)
                        self.assertNotIn(b"file:///", payload)
                        self.assertNotIn(b"paperread-upload-import-", payload)


if __name__ == "__main__":
    unittest.main()
