"""M1 state-machine tests. All reader data and model output are synthetic."""
import copy
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from server.service import Application, core, outline_steps

PAPER = "2601.00001v1"
LOCATION = {"ids": ["S1.p1"], "page": None, "quote": None}


def overview():
    return {"type": "overview", "title": "Synthetic paper", "question": {"text": "A question", "where": LOCATION},
            "model": {"summary": "A model", "items": []}, "conclusions": {"text": "Limited result", "where": LOCATION},
            "foundations": [{"name": "Only mentioned", "note": "Prerequisite"}], "previously": [],
            "route": {"steps": [{"step": i, "name": f"Step {i}", "range": "S1"} for i in range(1, 4)], "skim": "None"},
            "next": {"text": "Read here", "where": LOCATION}, "highlights": []}


def point(name="Necessary foundation", next_id="S1.p1"):
    return {"type": "point", "point": "Current point", "where": LOCATION, "intro": "Why this matters",
            "assumes": [{"concept": "Only assumed", "status": "new", "explained_at": None, "note": "Not yet taught"}],
            "concepts": [{"name": name, "in_this_paper": "Used here", "in_this_paper_where": ["S1.p1"],
                          "itself": "Picture then result", "sources": []}], "together": "How it fits",
            "back_to_text": "Basic knowledge remains; defer the derivation.", "unexpanded": ["Not taught"],
            "progress": {"step": 1, "steps_total": 3, "text": "Still explaining necessary basics",
                         "next": {"point": "Remaining basics", "where": {**LOCATION, "ids": [next_id]}}}, "highlights": []}


class FakeBackend:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.prompts = []

    def generate(self, prompt, schema):
        self.prompts.append(prompt)
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return copy.deepcopy(value)


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="paperread-server-test-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        self.store = core.Store(self.home)
        self.path = self.home / "papers" / PAPER
        data = {"title": "Synthetic paper", "authors": [], "source": "arxiv_html", "parser_version": core.PARSER_VERSION,
                "source_url": "https://arxiv.org/html/" + PAPER,
                "sections": [{"id": "S1", "title": "Setup", "level": 1, "order": 1, "end_order": 15}],
                "paragraphs": [{"id": "S1.p1", "section": "S1", "text": "Original text. ⟨S1.E1⟩", "order": 2,
                                "end_order": 5, "equations": ["S1.E1"], "sentences": ["Original text."]},
                               {"id": "S1.p2", "section": "S1", "text": "Next paragraph.", "order": 6,
                                "end_order": 7, "equations": [], "sentences": ["Next paragraph."]}],
                "equations": [{"id": "S1.E1", "latex": "x=1", "order": 3, "section": "S1", "paragraph": "S1.p1"}],
                "figures": [{"id": "S1.T1", "caption": "Table 1: Synthetic table", "order": 9, "section": "S1", "subcaptions": []}],
                "references": []}
        core.write_json(self.path / "paper.json", data)
        self.backend = FakeBackend(overview(), point(), point("Second foundation"))
        self.app = Application(self.store, self.backend)
        self.addCleanup(self.app.close)

    def run_turn(self, request=None):
        task_id = self.app.explain(PAPER, request or {"mode": "continue"})
        events = [event for event in self.app.task_events(task_id) if event is not None]
        # Wait until the generation worker releases its per-paper active slot.
        with self.app.get_task(task_id).condition:
            pass
        return events[-1]

    def display_overview(self):
        _, kind, result = self.run_turn()
        self.assertEqual(kind, "result")
        self.app.acknowledge(PAPER, result["id"])
        return result

    def test_content_order_equation_not_duplicated_and_table_retained(self):
        value = self.app.content(PAPER)
        self.assertEqual([b["id"] for b in value["blocks"]], ["S1", "S1.p1", "S1.p2", "S1.T1"])
        self.assertEqual(value["equations"], {"S1.E1": "x=1"})
        self.assertNotIn(str(self.home), json.dumps(value))

    def test_overview_then_point_waits_for_display_and_never_assumes_known(self):
        _, kind, result = self.run_turn()
        self.assertEqual(kind, "result")
        self.assertEqual(self.store.log_show(PAPER), [])
        self.assertFalse((self.home / "profile.json").exists())
        _, _, repeated = self.run_turn()
        self.assertEqual(result["id"], repeated["id"])
        self.assertEqual(len(self.backend.prompts), 1)
        self.app.acknowledge(PAPER, result["id"])
        self.assertFalse((self.home / "profile.json").exists())
        _, kind, explained = self.run_turn()
        self.assertEqual(kind, "result")
        self.app.acknowledge(PAPER, explained["id"])
        self.app.acknowledge(PAPER, explained["id"])
        self.assertEqual(len(self.store.log_show(PAPER, last=0)), 2)
        self.assertEqual(set(self.store.profile()["concepts"]), {"Necessary foundation"})
        self.assertEqual(self.store.profile()["concepts"]["Necessary foundation"]["status"], "explained")
        self.assertEqual(len(outline_steps(self.path / "outline.md")), 3)

    def test_first_question_persists_across_restart_and_continue(self):
        question = {"mode": "ask", "question": "Why this term?", "where": "S1.p1"}
        _, _, first = self.run_turn(question)
        self.app.acknowledge(PAPER, first["id"])
        other = Application(self.store, FakeBackend(point()))
        self.addCleanup(other.close)
        task = other.explain(PAPER, {"mode": "continue"})
        events = [e for e in other.task_events(task) if e]
        self.assertEqual(events[-1][1], "result")
        self.assertIn('"request": {"mode": "ask", "question": "Why this term?"', other.backend.prompts[0])
        self.assertIsNone(core.read_json(self.path / "agent-state.json")["pending"])

    def test_pending_foundations_keep_same_point_and_reject_jumping(self):
        self.display_overview()
        _, _, first = self.run_turn()
        self.app.acknowledge(PAPER, first["id"])
        bad = point()
        bad["where"] = {**LOCATION, "ids": ["S1.p2"]}
        self.backend.responses = [bad, point("Second foundation")]
        _, kind, second = self.run_turn()
        self.assertEqual(kind, "result")
        self.assertEqual(second["point"], first["point"])
        self.assertEqual(second["progress"]["step"], 1)
        self.assertEqual(second["where"]["ids"], ["S1.p1"])
        self.assertIn("Remaining basics", self.backend.prompts[-1])

    def test_missing_fields_retry_once_and_no_partial_state(self):
        self.display_overview()
        bad = point()
        del bad["assumes"]
        self.backend.responses = [bad, bad]
        _, kind, error = self.run_turn()
        self.assertEqual(kind, "error")
        self.assertEqual(len(self.app.history(PAPER)), 1)
        self.assertEqual(len(self.store.log_show(PAPER, last=0)), 1)
        self.assertIn("未保存", error)

    def test_known_and_unsure_remain_on_display(self):
        self.display_overview()
        for name, status in (("Necessary foundation", "known"), ("Second foundation", "unsure")):
            self.store.update_profile("mark", SimpleNamespace(concept=name, status=status, source="Reader explicit", paper=PAPER, where="S1.p1"))
        for _ in range(2):
            _, kind, result = self.run_turn()
            self.assertEqual(kind, "result")
            self.app.acknowledge(PAPER, result["id"])
        self.assertEqual(self.store.profile()["concepts"]["Necessary foundation"]["status"], "known")
        self.assertEqual(self.store.profile()["concepts"]["Second foundation"]["status"], "unsure")

    def test_crash_between_log_and_receipt_does_not_duplicate(self):
        first = self.display_overview()
        state = core.read_json(self.path / "agent-state.json")
        state["displayed"] = []
        core.write_json(self.path / "agent-state.json", state)
        self.app.acknowledge(PAPER, first["id"])
        self.assertEqual(len(self.store.log_show(PAPER, last=0)), 1)

    def test_private_profile_and_local_paths_not_in_prompt(self):
        core.write_json(self.home / "profile.json", {"background": "PRIVATE_IDENTITY", "areas": {"Quantum": {"level": "none"}},
                        "concepts": {"Vector": {"status": "known", "source": "PRIVATE_CONFIRMATION"}}})
        self.run_turn()
        prompt = self.backend.prompts[0]
        self.assertNotIn("PRIVATE_IDENTITY", prompt)
        self.assertNotIn("PRIVATE_CONFIRMATION", prompt)
        self.assertNotIn(str(self.home), prompt)
        self.assertIn('"Quantum": "none"', prompt)

    def test_invalid_requests_and_ids_rejected_before_model(self):
        for request in ({"mode": "unknown"}, {"mode": "ask"}, {"mode": "ask", "question": "Q", "where": "no-such-id"}):
            with self.subTest(request=request), self.assertRaises(ValueError):
                self.app.explain(PAPER, request)
        self.assertEqual(self.backend.prompts, [])
        with self.assertRaises(FileNotFoundError):
            self.app.acknowledge(PAPER, "unknown")

    def test_model_error_does_not_expose_sensitive_response(self):
        self.backend.responses = [RuntimeError("PRIVATE_MODEL_KEY")]
        _, kind, error = self.run_turn()
        self.assertEqual(kind, "error")
        self.assertNotIn("PRIVATE_MODEL_KEY", error)
        self.assertEqual(self.app.history(PAPER), [])

    def test_nonexistent_source_rejected(self):
        self.display_overview()
        bad = point()
        bad["concepts"][0]["sources"] = [{"title": "Made up", "url": "https://made-up.invalid/book"}]
        self.backend.responses = [bad, bad]
        self.assertEqual(self.run_turn()[1], "error")

    def test_model_cannot_invent_known_or_continue_past_end(self):
        self.display_overview()
        bad = point()
        bad["assumes"][0]["status"] = "known"
        self.backend.responses = [bad, bad]
        self.assertEqual(self.run_turn()[1], "error")
        last = point()
        last["progress"]["next"] = None
        self.backend.responses = [last]
        _, kind, result = self.run_turn()
        self.assertEqual(kind, "result")
        self.app.acknowledge(PAPER, result["id"])
        before = len(self.backend.prompts)
        self.assertEqual(self.run_turn()[1], "error")
        self.assertEqual(len(self.backend.prompts), before)

    def test_pdf_concept_reference_must_fit_document(self):
        result = point()
        result["concepts"][0]["in_this_paper_where"] = ["PDF 第 999 页"]
        with self.assertRaisesRegex(ValueError, "页码越界"):
            self.app._check_result(result, {"mode": "ask"}, {"explanations": []}, {}, 2)

    def test_latex_in_outline_name_survives_replacement(self):
        (self.path / "outline.md").write_text("## 主线步骤\nPending\n## Other\nKeep me\n", encoding="utf-8")
        value = overview()
        value["route"]["steps"][0]["name"] = r"$\rho$ state"
        self.app._save_outline(self.path, value)
        text = (self.path / "outline.md").read_text(encoding="utf-8")
        self.assertIn(r"$\rho$ state", text)
        self.assertIn("Keep me", text)

    def test_duplicate_task_is_rejected_and_workers_finish(self):
        entered, release = threading.Event(), threading.Event()
        original = self.backend.generate
        def blocking(prompt, schema):
            entered.set()
            release.wait(3)
            return original(prompt, schema)
        self.backend.generate = blocking
        task_id = self.app.explain(PAPER, {"mode": "continue"})
        self.assertTrue(entered.wait(2))
        try:
            with self.assertRaises(RuntimeError):
                self.app.explain(PAPER, {"mode": "continue"})
        finally:
            release.set()
        events = [event for event in self.app.task_events(task_id) if event]
        replay = list(self.app.task_events(task_id, after=events[-2][0]))
        self.assertEqual(replay[-1], events[-1])

    def test_pdf_content_and_unreadable_pdf_fail_without_generated_explanation(self):
        data = core.read_json(self.path / "paper.json")
        data["source"] = "pdf_only"
        core.write_json(self.path / "paper.json", data)
        (self.path / "paper.pdf").write_bytes(b"%PDF- synthetic test only")
        self.assertEqual(self.app.content(PAPER), {"pdf": f"/api/papers/{PAPER}/pdf"})
        self.assertEqual(self.app.pdf_path(PAPER), self.path / "paper.pdf")
        with patch("server.service.extract_pdf", side_effect=ValueError("PDF unreadable")):
            self.assertEqual(self.run_turn()[1], "error")
        self.assertEqual(self.backend.prompts, [])
        with self.assertRaises(ValueError):
            self.app.explain(PAPER, {"mode": "ask", "quote": "text", "page": True})


if __name__ == "__main__":
    unittest.main()
