"""Offline tests of pure M2 concept-record validation and transitions."""

from copy import deepcopy
import unittest

from server.records import (
    apply_feedback, empty_record, mark_displayed, normalize_feedback,
    normalize_put, replace_record, validate_record,
)


OLD = "2026-11-15T08:30:00Z"
NOW = "2026-11-15T08:41:00Z"
LATER = "2026-11-15T08:42:00Z"


def entry(name="绕数", status="explained", *, updated=OLD, source="point-old", note=None):
    return {"name": name, "status": status, "explained_in": source,
            "where": "S1.p1" if source else None, "note": note, "updated": updated}


def record(*items, version=3):
    return {"version": version, "concepts": list(items)}


def point(*names, identifier="point-new"):
    return {"id": identifier, "type": "point",
            "where": {"ids": ["S2.p1", "S2.p2"], "page": None, "quote": None},
            "concepts": [{"name": name, "itself": "Expanded explanation."} for name in names],
            "assumes": [{"concept": "Only assumed", "status": "new"}],
            "unexpanded": ["Only mentioned"]}


def feedback(kind, concept=None, **kwargs):
    body = {"explanation": "point-new", "kind": kind}
    if concept is not None:
        body["concept"] = concept
    return dict(body, **kwargs)


def by_name(value):
    return {item["name"]: item for item in value["concepts"]}


class RecordValidationTests(unittest.TestCase):
    def test_empty_is_fresh_and_valid(self):
        value = empty_record()
        self.assertEqual(value, {"version": 0, "concepts": []})
        value["concepts"].append({})
        self.assertEqual(validate_record(empty_record()), {"version": 0, "concepts": []})

    def test_persisted_shape_is_copied_and_sorted_by_actual_time(self):
        value = record(entry("older", updated="2026-11-15T17:00:00+09:00"),
                       entry("newer", updated="2026-11-15T08:30:00Z"))
        before = deepcopy(value)
        result = validate_record(value)
        self.assertEqual([item["name"] for item in result["concepts"]], ["newer", "older"])
        result["concepts"][0]["note"] = "changed"
        self.assertEqual(value, before)

    def test_persisted_corruption_never_becomes_an_empty_record(self):
        invalid = [None, [], {}, {"version": True, "concepts": []},
                   {"version": -1, "concepts": []}, {"version": 1.0, "concepts": []},
                   {"version": 0, "concepts": None},
                   {"version": 0, "concepts": [], "extra": 1}]
        for key in entry():
            value = entry()
            del value[key]
            invalid.append(record(value))
        for key, value in [("name", " "), ("name", "a" * 81), ("status", "new"),
                           ("status", []), ("note", 3), ("note", "a" * 201),
                           ("where", " "), ("explained_in", []),
                           ("updated", "2026-11-15"), ("updated", "2026-02-30T08:00:00Z"),
                           ("updated", 10), ("unexpected", True)]:
            item = entry()
            item[key] = value
            invalid.append(record(item))
        invalid.extend([record(entry("STRASSE"), entry(" Straße ")),
                        record(*(entry(str(index)) for index in range(301)))])
        for value in invalid:
            with self.subTest(value=str(value)[:140]):
                with self.assertRaisesRegex(ValueError, "概念记录损坏"):
                    validate_record(value)

    def test_put_normalizes_names_and_missing_notes(self):
        body = {"version": 0, "concepts": [{"name": "  Linear Algebra  ", "status": "known"}]}
        original = deepcopy(body)
        version, items = normalize_put(body)
        self.assertEqual(version, 0)
        self.assertEqual(items, [{"name": "Linear Algebra", "status": "known", "note": None}])
        self.assertEqual(body, original)

    def test_put_accepts_limits_and_rejects_shape_or_limits(self):
        normalize_put({"version": 0, "concepts": [{"name": "a" * 80, "status": "known", "note": "n" * 200}]})
        normalize_put({"version": 0, "concepts": [{"name": str(i), "status": "known"} for i in range(300)]})
        invalid = [{"version": True, "concepts": []}, {"version": 0},
                   {"version": 0, "concepts": [], "extra": 1}]
        for item in [None, {"name": "x"}, {"name": "x", "status": "new"},
                     {"name": "a" * 81, "status": "known"},
                     {"name": "x", "status": "known", "explained_in": None},
                     {"name": "x", "status": "known", "note": "x" * 201}]:
            invalid.append({"version": 0, "concepts": [item]})
        invalid.extend([{"version": 0, "concepts": [{"name": name, "status": "known"} for name in ["straße", "STRASSE"]]},
                        {"version": 0, "concepts": [{"name": str(i), "status": "known"} for i in range(301)]}])
        for value in invalid:
            with self.subTest(value=str(value)[:140]), self.assertRaises(ValueError):
                normalize_put(value)


class RecordTransitionTests(unittest.TestCase):
    def test_replace_removes_changes_and_adds_once_preserving_existing_source(self):
        original = record(entry("delete"), entry("Keep", note="old"))
        before = deepcopy(original)
        result = replace_record(original, [{"name": "keep", "status": "known", "note": "new"},
                                           {"name": "manual", "status": "unsure"}], NOW)
        self.assertEqual(result["version"], 4)
        self.assertEqual(set(by_name(result)), {"keep", "manual"})
        self.assertEqual(by_name(result)["keep"], entry("keep", "known", updated=NOW, note="new"))
        self.assertEqual(by_name(result)["manual"], entry("manual", "unsure", updated=NOW, source=None))
        self.assertEqual(original, before)

    def test_replace_no_op_preserves_timestamp_and_version(self):
        current = record(entry(note="note"))
        result = replace_record(current, [{"name": " 绕数 ", "status": "explained", "note": "note"}], NOW)
        self.assertEqual(result, current)
        self.assertIsNot(result["concepts"][0], current["concepts"][0])

    def test_reordering_is_no_op_and_only_changed_entry_gets_timestamp(self):
        current = record(entry("old"), entry("keep"))
        result = replace_record(current, [{"name": "keep", "status": "explained"},
                                          {"name": "old", "status": "explained"}], NOW)
        self.assertEqual(result["version"], 3)
        result = replace_record(current, [{"name": "keep", "status": "explained"},
                                          {"name": "old", "status": "unsure"}], NOW)
        self.assertEqual(by_name(result)["old"]["updated"], NOW)
        self.assertEqual(by_name(result)["keep"]["updated"], OLD)
        self.assertEqual(result["concepts"][0]["name"], "old")

    def test_clear_bumps_version_once_including_empty_no_op(self):
        result = replace_record(record(entry()), [], NOW)
        self.assertEqual(result, {"version": 4, "concepts": []})
        self.assertEqual(replace_record(result, [], LATER), result)
        self.assertEqual(replace_record(empty_record(), [], NOW), empty_record())

    def test_display_adds_only_expanded_concepts_and_deduplicates(self):
        explanation = point("绕数", " Word ", "WORD")
        before = deepcopy(explanation)
        result = mark_displayed(empty_record(), explanation, NOW)
        self.assertEqual(result["version"], 1)
        self.assertEqual(set(by_name(result)), {"绕数", "Word"})
        for item in result["concepts"]:
            self.assertEqual((item["status"], item["explained_in"], item["where"], item["updated"]),
                             ("explained", "point-new", "S2.p1", NOW))
        self.assertEqual(mark_displayed(result, explanation, LATER), result)
        self.assertEqual(explanation, before)

    def test_display_preserves_known_unsure_and_notes_but_refreshes_sources(self):
        current = record(entry("known", "known", note="confirmed"), entry("unsure", "unsure", note="stuck"))
        before = deepcopy(current)
        result = mark_displayed(current, point("KNOWN", "unsure"), NOW)
        self.assertEqual(result["version"], 4)
        for item in result["concepts"]:
            self.assertEqual(item["status"], item["name"])
            self.assertEqual(item["note"], by_name(current)[item["name"]]["note"])
            self.assertEqual(item["explained_in"], "point-new")
        self.assertEqual(current, before)

    def test_overview_and_empty_point_do_not_change_record(self):
        current = record(entry())
        overview = {"id": "overview", "type": "overview", "concepts": [{"name": "not recorded"}]}
        self.assertEqual(mark_displayed(current, overview, NOW), current)
        self.assertEqual(mark_displayed(current, point(), NOW), current)

    def test_pdf_source_uses_page_label(self):
        explanation = point("绕数")
        explanation["where"] = {"ids": [], "page": 12, "quote": "PDF passage"}
        result = mark_displayed(empty_record(), explanation, NOW)
        self.assertEqual(result["concepts"][0]["where"], "PDF 第 12 页")

    def test_invalid_model_names_and_record_overflow_are_atomic_errors(self):
        current = record(*(entry(str(i)) for i in range(300)))
        original = deepcopy(current)
        for explanation in [point("new"), point("a" * 81), point(" "), point(None),
                            point(*(str(i) for i in range(301)))]:
            with self.subTest(explanation=str(explanation)[:80]), self.assertRaises(ValueError):
                mark_displayed(current, explanation, NOW)
        self.assertEqual(current, original)


class RecordFeedbackTests(unittest.TestCase):
    def test_feedback_shape_and_canonical_values(self):
        self.assertEqual(normalize_feedback(feedback("stuck", " x ")),
                         feedback("stuck", "x"))
        self.assertEqual(normalize_feedback(feedback("unclear", note=None)),
                         feedback("unclear", note=None))
        invalid = [None, {}, feedback("other"), feedback("stuck"), feedback("known", " "),
                   feedback("known", "x" * 81), feedback("unclear", "x"),
                   {"explanation": "point-new", "kind": "unclear", "concept": None}, feedback("known", "x", note=7),
                   feedback("known", "x", note="n" * 201), feedback("unclear", extra=1),
                   {"explanation": " ", "kind": "unclear"},
                   {"explanation": "x" * 129, "kind": "unclear"},
                   {"explanation": "point-new", "kind": []}]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_feedback(value)

    def test_new_stuck_and_known_get_null_sources_when_not_expanded(self):
        for kind, status in [("stuck", "unsure"), ("known", "known")]:
            with self.subTest(kind=kind):
                result = apply_feedback(empty_record(), point("expanded"), feedback(kind, "manual"), NOW)
                self.assertEqual(result, record(entry("manual", status, source=None, updated=NOW), version=1))
                self.assertEqual(apply_feedback(result, point("expanded"), feedback(kind, "manual"), LATER), result)

    def test_new_feedback_concept_uses_source_if_actually_expanded(self):
        result = apply_feedback(empty_record(), point("Winding"), feedback("stuck", "winding"), NOW)
        item = result["concepts"][0]
        self.assertEqual((item["explained_in"], item["where"]), ("point-new", "S2.p1"))

    def test_feedback_preserves_existing_sources_and_omitted_note(self):
        current = record(entry("绕数", "known", note="keep"))
        original = deepcopy(current)
        result = apply_feedback(current, point("绕数"), feedback("stuck", "绕数"), NOW)
        self.assertEqual(result["concepts"][0], entry("绕数", "unsure", updated=NOW, note="keep"))
        self.assertEqual(current, original)
        cleared = apply_feedback(result, point("绕数"), feedback("stuck", "绕数", note=None), LATER)
        self.assertIsNone(cleared["concepts"][0]["note"])
        self.assertEqual(cleared["version"], 5)

    def test_unclear_changes_expanded_only_and_leaves_known_entirely_unchanged(self):
        known = entry("known", "known", note="leave this")
        current = record(known, entry("expanded"), entry("unexpanded"))
        explanation = point("known", "expanded", "new", "NEW")
        result = apply_feedback(current, explanation, feedback("unclear", note="still stuck"), NOW)
        self.assertEqual(result["version"], 4)
        self.assertEqual(by_name(result)["known"], known)
        self.assertEqual(by_name(result)["unexpanded"], by_name(current)["unexpanded"])
        for name in ("expanded", "new"):
            self.assertEqual(by_name(result)[name]["status"], "unsure")
            self.assertEqual(by_name(result)[name]["note"], "still stuck")
        self.assertEqual(apply_feedback(result, explanation, feedback("unclear", note="still stuck"), LATER), result)

    def test_overview_unclear_has_no_concepts_but_named_feedback_is_allowed(self):
        overview = {"type": "overview", "id": "point-new", "foundations": [{"name": "foundation"}]}
        self.assertEqual(apply_feedback(empty_record(), overview, feedback("unclear"), NOW), empty_record())
        result = apply_feedback(empty_record(), overview, feedback("stuck", "foundation"), NOW)
        self.assertIsNone(result["concepts"][0]["explained_in"])

    def test_feedback_overflow_and_wrong_explanation_fail_without_mutation(self):
        current = record(*(entry(str(i)) for i in range(300)))
        before = deepcopy(current)
        with self.assertRaises(ValueError):
            apply_feedback(current, point("new"), feedback("stuck", "new"), NOW)
        with self.assertRaises(ValueError):
            apply_feedback(current, point(identifier="other"), feedback("known", "0"), NOW)
        self.assertEqual(current, before)


if __name__ == "__main__":
    unittest.main()
