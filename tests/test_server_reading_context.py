"""Fresh reader input must replace historic knowledge assumptions, without erasing history."""
import copy
import json
import unittest
from unittest.mock import patch

import test_server_service as fixtures
import test_server_integration as http_fixtures
from server.service import Application, core


class ReadingContextTests(unittest.TestCase):
    def setUp(self):
        fixtures.ServiceTests.setUp(self)

    def turn(self, context, request=None, app=None):
        app = app or self.app
        task = app.explain(fixtures.PAPER, {**(request or {"mode": "continue"}),
                                           "reading_context": context})
        events = [e for e in app.task_events(task) if e]
        self.assertTrue(app.wait_closed(2))
        return events[-1]

    def material(self, prompt):
        return json.loads(prompt.split("本轮材料（JSON 数据）：\n", 1)[1])

    def test_missing_or_invalid_background_stops_before_model_or_storage(self):
        requests = [{"mode": "continue"}] + [
            {"mode": "continue", "reading_context": value}
            for value in (None, "", " \n ", True, {}, [], "x" * 2001)
        ]
        for request in requests:
            with self.subTest(request=request), self.assertRaisesRegex(ValueError, "本次阅读"):
                self.app.explain(fixtures.PAPER, request)
        self.assertEqual(self.backend.prompts, [])
        self.assertEqual(self.app.tasks, {})
        self.assertFalse((self.path / "agent-state.json").exists())

    def test_generation_does_not_read_old_profile_or_legacy_logs(self):
        core.write_json(self.home / "profile.json", {"background": "OLD_BACKGROUND",
                        "areas": {"OLD_AREA": {"level": "studied"}},
                        "concepts": {"OLD_CONCEPT": {"status": "known"}}})
        before = (self.home / "profile.json").read_bytes()
        with patch.object(self.store, "profile", side_effect=AssertionError("Do not load old knowledge")), \
                patch.object(self.store, "log_show", side_effect=AssertionError("Do not load legacy logs")):
            _, kind, _ = self.turn("  这次我想从基本术语讲起。  ")
        self.assertEqual(kind, "result")
        material = self.material(self.backend.prompts[0])
        self.assertEqual(material["reading_context"], "这次我想从基本术语讲起。")
        self.assertNotIn("learning_state", material)
        self.assertNotIn("legacy_reading_records", material)
        self.assertNotIn("OLD_", self.backend.prompts[0])
        self.assertEqual((self.home / "profile.json").read_bytes(), before)

    def test_pending_question_uses_new_background_after_restart_without_saving_it(self):
        question = {"mode": "ask", "question": "Why this term?", "where": "S1.p1"}
        _, kind, first = self.turn("FIRST_PRIVATE_CONTEXT", question)
        self.assertEqual(kind, "result")
        self.app.acknowledge(fixtures.PAPER, first["id"])
        state = core.read_json(self.path / "agent-state.json")
        self.assertEqual(state["pending"], question)
        other = Application(self.store, fixtures.FakeBackend(fixtures.point()))
        self.addCleanup(other.close)
        with self.assertRaisesRegex(ValueError, "本次阅读"):
            other.explain(fixtures.PAPER, {"mode": "continue"})
        _, kind, result = self.turn("SECOND_PRIVATE_CONTEXT", app=other)
        self.assertEqual(kind, "result")
        material = self.material(other.backend.prompts[0])
        self.assertEqual(material["reading_context"], "SECOND_PRIVATE_CONTEXT")
        self.assertEqual(material["request"], question)
        self.assertNotIn("FIRST_PRIVATE_CONTEXT", other.backend.prompts[0])
        self.assertNotIn("reading_context", result["asked"])
        self.assertNotIn("PRIVATE_CONTEXT", (self.path / "agent-state.json").read_text())

    def test_old_explanation_prose_is_excluded_but_reading_position_is_kept(self):
        _, _, first = self.turn("Current background")
        self.app.acknowledge(fixtures.PAPER, first["id"])
        state = core.read_json(self.path / "agent-state.json")
        state["explanations"][0]["foundations"][0]["note"] = "OLD_ASSUMPTION_SENTINEL"
        state["explanations"][0]["previously"] = [{"name": "OLD_PROFILE_SENTINEL"}]
        core.write_json(self.path / "agent-state.json", state)
        _, kind, _ = self.turn("I have not studied this field")
        self.assertEqual(kind, "result")
        prompt = self.backend.prompts[-1]
        self.assertNotIn("OLD_ASSUMPTION_SENTINEL", prompt)
        self.assertNotIn("OLD_PROFILE_SENTINEL", prompt)
        progress = self.material(prompt)["reading_progress"]
        self.assertEqual(progress[-1]["next"]["where"], fixtures.LOCATION)
        # Existing history remains available to the reader; only model input is restricted.
        self.assertEqual(self.app.history(fixtures.PAPER)[0]["foundations"][0]["note"], "OLD_ASSUMPTION_SENTINEL")

    def test_old_known_status_cannot_be_reused_in_new_output(self):
        _, _, first = self.turn("Start from basics")
        self.app.acknowledge(fixtures.PAPER, first["id"])
        core.write_json(self.home / "profile.json", {"background": "", "areas": {}, "concepts": {
            "Only assumed": {"status": "known", "paper": fixtures.PAPER, "where": "S1.p1"}}})
        bad = fixtures.point()
        bad["assumes"][0]["status"] = "known"
        self.backend.responses = [copy.deepcopy(bad), copy.deepcopy(bad)]
        _, kind, _ = self.turn("Start from basics")
        self.assertEqual(kind, "error")
        self.assertEqual(len(self.app.history(fixtures.PAPER)), 1)

    def test_only_current_page_explanations_are_reused_for_continuity(self):
        session = "a" * 32
        request = {"mode": "continue", "reading_session": session}
        _, _, first = self.turn("Current background", request)
        self.app.acknowledge(fixtures.PAPER, first["id"])
        _, kind, second = self.turn("Current background", request)
        self.assertEqual(kind, "result")
        material = self.material(self.backend.prompts[-1])
        self.assertEqual([item["id"] for item in material["current_reading_explanations"]], [first["id"]])
        self.app.acknowledge(fixtures.PAPER, second["id"])
        # Opening another page starts fresh even when the background text is identical.
        _, kind, _ = self.turn("Current background", {"mode": "continue", "reading_session": "b" * 32})
        self.assertEqual(kind, "result")
        material = self.material(self.backend.prompts[-1])
        self.assertEqual(material["current_reading_explanations"], [])
        self.assertEqual(len(material["reading_progress"]), 2)
        self.assertNotIn(session, (self.path / "agent-state.json").read_text())

    def test_changed_background_or_server_restart_discards_session_assumptions(self):
        request = {"mode": "continue", "reading_session": "c" * 32}
        _, _, first = self.turn("First background", request)
        self.app.acknowledge(fixtures.PAPER, first["id"])
        _, _, second = self.turn("Changed background", request)
        self.assertEqual(self.material(self.backend.prompts[-1])["current_reading_explanations"], [])
        self.app.acknowledge(fixtures.PAPER, second["id"])
        _, _, third = self.turn("First background", request)
        self.assertEqual(self.material(self.backend.prompts[-1])["current_reading_explanations"], [])
        self.app.acknowledge(fixtures.PAPER, third["id"])
        other = Application(self.store, fixtures.FakeBackend(fixtures.point()))
        self.addCleanup(other.close)
        _, kind, _ = self.turn("First background", request, app=other)
        self.assertEqual(kind, "result")
        self.assertEqual(self.material(other.backend.prompts[-1])["current_reading_explanations"], [])

    def test_invalid_session_is_rejected_without_starting_model(self):
        for session in (42, [], "", "x" * 81, "../../../private/path"):
            with self.subTest(session=session), self.assertRaises(ValueError):
                self.turn("Background", {"mode": "continue", "reading_session": session})
        self.assertEqual(self.backend.prompts, [])

    def test_old_cross_paper_records_are_rejected_even_if_they_exist(self):
        core.write_json(self.home / "profile.json", {"background": "", "areas": {}, "concepts": {
            "Vector": {"status": "known", "paper": "old-paper"}}})
        bad = fixtures.overview()
        bad["previously"] = [{"name": "Vector", "status": "known", "paper": "old-paper"}]
        self.backend.responses = [copy.deepcopy(bad), copy.deepcopy(bad)]
        _, kind, _ = self.turn("Start from basics")
        self.assertEqual(kind, "error")
        self.assertEqual(self.app.history(fixtures.PAPER), [])


class ReadingContextHTTPTests(unittest.TestCase):
    setUp = http_fixtures.ServerIntegrationTests.setUp
    start = http_fixtures.ServerIntegrationTests.start
    stop = http_fixtures.ServerIntegrationTests.stop
    stop_all = http_fixtures.ServerIntegrationTests.stop_all
    request = http_fixtures.ServerIntegrationTests.request

    def test_http_cannot_bypass_fresh_background_question(self):
        for body in ({"mode": "continue"}, {"mode": "ask", "question": "Explain this"}):
            status, _, data = self.request("POST", f"/api/papers/{http_fixtures.PAPER}/explain", body)
            self.assertEqual(status, 400)
            self.assertIn("本次阅读", json.loads(data)["error"])
        self.assertEqual(self.backend.prompts, [])
        self.assertFalse((self.paper_dir / "agent-state.json").exists())


if __name__ == "__main__":
    unittest.main()
