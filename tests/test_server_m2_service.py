"""M2 persistence, request-version gating and feedback-driven reading with synthetic data."""
import copy
import json
import threading
import unittest
from unittest.mock import patch

import test_server_service as fixtures
from server.service import Application, core

PAPER = fixtures.PAPER


class M2ServiceTests(unittest.TestCase):
    setUp = fixtures.ServiceTests.setUp

    def turn(self, response=None, request=None, app=None):
        app = app or self.app
        if response is not None:
            app.backend.responses = [response]
        body = fixtures.reading_request(request or {"mode": "continue"}, app=app)
        task = app.explain(PAPER, body)
        events = [e for e in app.task_events(task) if e]
        self.assertTrue(app.wait_closed(2))
        return events[-1][1:]

    def display_point(self, value=None):
        kind, overview = self.turn(fixtures.overview())
        self.assertEqual(kind, "result")
        self.app.acknowledge(PAPER, overview["id"])
        kind, point = self.turn(value or fixtures.point())
        self.assertEqual(kind, "result")
        self.app.acknowledge(PAPER, point["id"])
        return point

    def items(self, app=None):
        return {item["name"]: item for item in (app or self.app).record(PAPER)["concepts"]}

    def material(self, prompt=None):
        text = (prompt or self.backend.prompts[-1]).split("本轮材料（JSON 数据）：\n", 1)[1]
        return json.JSONDecoder().raw_decode(text)[0]

    def test_empty_record_does_not_migrate_profile_or_logs_or_create_state(self):
        core.write_json(self.home / "profile.json", {"background": "PRIVATE_OLD", "areas": {},
                                                   "concepts": {"Old": {"status": "known"}}})
        self.store.log_add(PAPER, {"explained": ["LEGACY_CONCEPT"]})
        with patch.object(self.store, "profile", side_effect=AssertionError("No profile")), \
                patch.object(self.store, "log_show", side_effect=AssertionError("No log migration")):
            self.assertEqual(self.app.record(PAPER), {"version": 0, "concepts": []})
            self.assertFalse((self.path / "agent-state.json").exists())
            self.assertEqual(self.turn(fixtures.overview())[0], "result")
        self.assertNotIn("PRIVATE_OLD", self.backend.prompts[0])
        self.assertNotIn("LEGACY_CONCEPT", self.backend.prompts[0])

    def test_required_or_stale_version_is_rejected_before_task_creation(self):
        for value in (None, True, -1, 0.0, "0"):
            body = {"mode": "continue", "reading_context": "Start with basics"}
            if value is not None:
                body["record_version"] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.app.explain(PAPER, body)
        with self.assertRaises(RuntimeError):
            self.app.explain(PAPER, {"mode": "continue", "reading_context": "Basics", "record_version": 9})
        self.assertEqual(self.app.tasks, {})
        self.assertEqual(self.backend.prompts, [])
        self.assertFalse((self.path / "agent-state.json").exists())

    def test_display_is_atomic_idempotent_and_does_not_write_old_profile(self):
        first = self.display_point()
        record = self.app.record(PAPER)
        self.assertEqual(record["version"], 1)
        self.assertEqual(set(self.items()), {"Necessary foundation"})
        self.assertEqual(self.items()["Necessary foundation"]["explained_in"], first["id"])
        self.assertEqual(self.items()["Necessary foundation"]["where"], "S1.p1")
        self.app.acknowledge(PAPER, first["id"])
        self.assertEqual(self.app.record(PAPER), record)
        self.assertFalse((self.home / "profile.json").exists())

    def test_put_retains_sources_adds_deletes_and_stale_put_leaves_file_unchanged(self):
        first = self.display_point()
        updated = self.app.update_record(PAPER, {"version": 1, "concepts": [
            {"name": "necessary FOUNDATION", "status": "known", "note": "I understand"},
            {"name": "New term", "status": "unsure"}]})
        self.assertEqual(updated["version"], 2)
        self.assertEqual(self.items()["necessary FOUNDATION"]["explained_in"], first["id"])
        self.assertIsNone(self.items()["New term"]["where"])
        before = (self.path / "agent-state.json").read_bytes()
        with self.assertRaises(RuntimeError):
            self.app.update_record(PAPER, {"version": 1, "concepts": []})
        self.assertEqual((self.path / "agent-state.json").read_bytes(), before)
        self.assertEqual(self.app.update_record(PAPER, {"version": 2, "concepts": []}),
                         {"version": 3, "concepts": []})
        self.assertEqual(self.app.update_record(PAPER, {"version": 3, "concepts": []})["version"], 3)

    def test_feedback_adds_unknown_term_and_retries_once_even_after_restart(self):
        first = self.display_point()
        body = {"explanation": first["id"], "kind": "stuck", "concept": " Unknown term ", "note": "Why?"}
        record = self.app.feedback(PAPER, body)
        self.assertEqual(self.items()["Unknown term"]["status"], "unsure")
        self.assertIsNone(self.items()["Unknown term"]["explained_in"])
        other = Application(self.store, fixtures.FakeBackend())
        self.addCleanup(other.close)
        self.assertEqual(other.record(PAPER), record)
        self.assertEqual(other.feedback(PAPER, dict(body, concept="unknown TERM")), record)
        logs = [entry for entry in self.store.log_show(PAPER, last=0) if entry.get("feedback_id")]
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0]["feedback"], "Why?")
        self.assertEqual(logs[0]["explained"], [])

    def test_known_and_unsure_survive_later_explanation_with_new_source(self):
        value = fixtures.point()
        value["concepts"].append(dict(value["concepts"][0], name="Another concept"))
        first = self.display_point(value)
        self.app.feedback(PAPER, {"explanation": first["id"], "kind": "known", "concept": "Necessary foundation"})
        self.app.feedback(PAPER, {"explanation": first["id"], "kind": "stuck", "concept": "Another concept"})
        _, second = self.turn(value)
        self.app.acknowledge(PAPER, second["id"])
        self.assertEqual(self.items()["Necessary foundation"]["status"], "known")
        self.assertEqual(self.items()["Another concept"]["status"], "unsure")
        self.assertEqual({item["explained_in"] for item in self.items().values()}, {second["id"]})

    def test_unclear_preserves_known_and_repeats_location_then_continues(self):
        value = fixtures.point(next_id="S1.p2")
        value["concepts"].append(dict(value["concepts"][0], name="Already known"))
        first = self.display_point(value)
        self.app.feedback(PAPER, {"explanation": first["id"], "kind": "known", "concept": "Already known"})
        record = self.app.feedback(PAPER, {"explanation": first["id"], "kind": "unclear", "note": "Try a picture"})
        self.assertEqual(self.items()["Already known"]["status"], "known")
        self.assertEqual(self.items()["Necessary foundation"]["status"], "unsure")
        bad = fixtures.point(next_id="S1.p2")
        bad["where"] = dict(fixtures.LOCATION, ids=["S1.p2"])
        self.backend.responses = [bad, fixtures.point(next_id="S1.p2")]
        kind, repeated = self.turn()
        self.assertEqual(kind, "result")
        material = self.material()
        self.assertEqual(material["concept_record"], record)
        self.assertEqual(material["repeat_point"]["where"], first["where"])
        self.assertEqual(material["unsure_previous_explanations"][0]["previous_teaching"][0]["itself"],
                         first["concepts"][0]["itself"])
        self.app.acknowledge(PAPER, repeated["id"])
        forward = fixtures.point(next_id="S1.p2")
        forward["where"] = dict(fixtures.LOCATION, ids=["S1.p2"])
        forward["progress"]["step"] = 2
        self.assertEqual(self.turn(forward)[0], "result")
        self.assertIsNone(self.material()["repeat_point"])

    def test_unclear_step_must_stay_and_failed_generation_keeps_repeat_on_restart(self):
        first = self.display_point(fixtures.point(next_id="S1.p2"))
        self.app.feedback(PAPER, {"explanation": first["id"], "kind": "unclear"})
        bad = fixtures.point()
        bad["progress"]["step"] = 2
        self.backend.responses = [bad, bad]
        self.assertEqual(self.turn()[0], "error")
        other = Application(self.store, fixtures.FakeBackend(fixtures.point()))
        self.addCleanup(other.close)
        self.assertEqual(self.turn(app=other)[0], "result")
        self.assertEqual(self.material(other.backend.prompts[-1])["repeat_point"]["step"], 1)

    def test_unclear_at_end_can_repeat_and_ask_still_targets_selected_text(self):
        value = fixtures.point()
        value["progress"]["next"] = None
        first = self.display_point(value)
        self.app.feedback(PAPER, {"explanation": first["id"], "kind": "unclear"})
        kind, repeated = self.turn(value)
        self.assertEqual(kind, "result")
        self.app.acknowledge(PAPER, repeated["id"])
        self.app.feedback(PAPER, {"explanation": repeated["id"], "kind": "unclear"})
        answer = fixtures.point()
        answer["where"] = dict(fixtures.LOCATION, ids=["S1.p2"])
        self.assertEqual(self.turn(answer, {"mode": "ask", "question": "This one?", "where": "S1.p2"})[0], "result")
        self.assertIsNone(self.material()["repeat_point"])

    def test_assumption_status_and_source_match_current_casefold_record(self):
        first = self.display_point()
        self.app.feedback(PAPER, {"explanation": first["id"], "kind": "known", "concept": "Necessary foundation"})
        good = fixtures.point("Different term")
        good["assumes"] = [{"concept": " necessary FOUNDATION ", "status": "known",
                            "explained_at": "S1.p1", "note": "Reader confirmed"}]
        for field, invalid in (("status", "new"), ("explained_at", None)):
            bad = copy.deepcopy(good)
            bad["assumes"][0][field] = invalid
            self.backend.responses = [bad, bad]
            self.assertEqual(self.turn()[0], "error")
        self.assertEqual(self.turn(good)[0], "result")

    def test_feedback_and_put_blocked_while_model_is_running(self):
        first = self.display_point()
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def generate(prompt, schema):
            entered.set()
            release.wait(3)
            return fixtures.point()
        self.backend.generate = generate
        identifier = self.app.explain(PAPER, fixtures.reading_request({"mode": "continue"}, app=self.app))
        self.assertTrue(entered.wait(2))
        before = (self.path / "agent-state.json").read_bytes()
        with self.assertRaises(RuntimeError):
            self.app.update_record(PAPER, {"version": 1, "concepts": []})
        with self.assertRaises(RuntimeError):
            self.app.feedback(PAPER, {"explanation": first["id"], "kind": "unclear"})
        self.assertEqual((self.path / "agent-state.json").read_bytes(), before)
        self.app.cancel_task(identifier)
        release.set()
        self.assertTrue(self.app.wait_closed(2))

    def test_feedback_outbox_recovers_log_failure_without_reapplying_record(self):
        first = self.display_point()
        body = {"explanation": first["id"], "kind": "stuck", "concept": "Word"}
        with patch.object(self.store, "log_add", side_effect=OSError("simulated log failure")):
            with self.assertRaises(OSError):
                self.app.feedback(PAPER, body)
        record = self.app.record(PAPER)
        other = Application(self.store, fixtures.FakeBackend())
        self.addCleanup(other.close)
        self.assertEqual(other.feedback(PAPER, body), record)
        self.assertEqual(other.feedback(PAPER, body), record)
        feedback = [item for item in self.store.log_show(PAPER, last=0) if item.get("feedback_id")]
        self.assertEqual(len(feedback), 1)
        self.assertFalse(core.read_json(self.path / "agent-state.json")["feedback_outbox"])

    def test_feedback_outbox_clear_failure_after_log_append_recovers_once(self):
        first = self.display_point()
        body = {"explanation": first["id"], "kind": "stuck", "concept": "Word"}
        write = core.write_json
        calls = []
        def fail_second(path, value):
            calls.append(path)
            if len(calls) == 2:
                raise OSError("simulated outbox acknowledgement failure")
            return write(path, value)
        with patch("server.service.core.write_json", side_effect=fail_second):
            with self.assertRaises(OSError):
                self.app.feedback(PAPER, body)
        before = self.app.record(PAPER)
        other = Application(self.store, fixtures.FakeBackend())
        self.addCleanup(other.close)
        self.assertEqual(other.feedback(PAPER, body), before)
        feedback = [entry for entry in self.store.log_show(PAPER, last=0) if entry.get("feedback_id")]
        self.assertEqual(len(feedback), 1)

    def test_changed_record_cannot_recover_old_pending_card_as_new_generation(self):
        _, overview = self.turn(fixtures.overview())
        self.app.acknowledge(PAPER, overview["id"])
        _, point = self.turn(fixtures.point())
        self.app.update_record(PAPER, {"version": 0, "concepts": [
            {"name": "Only assumed", "status": "known"}]})
        count, tasks = len(self.backend.prompts), len(self.app.tasks)
        with self.assertRaisesRegex(RuntimeError, "恢复历史"):
            self.turn()
        self.assertEqual((len(self.backend.prompts), len(self.app.tasks)), (count, tasks))
        # History is still available; acknowledging it does not rewrite its original prose.
        self.assertEqual(self.app.history(PAPER)[-1]["id"], point["id"])
        self.app.acknowledge(PAPER, point["id"])
        fresh = fixtures.point()
        fresh["assumes"][0]["status"] = "known"
        kind, result = self.turn(fresh)
        self.assertEqual(kind, "result")
        self.assertNotEqual(result["id"], point["id"])

    def test_note_only_edit_also_requires_pending_card_receipt(self):
        self.display_point()
        self.turn(fixtures.point())  # generated using version 1, not displayed yet
        self.app.update_record(PAPER, {"version": 1, "concepts": [
            {"name": "Necessary foundation", "status": "explained", "note": "Use an everyday example"}]})
        with self.assertRaisesRegex(RuntimeError, "恢复历史"):
            self.turn()

    def test_ack_crash_after_log_does_not_apply_record_twice(self):
        kind, overview = self.turn(fixtures.overview())
        self.app.acknowledge(PAPER, overview["id"])
        _, point = self.turn(fixtures.point())
        with patch("server.service.core.write_json", side_effect=OSError("simulated atomic write failure")):
            with self.assertRaises(OSError):
                self.app.acknowledge(PAPER, point["id"])
        self.assertEqual(self.app.record(PAPER)["version"], 0)
        self.app.acknowledge(PAPER, point["id"])
        self.app.acknowledge(PAPER, point["id"])
        self.assertEqual(self.app.record(PAPER)["version"], 1)
        matching = [item for item in self.store.log_show(PAPER, last=0) if item.get("explanation_id") == point["id"]]
        self.assertEqual(len(matching), 1)

    def test_feedback_before_receipt_does_not_suppress_actual_reading_log(self):
        _, overview = self.turn(fixtures.overview())
        self.app.acknowledge(PAPER, overview["id"])
        _, point = self.turn(fixtures.point())
        # A feedback action can reach the server while the display receipt is retrying.
        self.app.feedback(PAPER, {"explanation": point["id"], "kind": "unclear"})
        self.app.acknowledge(PAPER, point["id"])
        self.app.acknowledge(PAPER, point["id"])
        matching = [entry for entry in self.store.log_show(PAPER, last=0)
                    if entry.get("explanation_id") == point["id"]]
        self.assertEqual(len(matching), 2)
        self.assertEqual(len([entry for entry in matching if entry["explained"]]), 1)
        self.assertEqual(self.items()["Necessary foundation"]["status"], "unsure")

    def test_records_do_not_cross_papers_and_bad_feedback_is_nonmutating(self):
        first = self.display_point()
        other = "2601.00002v1"
        core.write_json(self.home / "papers" / other / "paper.json", core.read_json(self.path / "paper.json"))
        self.assertEqual(self.app.record(other), {"version": 0, "concepts": []})
        before = (self.path / "agent-state.json").read_bytes()
        with self.assertRaises(FileNotFoundError):
            self.app.feedback(PAPER, {"explanation": "missing", "kind": "unclear"})
        with self.assertRaises(ValueError):
            self.app.feedback(PAPER, {"explanation": first["id"], "kind": "unclear", "concept": "Word"})
        self.assertEqual((self.path / "agent-state.json").read_bytes(), before)

    def test_overview_receipt_and_unclear_do_not_record_mentioned_foundations(self):
        _, overview = self.turn(fixtures.overview())
        self.app.acknowledge(PAPER, overview["id"])
        self.app.feedback(PAPER, {"explanation": overview["id"], "kind": "unclear"})
        self.assertEqual(self.app.record(PAPER), {"version": 0, "concepts": []})
        self.assertEqual(self.turn(fixtures.point())[0], "result")
        self.assertIsNone(self.material()["repeat_point"])

    def test_malformed_persisted_record_is_preserved_not_reset(self):
        state = {"explanations": [], "displayed": [], "pending": None,
                 "concept_record": {"version": True, "concepts": []}}
        core.write_json(self.path / "agent-state.json", state)
        before = (self.path / "agent-state.json").read_bytes()
        with self.assertRaises(ValueError):
            self.app.record(PAPER)
        with self.assertRaises(ValueError):
            self.app.update_record(PAPER, {"version": 0, "concepts": []})
        self.assertEqual((self.path / "agent-state.json").read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
