"""Offline boundary and transition tests for the pure M3 map helpers."""

from copy import deepcopy
import unittest

from server.map_layout import (
    empty_layout, normalize_layout, replace_layout, validate_saved_layout,
)


def node(name="绕数", x=880, y=790):
    return {"name": name, "x": x, "y": y}


def note(identifier="n-1", x=652, y=520, text="回头再看。"):
    return {"id": identifier, "x": x, "y": y, "text": text}


def layout(*, version=3, nodes=None, notes=None):
    return {"version": version, "nodes": [node()] if nodes is None else nodes,
            "notes": [note()] if notes is None else notes}


class MapValidationTests(unittest.TestCase):
    def assert_invalid(self, value):
        with self.assertRaisesRegex(ValueError, "[\u4e00-\u9fff]"):
            normalize_layout(value)
        with self.assertRaisesRegex(ValueError, "地图文件损坏.*保留"):
            validate_saved_layout(value)

    def test_empty_is_fresh_and_valid(self):
        first = empty_layout()
        self.assertEqual(first, {"version": 0, "nodes": [], "notes": []})
        self.assertEqual(normalize_layout(first), first)
        first["nodes"].append(node())
        first["notes"].append(note())
        self.assertEqual(empty_layout(), {"version": 0, "nodes": [], "notes": []})

    def test_normalization_trims_names_but_preserves_note_text_and_order(self):
        value = layout(nodes=[node("  Linear Algebra\n"), node("绕数")],
                       notes=[note("N-1", text="  笔记\n换行。 "), note("n-1")])
        before = deepcopy(value)
        for validate in (normalize_layout, validate_saved_layout):
            with self.subTest(validate=validate.__name__):
                result = validate(value)
                self.assertEqual(result["nodes"], [node("Linear Algebra"), node("绕数")])
                self.assertEqual(result["notes"], before["notes"])
                result["nodes"][0]["x"] = 0
                result["notes"][0]["text"] = "edited"
                result["nodes"].append(node("new"))
                self.assertEqual(value, before)

    def test_top_level_shape_and_version_are_strict(self):
        invalid = [None, [], (), "map", 1, True, {}]
        for key in ("version", "nodes", "notes"):
            value = layout()
            del value[key]
            invalid.append(value)
        invalid.append(dict(layout(), extra=None))
        for version in (None, True, False, -1, 0.0, 1.0, "0", [], {},
                        float("nan"), float("inf"), float("-inf")):
            invalid.append(layout(version=version))
        for index, value in enumerate(invalid):
            with self.subTest(case=index):
                self.assert_invalid(value)
        for version in (0, 1, 10 ** 50):
            with self.subTest(version=version):
                self.assertEqual(normalize_layout(layout(version=version))["version"], version)

    def test_collections_must_be_arrays_with_bounded_counts(self):
        for key, factory, limit in (("nodes", node, 400), ("notes", note, 60)):
            for bad in (None, {}, (), "", 1, False):
                value = layout()
                value[key] = bad
                with self.subTest(key=key, bad=repr(bad)):
                    self.assert_invalid(value)
            for count in (0, 1, limit):
                value = layout()
                value[key] = [factory(str(i)) for i in range(count)]
                with self.subTest(key=key, count=count):
                    self.assertEqual(len(normalize_layout(value)[key]), count)
            value[key] = [factory(str(i)) for i in range(limit + 1)]
            with self.subTest(key=key, count=limit + 1):
                self.assert_invalid(value)

    def test_each_item_requires_exact_fields(self):
        for key, factory in (("nodes", node), ("notes", note)):
            invalid = [None, [], "item", 1, True, {}]
            for field in factory():
                item = factory()
                del item[field]
                invalid.append(item)
            invalid.append(dict(factory(), extra="unexpected"))
            for index, item in enumerate(invalid):
                value = layout()
                value[key] = [item]
                with self.subTest(key=key, case=index):
                    self.assert_invalid(value)

    def test_coordinates_are_inclusive_integers_without_boolean_or_float_coercion(self):
        for key, factory in (("nodes", node), ("notes", note)):
            for axis, maximum in (("x", 1440), ("y", 900)):
                for coordinate in (0, 1, maximum - 1, maximum):
                    item = factory()
                    item[axis] = coordinate
                    value = layout()
                    value[key] = [item]
                    with self.subTest(key=key, axis=axis, coordinate=coordinate):
                        self.assertEqual(normalize_layout(value)[key][0][axis], coordinate)
                for bad in (-1, maximum + 1, True, False, 0.0, 1.5, None, "1", [], {},
                            float("nan"), float("inf"), float("-inf")):
                    item = factory()
                    item[axis] = bad
                    value = layout()
                    value[key] = [item]
                    with self.subTest(key=key, axis=axis, bad=repr(bad)):
                        self.assert_invalid(value)

    def test_names_trim_then_check_length_and_casefold_duplicates(self):
        for name in ("x", "字" * 80, " \t" + "x" * 80 + "\n "):
            with self.subTest(name_length=len(name)):
                self.assertEqual(normalize_layout(layout(nodes=[node(name)]))["nodes"][0]["name"],
                                 name.strip())
        for bad in (None, 1, True, [], {}, "", " \t\n", "字" * 81):
            with self.subTest(bad=repr(bad)):
                self.assert_invalid(layout(nodes=[node(bad)]))
        for first, second in (("绕数", "绕数"), ("Name", " name "), ("Straße", "STRASSE")):
            with self.subTest(first=first, second=second):
                self.assert_invalid(layout(nodes=[node(first), node(second)]))

    def test_note_ids_are_ascii_untrimmed_unique_and_case_sensitive(self):
        for identifier in ("a", "A-z_09", "x" * 40, "_", "-"):
            with self.subTest(identifier=identifier):
                self.assertEqual(normalize_layout(layout(notes=[note(identifier)]))["notes"][0]["id"],
                                 identifier)
        for bad in (None, 1, True, [], {}, "", "x" * 41, " n-1", "n-1 ", "a\n",
                    "中文", "é", "ａ", "n/1", "n.1", "n 1"):
            with self.subTest(bad=repr(bad)):
                self.assert_invalid(layout(notes=[note(bad)]))
        self.assert_invalid(layout(notes=[note("n-1"), note("n-1")]))
        case_distinct = layout(notes=[note("n-1"), note("N-1")])
        self.assertEqual(normalize_layout(case_distinct), case_distinct)

    def test_note_text_counts_preserved_whitespace_but_rejects_blank_text(self):
        for text in ("字", "字" * 300, " " + "字" * 298 + " ", " \n字\t "):
            with self.subTest(length=len(text)):
                self.assertEqual(normalize_layout(layout(notes=[note(text=text)]))["notes"][0]["text"], text)
        for bad in (None, 1, True, [], {}, "", " \t\n", "字" * 301, " " + "字" * 300):
            with self.subTest(bad=repr(bad)[:60]):
                self.assert_invalid(layout(notes=[note(text=bad)]))

    def test_failed_validation_does_not_mutate_partially_processed_input(self):
        for value in (layout(nodes=[node(" trimmed "), node("")]),
                      layout(notes=[note("first"), note("second", text=" ")])):
            before = deepcopy(value)
            self.assert_invalid(value)
            self.assertEqual(value, before)


class MapReplacementTests(unittest.TestCase):
    def test_replace_adds_edits_and_removes_content_with_one_version_bump(self):
        current = layout(nodes=[node("delete"), node("keep")], notes=[note("old"), note("keep")])
        incoming = layout(nodes=[node("keep", x=0), node("new")],
                          notes=[note("keep", text="changed"), note("new")])
        before = deepcopy((current, incoming))
        result = replace_layout(current, incoming)
        self.assertEqual(result, dict(incoming, version=4))
        self.assertEqual((current, incoming), before)
        result["nodes"][0]["y"] = 0
        result["notes"][0]["text"] = "result changed"
        self.assertEqual((current, incoming), before)

    def test_noop_ignores_reordering_and_normalized_name_spaces_and_keeps_saved_order(self):
        current = layout(nodes=[node("first"), node("second")],
                         notes=[note("first"), note("second")])
        incoming = layout(nodes=[node(" second "), node("first")],
                          notes=[note("second"), note("first")])
        before = deepcopy((current, incoming))
        result = replace_layout(current, incoming)
        self.assertEqual(result, current)
        result["nodes"][0]["x"] = 0
        result["notes"][0]["text"] = "changed"
        self.assertEqual((current, incoming), before)

    def test_display_name_case_and_note_whitespace_are_content_changes(self):
        current = layout(nodes=[node("Name")], notes=[note(text="text")])
        for incoming in (layout(nodes=[node("NAME")], notes=[note(text="text")]),
                         layout(nodes=[node("Name")], notes=[note(text=" text ")])):
            with self.subTest(incoming=incoming):
                self.assertEqual(replace_layout(current, incoming), dict(incoming, version=4))

    def test_each_editable_field_change_and_removal_bumps_the_version(self):
        current = layout()
        for collection, field, replacement in (("nodes", "x", 0), ("nodes", "y", 0),
                                                ("nodes", "name", "new"),
                                                ("notes", "x", 0), ("notes", "y", 0),
                                                ("notes", "id", "new"), ("notes", "text", "new")):
            incoming = deepcopy(current)
            incoming[collection][0][field] = replacement
            with self.subTest(collection=collection, field=field):
                self.assertEqual(replace_layout(current, incoming), dict(incoming, version=4))
        for collection in ("nodes", "notes"):
            incoming = deepcopy(current)
            incoming[collection] = []
            with self.subTest(removed=collection):
                self.assertEqual(replace_layout(current, incoming), dict(incoming, version=4))

    def test_clear_and_empty_noop(self):
        result = replace_layout(layout(), layout(nodes=[], notes=[]))
        self.assertEqual(result, {"version": 4, "nodes": [], "notes": []})
        self.assertEqual(replace_layout(result, result), result)
        self.assertEqual(replace_layout(empty_layout(), empty_layout()), empty_layout())

    def test_malformed_inputs_and_stale_or_future_versions_are_rejected_without_mutation(self):
        cases = [(dict(layout(), version=True), layout()),
                 (layout(), dict(layout(), extra=None)),
                 (layout(), layout(version=2)), (layout(), layout(version=4))]
        for current, incoming in cases:
            before = deepcopy((current, incoming))
            with self.subTest(current_version=current["version"], incoming_version=incoming["version"]):
                with self.assertRaisesRegex(ValueError, "[\u4e00-\u9fff]"):
                    replace_layout(current, incoming)
                self.assertEqual((current, incoming), before)


if __name__ == "__main__":
    unittest.main()
