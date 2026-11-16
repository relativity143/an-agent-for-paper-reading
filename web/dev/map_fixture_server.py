"""Isolated real HTTP/Store fixture for test_map_persistence.mjs.

The caller owns --home and its cleanup. stdin 'stop' closes the server; reuse
the same directory and port to check persistence across a complete restart.
No model or external network request is allowed.
"""
import argparse
import json
import os
from pathlib import Path
import sys
import threading

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
PAPERS = ("2601.00001v1", "2601.00002v1")


class Offline:
    def __init__(self):
        self.network_calls = 0
        self.model_calls = 0

    def get(self, url):
        self.network_calls += 1
        raise AssertionError("Map tests cannot use external services")

    def generate(self, prompt, schema):
        self.model_calls += 1
        raise AssertionError("Map generation cannot call a model")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", required=True)
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()
    os.environ["PAPERREAD_HOME"] = args.home
    from server.http import create_server
    from server.service import Application, core

    for index, paper in enumerate(PAPERS, 1):
        folder = Path(args.home) / "papers" / paper
        if (folder / "paper.json").exists():
            continue
        core.write_json(folder / "paper.json", {
            "arxiv_id": paper[:-2], "version": "v1", "title": f"Map fixture {index}",
            "authors": ["Synthetic Author"], "source": "arxiv_html",
            "source_url": "https://arxiv.org/html/" + paper, "parser_version": core.PARSER_VERSION,
            "sections": [{"id": "S1", "title": "Toy model", "level": 1, "order": 1, "end_order": 5}],
            "paragraphs": [{"id": "S1.p1", "section": "S1", "text": "A synthetic model uses x=1.",
                            "order": 2, "end_order": 3, "equations": [],
                            "sentences": ["A synthetic model uses x=1."]}],
            "equations": [], "figures": [], "references": [],
        })
        core.write_json(folder / "agent-state.json", {
            "explanations": [], "displayed": [], "pending": None,
            "concept_record": {"version": 7, "concepts": [
                {"name": name, "status": status, "explained_in": None,
                 "where": "S1.p1", "note": "Original record", "updated": "2026-01-01T00:00:00Z"}
                for name, status in (("Alpha", "known"), ("Beta", "unsure"))]},
        })
    offline = Offline()
    app = Application(core.Store(client=offline), offline,
                      backend_info={"backend": "fixture", "backends": []})
    server = create_server(app, port=args.port)
    thread = threading.Thread(target=server.serve_forever,
                              kwargs={"poll_interval": 0.02}, name="map-fixture", daemon=True)
    thread.start()
    print(json.dumps({"ready": True, "port": server.server_port, "pid": os.getpid()}), flush=True)
    try:
        for line in sys.stdin:
            if line.strip() == "stop":
                break
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        app.close()
        assert not thread.is_alive(), "Fixture server failed to stop"
        print(json.dumps({"stopped": True, "model_calls": offline.model_calls,
                          "network_calls": offline.network_calls, "tasks": len(app.tasks)}), flush=True)


if __name__ == "__main__":
    main()
