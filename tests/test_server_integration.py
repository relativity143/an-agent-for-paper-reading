"""Offline M1 integration: real Store, Application, HTTP, and persisted state."""
import copy
import http.client
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from server.http import create_server
from server.service import Application, core

PAPER = "2601.00001v1"
LOCATION = {"ids": ["S1.p1"], "page": None, "quote": None}


def overview():
    return {
        "type": "overview", "title": "Integration fixture",
        "question": {"text": "What does the toy model predict?", "where": LOCATION},
        "model": {"summary": "A synthetic toy model", "items": []},
        "conclusions": {"text": "Only a synthetic result", "where": LOCATION},
        "foundations": [{"name": "Mentioned prerequisite", "note": "Not yet explained"}],
        "previously": [],
        "route": {"steps": [{"step": number, "name": f"Step {number}", "range": "S1"}
                            for number in range(1, 4)], "skim": "None"},
        "next": {"text": "Start with the first paragraph", "where": LOCATION},
        "highlights": [],
    }


def point(concept):
    return {
        "type": "point", "point": "The first paragraph", "where": LOCATION,
        "intro": "Understand the model's ingredients before using its formula.",
        "assumes": [{"concept": "Only assumed", "status": "new", "explained_at": None,
                     "note": "Still needs explanation"}],
        "concepts": [{"name": concept, "in_this_paper": "An ingredient of the toy model",
                      "in_this_paper_where": ["S1.p1"], "itself": "A simple picture and its consequence",
                      "sources": []}],
        "together": "The ingredients define the toy model.",
        "back_to_text": "Necessary basics remain; defer the formula's derivation.",
        "unexpanded": ["Remaining prerequisite"],
        "progress": {"step": 1, "steps_total": 3, "text": "Still explaining the first paragraph",
                     "next": {"point": "Remaining basics at this point", "where": LOCATION}},
        "highlights": [],
    }


class OfflineClient:
    def __init__(self):
        self.attempts = []

    def get(self, url):
        self.attempts.append(url)
        raise AssertionError("The integration fixture must never access external services")


class FakeBackend:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.prompts = []

    def generate(self, prompt, schema):
        self.prompts.append(prompt)
        if not self.responses:
            raise AssertionError("Unexpected extra model request")
        return copy.deepcopy(self.responses.pop(0))


class ServerIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="paperread-http-integration-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.environment = patch.dict(os.environ, {"PAPERREAD_HOME": str(self.home)})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.resources = []
        self.addCleanup(self.stop_all)
        self.client = OfflineClient()
        self.paper_dir = self.home / "papers" / PAPER
        core.write_json(self.paper_dir / "paper.json", {
            "arxiv_id": "2601.00001", "version": "v1", "title": "Integration fixture",
            "authors": ["Synthetic Author"], "source": "arxiv_html",
            "source_url": "https://arxiv.org/html/" + PAPER, "parser_version": core.PARSER_VERSION,
            "sections": [{"id": "S1", "title": "Toy model", "level": 1, "order": 1, "end_order": 5}],
            "paragraphs": [{"id": "S1.p1", "section": "S1", "text": "A synthetic model uses x=1.",
                            "order": 2, "end_order": 3, "equations": [],
                            "sentences": ["A synthetic model uses x=1."]}],
            "equations": [], "figures": [], "references": [],
        })
        self.backend = FakeBackend(overview(), point("First foundation"), point("Second foundation"))
        self.start(self.backend)

    def start(self, backend):
        store = core.Store(client=self.client)
        self.assertEqual(store.home, self.home.resolve())
        app = Application(store, backend)
        server = create_server(app, port=0, web_root=self.home / "no-web")
        thread = threading.Thread(target=server.serve_forever,
                                  kwargs={"poll_interval": 0.01},
                                  name="paperread-http-integration", daemon=True)
        resource = (server, thread, app)
        self.resources.append(resource)
        thread.start()
        self.server, self.thread, self.app, self.store = server, thread, app, store
        return resource

    def stop(self, resource):
        if resource not in self.resources:
            return
        server, thread, app = resource
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        app.close()
        self.resources.remove(resource)
        self.assertFalse(thread.is_alive())
        self.assertTrue(all(not worker.is_alive() for worker in app.pool._threads))

    def stop_all(self):
        for resource in self.resources[:]:
            self.stop(resource)

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        try:
            request_headers = dict(headers or {})
            if body is not None:
                request_headers["Content-Type"] = "application/json"
                body = json.dumps(body).encode("utf-8")
            connection.request(method, path, body=body, headers=request_headers)
            response = connection.getresponse()
            data = response.read()  # A missing SSE terminator/connection close fails within 3 seconds.
            self.assertTrue(response.isclosed())
            return response.status, dict(response.getheaders()), data
        finally:
            connection.close()

    def json_request(self, method, path, body=None, expected=200):
        status, headers, data = self.request(method, path, body)
        self.assertEqual(status, expected, data.decode("utf-8", errors="replace"))
        self.assertIn("application/json", headers["Content-Type"])
        self.assertNotIn(str(self.home).encode(), data)
        return json.loads(data)

    def turn(self):
        task = self.json_request("POST", f"/api/papers/{PAPER}/explain", {"mode": "continue"}, expected=202)["task"]
        status, headers, data = self.request("GET", f"/api/tasks/{task}/events")
        self.assertEqual(status, 200)
        self.assertIn("text/event-stream", headers["Content-Type"])
        self.assertEqual(headers["Connection"], "close")
        events = []
        for frame in data.decode("utf-8").split("\n\n"):
            fields = {}
            for line in frame.splitlines():
                if ": " in line and not line.startswith(":"):
                    key, value = line.split(": ", 1)
                    fields.setdefault(key, []).append(value)
            if "event" in fields:
                events.append((int(fields["id"][0]), fields["event"][0], "\n".join(fields.get("data", []))))
        self.assertTrue(events, data)
        self.assertEqual(events[-1][1], "result", events)
        self.assertEqual(sum(kind == "result" for _, kind, _ in events), 1)
        self.assertFalse(any(kind == "error" for _, kind, _ in events))
        self.assertEqual([identifier for identifier, _, _ in events], sorted({identifier for identifier, _, _ in events}))
        # A completed-task reconnect after the terminal ID must immediately close without re-emitting it.
        status, _, replay = self.request("GET", f"/api/tasks/{task}/events",
                                         headers={"Last-Event-ID": str(events[-1][0])})
        self.assertEqual((status, replay), (200, b""))
        return json.loads(events[-1][2])

    def acknowledge(self, result):
        response = self.json_request("POST", f'/api/papers/{PAPER}/explanations/{result["id"]}/displayed', {})
        self.assertEqual(response, {"id": result["id"], "displayed": True})

    def history(self):
        return self.json_request("GET", f"/api/papers/{PAPER}/explanations")

    def assert_no_known(self):
        concepts = self.store.profile()["concepts"]
        self.assertFalse(any(item["status"] == "known" for item in concepts.values()))
        self.assertNotIn("Only assumed", concepts)
        self.assertNotIn("Mentioned prerequisite", concepts)
        self.assertNotIn("Remaining prerequisite", concepts)

    def test_cached_paper_reading_display_and_restart_round_trip(self):
        metadata = self.json_request("POST", "/api/papers", {"input": PAPER})
        self.assertEqual(metadata["paper"], PAPER)
        self.assertEqual(metadata["source"], "arxiv_html")
        self.assertIsNone(metadata["steps"])
        content = self.json_request("GET", f"/api/papers/{PAPER}/content")
        self.assertEqual([block["id"] for block in content["blocks"]], ["S1", "S1.p1"])

        first = self.turn()
        self.assertEqual(first["type"], "overview")
        self.assertEqual(self.store.log_show(PAPER, last=0), [])
        self.assertEqual(self.store.profile()["concepts"], {})
        self.assertEqual(self.history(), [first])
        # Continue before display recovers the same result and never consumes another model output.
        self.assertEqual(self.turn(), first)
        self.assertEqual(len(self.backend.prompts), 1)
        self.assert_no_known()
        self.acknowledge(first)
        self.assertEqual(len(self.store.log_show(PAPER, last=0)), 1)
        self.assertEqual(self.store.profile()["concepts"], {})

        second = self.turn()
        self.assertEqual(second["type"], "point")
        self.assertEqual(self.store.profile()["concepts"], {})
        self.acknowledge(second)
        self.acknowledge(second)
        self.assertEqual(len(self.store.log_show(PAPER, last=0)), 2)
        self.assertEqual(self.store.profile()["concepts"]["First foundation"]["status"], "explained")
        self.assert_no_known()

        third = self.turn()
        self.assertEqual(third["point"], second["point"])
        self.assertEqual(third["where"], second["where"])
        self.assertEqual((third["progress"]["step"], second["progress"]["step"]), (1, 1))
        self.assertIn("Remaining basics at this point", self.backend.prompts[-1])
        self.assertEqual(set(self.store.profile()["concepts"]), {"First foundation"})
        self.assert_no_known()
        self.acknowledge(third)
        self.assertEqual({item["status"] for item in self.store.profile()["concepts"].values()}, {"explained"})
        expected_history = [first, second, third]
        self.assertEqual(self.history(), expected_history)
        expected_profile = self.store.profile()
        expected_logs = self.store.log_show(PAPER, last=0)
        self.assertEqual(len(expected_logs), 3)
        self.assertEqual(len(self.backend.prompts), 3)
        self.assertEqual(self.backend.responses, [])

        self.stop(self.resources[0])
        restored_backend = FakeBackend()
        self.start(restored_backend)
        self.assertEqual(self.history(), expected_history)
        self.assertEqual(self.store.profile(), expected_profile)
        self.assertEqual(self.store.log_show(PAPER, last=0), expected_logs)
        metadata = self.json_request("GET", f"/api/papers/{PAPER}")
        self.assertEqual(len(metadata["steps"]), 3)
        self.acknowledge(third)  # Receipts remain idempotent after a process restart.
        self.assertEqual(self.store.log_show(PAPER, last=0), expected_logs)
        self.assertEqual(restored_backend.prompts, [])
        self.assertEqual(self.client.attempts, [])
        self.assert_no_known()


if __name__ == "__main__":
    unittest.main()
