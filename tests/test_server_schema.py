"""Self-contained M1 schema tests, plus an optional shared frontend check."""

import copy
import json
from pathlib import Path
import unittest

from server.schema import ValidationError, explanation_schema, validate_explanation


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "agent_explanations.json"
FRONTEND_FIXTURE = ROOT / "web" / "mock" / "explanations-2610.27436v1.json"


def html_ids(value):
    found = set()
    if isinstance(value, list):
        for item in value:
            found.update(html_ids(item))
    elif isinstance(value, dict):
        for key, item in value.items():
            if key in {"ids", "in_this_paper_where"}:
                found.update(item)
            elif key == "explained_at" and item is not None:
                found.add(item)
            found.update(html_ids(item))
    return found


def pdf_version(value):
    value = copy.deepcopy(value)

    def convert(item):
        if isinstance(item, list):
            for child in item:
                convert(child)
        elif isinstance(item, dict):
            if "ids" in item:
                item.update(ids=[], page=2, quote="Original passage from the PDF.")
            if "in_this_paper_where" in item:
                item["in_this_paper_where"] = ["PDF 第 2 页"]
            if item.get("explained_at") is not None:
                item["explained_at"] = "PDF page 1"
            for child in item.values():
                convert(child)

    convert(value)
    return value


class ExplanationSchemaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.examples = json.loads(FIXTURE.read_text(encoding="utf-8"))["explanations"]
        cls.ids = html_ids(cls.examples)

    def setUp(self):
        self.point = copy.deepcopy(self.examples[1])
        self.overview = copy.deepcopy(self.examples[0])

    def invalid(self, value, message=None, **kwargs):
        with self.assertRaises(ValidationError) as caught:
            validate_explanation(value, **kwargs)
        if message:
            self.assertIn(message, str(caught.exception))

    def test_synthetic_examples_pass_without_modification(self):
        for example in self.examples:
            with self.subTest(id=example["id"]):
                result = validate_explanation(example, paper="synthetic-contract-paper", valid_ids=self.ids)
                self.assertEqual(result, example)
                self.assertIsNot(result, example)
                self.assertIsNot(result["highlights"], example["highlights"])

    def test_shared_frontend_examples_pass_when_available(self):
        if not FRONTEND_FIXTURE.is_file():
            self.skipTest("The separately developed frontend fixtures are not present.")
        fixture = json.loads(FRONTEND_FIXTURE.read_text(encoding="utf-8"))
        identifiers = html_ids(fixture["explanations"])
        for example in fixture["explanations"]:
            with self.subTest(id=example["id"]):
                self.assertEqual(validate_explanation(example, paper=fixture["paper"], valid_ids=identifiers), example)

    def test_missing_mandatory_fields_rejected(self):
        for key in ("intro", "assumes", "concepts", "back_to_text", "unexpanded", "progress"):
            with self.subTest(key=key):
                value = copy.deepcopy(self.point)
                del value[key]
                self.invalid(value, key)

    def test_each_foundation_needs_both_columns_and_references(self):
        for key in ("in_this_paper", "itself", "in_this_paper_where"):
            for replacement in (None, "", "   ", [], {}):
                with self.subTest(key=key, replacement=replacement):
                    value = copy.deepcopy(self.point)
                    value["concepts"][0][key] = replacement
                    self.invalid(value, key)
        del self.point["concepts"][0]["itself"]
        self.invalid(self.point, "itself")

    def test_current_point_can_continue_while_foundations_unfinished(self):
        self.point["progress"]["next"] = {
            "point": self.point["point"], "where": copy.deepcopy(self.point["where"]),
        }
        self.point["back_to_text"] = "先补完当前基础，再回到原文。"
        validate_explanation(self.point, valid_ids=self.ids)

    def test_unknown_progress_and_end_of_reading_are_allowed(self):
        self.point["progress"].update(step=None, steps_total=None, next=None)
        validate_explanation(self.point)
        self.overview["next"] = None
        validate_explanation(self.overview)

    def test_empty_foundations_do_not_require_invented_concepts(self):
        self.point.update(assumes=[], concepts=[], unexpanded=[])
        validate_explanation(self.point)
        self.overview.update(foundations=[], previously=[])
        validate_explanation(self.overview)

    def test_new_is_a_display_status_and_validation_is_pure(self):
        before = copy.deepcopy(self.point)
        result = validate_explanation(self.point)
        self.assertEqual(result["assumes"][0]["status"], "new")
        self.assertEqual(self.point, before)
        for status in ("known", "explained", "unsure"):
            self.point["assumes"][0]["status"] = status
            validate_explanation(self.point)
        self.point["assumes"][0]["status"] = "learned"
        self.invalid(self.point, "status")

    def test_pdf_locations_need_pages_and_quotes_but_not_html_ids(self):
        for example in self.examples:
            value = pdf_version(example)
            validate_explanation(value, pdf=True, valid_ids=set())
        value = pdf_version(self.point)
        for reference in ("PDF 第 2 页", "第2页", "PDF page 2", "page 2", "p. 2"):
            value["concepts"][0]["in_this_paper_where"] = [reference]
            validate_explanation(value, pdf=True)

    def test_bool_and_nonpositive_pdf_pages_rejected(self):
        for page in (True, False, 0, -1, 1.5, "2", None):
            with self.subTest(page=page):
                value = pdf_version(self.point)
                value["where"]["page"] = page
                self.invalid(value, "page", pdf=True)

    def test_pdf_location_must_not_invent_html_id_or_omit_quote(self):
        value = pdf_version(self.point)
        value["where"]["ids"] = ["S1.p1"]
        self.invalid(value, "ids", pdf=True)
        value = pdf_version(self.point)
        value["where"]["quote"] = None
        self.invalid(value, "quote", pdf=True)
        for reference in ("S1.p1", "PDF 第 0 页", "page -1", "page True", "见正文", "PDF page 2.5"):
            with self.subTest(reference=reference):
                value = pdf_version(self.point)
                value["concepts"][0]["in_this_paper_where"] = [reference]
                self.invalid(value, "in_this_paper_where", pdf=True)

    def test_reference_membership_is_optional_but_enforced_when_given(self):
        self.point["where"]["ids"] = ["unknown-but-valid-id"]
        validate_explanation(self.point)
        self.invalid(self.point, "unknown HTML element", valid_ids=self.ids)
        self.point = copy.deepcopy(self.examples[1])
        self.point["concepts"][0]["in_this_paper_where"] = ["missing-id"]
        self.invalid(self.point, "in_this_paper_where", valid_ids=self.ids)
        self.point = copy.deepcopy(self.examples[1])
        self.point["assumes"][0]["explained_at"] = "missing-id"
        self.invalid(self.point, "explained_at", valid_ids=self.ids)

    def test_html_locations_need_well_formed_ids(self):
        for identifiers in ([], [""], ["S1 p1"], ["S1\x00p1"], ["<script>"], ["#S1.p1"], [5]):
            with self.subTest(identifiers=identifiers):
                self.point["where"]["ids"] = identifiers
                self.invalid(self.point, "ids")

    def test_sources_only_accept_safe_http_urls(self):
        for url in ("javascript:alert(1)", "data:text/html,a", "file:///etc/passwd",
                    "//example.org", "https://", "https://user:pass@example.org/",
                    "https://example.org/\nscript", "https://example.org\\@evil.org/",
                    "https://example.org:999999/", "https://example.org/<script>"):
            with self.subTest(url=url):
                self.point["concepts"][0]["sources"][0]["url"] = url
                self.invalid(self.point, "url")
        for url in ("https://arxiv.org/html/1509.02295v1#S2", "http://example.org/paper?a=1&b=2"):
            self.point["concepts"][0]["sources"][0]["url"] = url
            validate_explanation(self.point)

    def test_nested_types_empty_text_and_unknown_fields_rejected(self):
        for path, replacement in ((["intro"], " \n "), (["assumes"], {}),
                                  (["concepts", 0, "sources"], "citation"),
                                  (["progress", "text"], 123),
                                  (["highlights", 0, "label"], False)):
            with self.subTest(path=path):
                value = copy.deepcopy(self.point)
                target = value
                for key in path[:-1]:
                    target = target[key]
                target[path[-1]] = replacement
                self.invalid(value)
        self.point["override_profile"] = {"status": "known"}
        self.invalid(self.point, "unexpected field")

    def test_overview_uses_frontend_route_object_and_nested_fields(self):
        self.overview["route"] = self.overview["route"]["steps"]
        self.invalid(self.overview, "route")
        self.overview = copy.deepcopy(self.examples[0])
        self.overview["route"]["steps"][0]["range"] = "§ II–IV / Appendix A"
        validate_explanation(self.overview)
        del self.overview["model"]["items"][0]["where"]
        self.invalid(self.overview, "where")

    def test_progress_is_integer_not_boolean_and_consistent(self):
        for field in ("step", "steps_total"):
            for number in (True, 0, -1, 1.2, "2"):
                value = copy.deepcopy(self.point)
                value["progress"][field] = number
                self.invalid(value, field)
        self.point["progress"].update(step=7, steps_total=6)
        self.invalid(self.point, "steps_total")

    def test_model_schema_omits_service_identifiers_and_is_independent(self):
        for kind in ("point", "overview"):
            schema = explanation_schema(kind)
            self.assertNotIn("id", schema["properties"])
            self.assertNotIn("paper", schema["properties"])
            self.assertFalse(schema["additionalProperties"])
            schema["properties"].clear()
            self.assertIn("type", explanation_schema(kind)["properties"])
        self.point.pop("id")
        self.point.pop("paper")
        validate_explanation(self.point)

    def test_model_schemas_require_all_properties_for_strict_providers(self):
        def check(node):
            if not isinstance(node, dict):
                return
            if "properties" in node:
                self.assertEqual(set(node["properties"]), set(node["required"]))
                self.assertFalse(node["additionalProperties"])
                for child in node["properties"].values():
                    check(child)
            if "items" in node:
                check(node["items"])
        for kind in ("point", "overview"):
            check(explanation_schema(kind))

    def test_optional_section_and_skim_do_not_require_invented_content(self):
        self.point.pop("section")
        validate_explanation(self.point)
        self.point["section"] = None
        validate_explanation(self.point)
        self.overview["route"].pop("skim")
        validate_explanation(self.overview)
        for skim in (None, "", "   "):
            self.overview["route"]["skim"] = skim
            validate_explanation(self.overview)

    def test_validation_errors_never_echo_untrusted_field_names_or_ids(self):
        marker = "generated-private-text-token"
        self.point[marker] = "ignored"
        with self.assertRaises(ValidationError) as caught:
            validate_explanation(self.point)
        self.assertNotIn(marker, str(caught.exception))
        del self.point[marker]
        self.point["where"]["ids"] = [marker]
        with self.assertRaises(ValidationError) as caught:
            validate_explanation(self.point, valid_ids=self.ids)
        self.assertNotIn(marker, str(caught.exception))

    def test_envelope_identity_and_kind_are_checked(self):
        self.invalid(self.point, "paper", paper="other-paper")
        self.point["id"] = " "
        self.invalid(self.point, "id")
        for value in (None, [], "explanation", {}, {"type": "map"}, {"type": True}):
            self.invalid(value)
        with self.assertRaises(ValidationError):
            explanation_schema("map")


if __name__ == "__main__":
    unittest.main()
