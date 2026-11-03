"""Offline behavior checks using a hand-authored, non-paper HTML fixture."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills" / "paper-foundations" / "scripts" / "paperread.py"
SPEC = importlib.util.spec_from_file_location("paperread_under_test", SCRIPT)
paperread = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = paperread
SPEC.loader.exec_module(paperread)
HTML = (Path(__file__).parent / "fixtures" / "synthetic_latexml.html").read_text(encoding="utf-8")


def parsed():
    return paperread.parse_html(HTML)


def atom(identifier="2610.27436v3"):
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<feed xmlns="http://www.w3.org/2005/Atom"><entry>'
        '<id>http://arxiv.org/abs/' + identifier + '</id>'
        '<title>Authoritative API title</title>'
        '<author><name>A. Researcher</name></author>'
        '<author><name>B. Collaborator</name></author>'
        '</entry></feed>'
    ).encode("utf-8")


class FakeClient:
    """Fail any unplanned request instead of ever accessing the network."""

    def __init__(self, routes=None):
        self.routes = routes or {}
        self.calls = []

    def get(self, url):
        self.calls.append(url)
        if url not in self.routes:
            raise AssertionError("Unexpected network request: " + url)
        value = self.routes[url]
        if isinstance(value, Exception):
            raise value
        return value


class InputTests(unittest.TestCase):
    def test_versioned_modern_identifiers_and_urls(self):
        for value in (
            "2610.27436v2",
            "https://arxiv.org/abs/2610.27436v2",
            "https://arxiv.org/pdf/2610.27436v2",
            "https://arxiv.org/pdf/2610.27436v2.pdf",
            "https://arxiv.org/html/2610.27436v2",
        ):
            with self.subTest(value=value):
                self.assertEqual(paperread.parse_arxiv(value), ("2610.27436", "v2"))

    def test_unversioned_and_old_identifiers(self):
        self.assertEqual(paperread.parse_arxiv("2610.27436")[0], "2610.27436")
        self.assertFalse(paperread.parse_arxiv("2610.27436")[1])
        self.assertEqual(
            paperread.parse_arxiv("https://arxiv.org/abs/cond-mat/0506581v1"),
            ("cond-mat/0506581", "v1"),
        )

    def test_non_arxiv_urls_and_path_traversal_are_rejected(self):
        for value in (
            "https://example.org/abs/2610.27436",
            "../../profile.json",
            "2610.27436/../../profile.json",
            "https://arxiv.org.evil.example/abs/2610.27436",
        ):
            with self.subTest(value=value), self.assertRaises(paperread.PaperError):
                paperread.parse_arxiv(value)


class ParserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = parsed()
        cls.paragraphs = {p["id"]: p for p in cls.data["paragraphs"]}

    def test_document_title_abstract_and_paragraph_order(self):
        self.assertEqual(self.data["title"], "A synthetic winding experiment")
        self.assertEqual(
            [p["id"] for p in self.data["paragraphs"]],
            ["abstract", "S1.p1", "S1.p2", "S1.SS1.p1", "S2.p1", "A1.p1"],
        )
        self.assertIn("We introduce a toy winding model.", self.paragraphs["abstract"]["text"])
        self.assertEqual(
            self.paragraphs["abstract"]["text"].count("We introduce a toy"), 1
        )

    def test_section_nesting_and_appendix(self):
        sections = {s["id"]: s for s in self.data["sections"]}
        self.assertEqual(sections["S1"]["level"], 1)
        self.assertEqual(sections["S1.SS1"]["level"], 2)
        self.assertEqual(sections["A1"]["level"], 1)
        self.assertEqual(self.paragraphs["S1.SS1.p1"]["section"], "S1.SS1")
        self.assertEqual(self.paragraphs["S2.p1"]["section"], "S2")
        self.assertEqual(sections["S1"]["title"], "1 Introduction")

    def test_inline_math_and_citations_preserve_reader_visible_content(self):
        text = self.paragraphs["S1.p1"]["text"]
        self.assertIn("$n = 2$", text)
        self.assertIn("[25]", text)
        self.assertNotIn("MATHML_SENTINEL", text)
        self.assertNotIn("\n", text)
        self.assertNotIn("  ", text)

    def test_notes_are_separate_and_page_chrome_and_bibliography_are_skipped(self):
        text = " ".join(p["text"] for p in self.data["paragraphs"])
        for sentinel in (
            "NOTE_SENTINEL", "BIBLIOGRAPHY_SENTINEL", "NAVIGATION_SENTINEL",
            "AUTHOR_SENTINEL", "DATE_SENTINEL", "LOGO_SENTINEL",
        ):
            self.assertNotIn(sentinel, text)
        self.assertIn("NOTE_SENTINEL", json.dumps(self.paragraphs["S1.p1"]["notes"]))

    def test_display_equation_has_placeholder_latex_and_paragraph(self):
        eqs = {e["id"]: e for e in self.data["equations"]}
        self.assertIn("⟨S1.E1⟩", self.paragraphs["S1.p2"]["text"])
        self.assertEqual(eqs["S1.E1"]["latex"], "H = t_1 + t_2")
        self.assertEqual(eqs["S1.E1"]["paragraph"], "S1.p2")
        self.assertIn("S1.E1", self.paragraphs["S1.p2"]["equations"])
        self.assertNotIn("H_SENTINEL", self.paragraphs["S1.p2"]["text"])

    def test_equation_group_rows_keep_their_own_addresses(self):
        eqs = {e["id"]: e for e in self.data["equations"]}
        self.assertEqual(eqs["S1.E2"]["latex"], "winding_a = 1")
        self.assertEqual(eqs["S1.E3"]["latex"], "winding_b = 2")
        self.assertEqual(eqs["S1.E2"]["paragraph"], "S1.SS1.p1")
        self.assertEqual(eqs["S1.E3"]["paragraph"], "S1.SS1.p1")
        self.assertNotIn("ROW_A_SENTINEL", self.paragraphs["S1.SS1.p1"]["text"])

    def test_subfigure_captions_are_merged_into_the_parent(self):
        self.assertEqual(len(self.data["figures"]), 1)
        figure = self.data["figures"][0]
        self.assertEqual(figure["id"], "S1.F1")
        self.assertEqual(figure["section"], "S1")
        for fragment in (
            "Panel A shows a winding loop.",
            "Panel B shows an open path.",
            "Figure 1: Synthetic winding paths.",
        ):
            self.assertIn(fragment, figure["caption"])
            self.assertEqual(figure["caption"].count(fragment), 1)


class NestedParserRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        html = (Path(__file__).parent / "fixtures" / "nested_latexml.html").read_text(encoding="utf-8")
        cls.data = paperread.parse_html(html)

    def test_display_math_inside_note_retains_its_latex(self):
        paragraph = self.data["paragraphs"][0]
        notes = " ".join(paragraph["notes"])
        self.assertIn("$z = x^2$", notes)
        self.assertNotIn("⟨S1.N1.E1⟩", notes)
        self.assertNotIn("NOTE_MATHML_SENTINEL", notes)
        self.assertNotIn("z = x^2", paragraph["text"])
        self.assertNotIn("S1.N1.E1", [eq["id"] for eq in self.data["equations"]])

    def test_nested_equation_points_to_the_emitted_outer_paragraph(self):
        self.assertEqual([p["id"] for p in self.data["paragraphs"]], ["S1.p1"])
        paragraph = self.data["paragraphs"][0]
        equation = next(eq for eq in self.data["equations"] if eq["id"] == "S1.E1")
        self.assertEqual(equation["paragraph"], "S1.p1")
        self.assertEqual(equation["latex"], "x = y + 1")
        self.assertIn("S1.E1", paragraph["equations"])
        self.assertIn("⟨S1.E1⟩", paragraph["text"])


class SentenceTests(unittest.TestCase):
    def test_known_abbreviations_do_not_end_a_sentence(self):
        text = "See Fig. Two, Eq. Three, Ref. Smith et al. More details follow. Next sentence."
        self.assertEqual(paperread.split_sentences(text), [
            "See Fig. Two, Eq. Three, Ref. Smith et al. More details follow.",
            "Next sentence.",
        ])

    def test_eg_ie_and_math_internal_periods_are_protected(self):
        text = "Use e.g. A or i.e. B with $x. Y$ and ⟨Eq. A⟩. Next result!"
        self.assertEqual(paperread.split_sentences(text), [
            "Use e.g. A or i.e. B with $x. Y$ and ⟨Eq. A⟩.",
            "Next result!",
        ])

    def test_sentence_can_start_with_inline_math(self):
        self.assertEqual(
            paperread.split_sentences(r"$n_{\downarrow}$. ${\cal T}$ symmetry requires"),
            [r"$n_{\downarrow}$.", r"${\cal T}$ symmetry requires"],
        )

    def test_math_internal_periods_stay_protected_before_math_leading_sentence(self):
        self.assertEqual(
            paperread.split_sentences("Use $x. Y$. $z. W$ follows."),
            ["Use $x. Y$.", "$z. W$ follows."],
        )

    def test_sentence_boundaries_question_mark_parenthesis_and_bracket(self):
        self.assertEqual(
            paperread.split_sentences("Ready? Yes! (A caveat.) [A source.]"),
            ["Ready?", "Yes!", "(A caveat.) [A source.]"],
        )

    def test_empty_text(self):
        self.assertEqual(paperread.split_sentences("  \n "), [])


class NavigationTests(unittest.TestCase):
    def setUp(self):
        self.data = parsed()

    def test_locate_returns_best_three_with_stable_sentence_ids(self):
        results = paperread.locate(self.data, "the WINDING  number is reused here")
        self.assertEqual(len(results), 3)
        best = results[0]
        self.assertEqual(best["id"], "S2.p1.s1")
        self.assertEqual(best["paragraph"], "S2.p1")
        self.assertEqual(best["section"], "S2")
        self.assertEqual(best["text"], "The WINDING number is reused here.")
        self.assertGreaterEqual(best["score"], results[1]["score"])

    def test_locate_can_match_a_pdf_quote_without_latex(self):
        results = paperread.locate(self.data, "Fig. 2 illustrates n equals two.")
        self.assertEqual(results[0]["paragraph"], "S1.p1")

    def test_context_uses_document_order_across_section_boundaries(self):
        result = paperread.context(self.data, "S1.SS1.p1", before=1, after=1)
        self.assertEqual(
            [p["id"] for p in result], ["S1.p2", "S1.SS1.p1", "S2.p1"]
        )
        self.assertEqual(result[2]["section"], "S2")
        self.assertEqual(
            [p["id"] for p in paperread.context(self.data, "abstract")],
            ["abstract", "S1.p1"],
        )

    def test_unknown_context_and_after_locations_are_errors(self):
        with self.assertRaises(paperread.PaperError):
            paperread.context(self.data, "S99.p1")
        with self.assertRaises(paperread.PaperError):
            paperread.find(self.data, "winding", after="S99.p1")

    def test_find_searches_body_captions_and_latex_without_case_sensitivity(self):
        result = paperread.find(self.data, "WINDING")
        ids = [r["id"] for r in result]
        for identifier in ("S1.p1", "S1.F1", "S1.E2", "S1.E3", "S2.p1"):
            self.assertIn(identifier, ids)
        by_id = {r["id"]: r for r in result}
        self.assertEqual(by_id["S1.F1"]["type"], "figure")
        self.assertEqual(by_id["S1.E2"]["type"], "equation")
        self.assertEqual(by_id["S1.E2"]["section"], "S1.SS1")

    def test_find_after_retains_later_figures_and_equations_in_document_order(self):
        ids = [r["id"] for r in paperread.find(self.data, "winding", after="S1.p2")]
        self.assertNotIn("S1.p1", ids)
        self.assertNotIn("S1.p2", ids)
        self.assertIn("S1.F1", ids)
        self.assertIn("S1.E2", ids)
        self.assertIn("S1.E3", ids)
        self.assertLess(ids.index("S1.F1"), ids.index("S1.E2"))
        self.assertLess(ids.index("S1.E2"), ids.index("S1.E3"))
        self.assertLess(ids.index("S1.E3"), ids.index("S2.p1"))


class TableRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = paperread.parse_html('''
            <main class="ltx_page_main"><section class="ltx_section" id="S2">
              <h2 class="ltx_title">2 Results</h2>
              <div class="ltx_para" id="S2.p1"><p>Winding before the table.</p></div>
              <figure class="ltx_table" id="S2.T1">
                <table><tr><td>1</td></tr></table>
                <figcaption>Table 1: Winding comparison.</figcaption>
              </figure>
              <div class="ltx_para" id="S2.p2"><p>Winding after the table.</p></div>
              <figure class="ltx_figure" id="S2.F1">
                <figcaption>Figure 1: Winding paths.</figcaption>
              </figure>
            </section></main>
        ''')

    def test_table_preserves_caption_section_and_document_order(self):
        self.assertEqual([f["id"] for f in self.data["figures"]], ["S2.T1", "S2.F1"])
        table = self.data["figures"][0]
        self.assertEqual(table["caption"], "Table 1: Winding comparison.")
        self.assertEqual(table["section"], "S2")
        before, after = self.data["paragraphs"]
        self.assertLess(before["end_order"], table["order"])
        self.assertLess(table["order"], after["order"])

    def test_find_searches_table_captions_and_respects_after(self):
        results = paperread.find(self.data, "WINDING", after="S2.p1")
        self.assertEqual([r["id"] for r in results], ["S2.T1", "S2.p2", "S2.F1"])
        self.assertEqual(results[0]["type"], "figure")
        self.assertEqual(results[0]["section"], "S2")
        self.assertIn("Winding comparison", results[0]["snippet"])
        self.assertEqual(
            [r["id"] for r in paperread.find(self.data, "winding", after="S2.p2")],
            ["S2.F1"],
        )

    def test_nested_table_and_figure_captions_merge_into_the_parent(self):
        for outer, inner in (("ltx_table", "ltx_figure"), ("ltx_figure", "ltx_table")):
            with self.subTest(outer=outer, inner=inner):
                data = paperread.parse_html('''
                    <main class="ltx_page_main"><section class="ltx_section" id="S2">
                      <figure class="%s" id="S2.visual1">
                        <figure class="%s" id="S2.visual1.a">
                          <figcaption>Panel A: Winding detail.</figcaption>
                        </figure>
                        <figcaption>Overall comparison.</figcaption>
                      </figure>
                    </section></main>
                ''' % (outer, inner))
                self.assertEqual([f["id"] for f in data["figures"]], ["S2.visual1"])
                self.assertEqual(data["figures"][0]["section"], "S2")
                self.assertEqual(
                    data["figures"][0]["caption"],
                    "Panel A: Winding detail. Overall comparison.",
                )


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "private"
        self.store = paperread.Store(self.home, client=FakeClient())

    def seed_paper(self, identifier="2610.27436v1", source="arxiv_html"):
        data = parsed()
        data.update(
            arxiv_id="2610.27436", version="v1", source=source,
            source_url="https://arxiv.org/html/" + identifier,
            fetched_at="2026-11-03T00:00:00Z", authors=[],
        )
        if source in ("pdf_only", "local_pdf"):
            for key in ("sections", "paragraphs", "equations", "figures"):
                data[key] = []
        path = self.home / "papers" / identifier
        path.mkdir(parents=True, exist_ok=True)
        (path / "paper.json").write_text(json.dumps(data), encoding="utf-8")
        return path, data

    def run_cli(self, *args, input_text=None):
        env = os.environ.copy()
        env["PAPERREAD_HOME"] = str(self.home)
        env["HOME"] = self.temp.name
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args], input=input_text,
            capture_output=True, text=True, env=env, timeout=10,
        )


class FetchTests(StoreCase):
    def test_api_resolves_latest_version_and_supplies_metadata(self):
        api_url = "https://export.arxiv.org/api/query?id_list=2610.27436"
        html_url = "https://arxiv.org/html/2610.27436v3"
        client = FakeClient({
            api_url: (atom(), api_url), html_url: (HTML.encode(), html_url),
        })
        store = paperread.Store(self.home, client=client)
        store.fetch("2610.27436")
        path, data = store.load("2610.27436v3")
        self.assertEqual(data["version"], "v3")
        self.assertEqual(data["title"], "Authoritative API title")
        self.assertEqual(data["authors"], ["A. Researcher", "B. Collaborator"])
        self.assertEqual(data["source"], "arxiv_html")
        self.assertEqual(client.calls, [api_url, html_url])
        self.assertTrue((Path(path) / "source.html").is_file())

    def test_old_identifier_is_resolved_without_creating_nested_cache_paths(self):
        api_url = "https://export.arxiv.org/api/query?id_list=cond-mat%2F0506581"
        html_url = "https://arxiv.org/html/cond-mat/0506581v2"
        client = FakeClient({
            api_url: (atom("cond-mat/0506581v2"), api_url),
            html_url: (HTML.encode(), html_url),
        })
        store = paperread.Store(self.home, client=client)
        store.fetch("cond-mat/0506581")
        path, data = store.load("cond-mat/0506581v2")
        self.assertEqual(data["arxiv_id"], "cond-mat/0506581")
        self.assertEqual(Path(path).parent, (self.home / "papers").resolve())
        self.assertEqual(data["version"], "v2")

    def test_explicit_version_cache_is_used_without_any_network_requests(self):
        self.seed_paper()
        self.store.fetch("https://arxiv.org/abs/2610.27436v1")
        self.assertEqual(self.store.client.calls, [])

    def test_html_failures_and_ar5iv_abstract_redirect_fall_back_to_pdf(self):
        html_url = "https://arxiv.org/html/2610.27436v1"
        ar5iv_url = "https://ar5iv.labs.arxiv.org/html/2610.27436v1"
        pdf_url = "https://arxiv.org/pdf/2610.27436v1"
        pdf = b"%PDF-1.4\nSynthetic fixture only\n%%EOF\n"
        client = FakeClient({
            html_url: (b"<html>HTML unavailable</html>", html_url),
            ar5iv_url: (HTML.encode(), "https://arxiv.org/abs/2610.27436v1"),
            pdf_url: (pdf, pdf_url),
        })
        store = paperread.Store(self.home, client=client)
        store.fetch("2610.27436v1")
        path, data = store.load("2610.27436v1")
        self.assertEqual(data["source"], "pdf_only")
        self.assertEqual(data["paragraphs"], [])
        self.assertEqual((Path(path) / "paper.pdf").read_bytes(), pdf)
        self.assertEqual(client.calls, [html_url, ar5iv_url, pdf_url])
        with self.assertRaisesRegex(paperread.PaperError, "PDF|pdf"):
            store.load("2610.27436v1", require_html=True)

    def test_valid_ar5iv_page_is_used_before_pdf(self):
        html_url = "https://arxiv.org/html/2610.27436v1"
        ar5iv_url = "https://ar5iv.labs.arxiv.org/html/2610.27436v1"
        client = FakeClient({
            html_url: paperread.DownloadError("not found", fallback_allowed=True),
            ar5iv_url: (HTML.encode(), ar5iv_url),
        })
        store = paperread.Store(self.home, client=client)
        store.fetch("2610.27436v1")
        _, data = store.load("2610.27436v1")
        self.assertEqual(data["source"], "ar5iv")
        self.assertTrue(data["paragraphs"])

    def test_corrupt_download_is_not_committed_as_a_cached_paper(self):
        html_url = "https://arxiv.org/html/2610.27436v1"
        ar5iv_url = "https://ar5iv.labs.arxiv.org/html/2610.27436v1"
        pdf_url = "https://arxiv.org/pdf/2610.27436v1"
        client = FakeClient({
            html_url: (b"<html>No paper</html>", html_url),
            ar5iv_url: (b"<html>No paper</html>", ar5iv_url),
            pdf_url: (b"<html>Rate limited</html>", pdf_url),
        })
        with self.assertRaises(paperread.PaperError):
            paperread.Store(self.home, client=client).fetch("2610.27436v1")
        self.assertEqual(list(self.home.glob("papers/**/paper.json")), [])
        self.assertEqual(list(self.home.glob("papers/**/paper.pdf")), [])

    def test_pdf_redirect_to_another_version_is_rejected_without_caching(self):
        html_url = "https://arxiv.org/html/2610.27436v1"
        ar5iv_url = "https://ar5iv.labs.arxiv.org/html/2610.27436v1"
        pdf_url = "https://arxiv.org/pdf/2610.27436v1"
        client = FakeClient({
            html_url: (b"<html>No HTML version</html>", html_url),
            ar5iv_url: (b"<html>No HTML version</html>", ar5iv_url),
            pdf_url: (b"%PDF-1.4\nWrong version\n%%EOF\n", "https://arxiv.org/pdf/2610.27436v2"),
        })
        with self.assertRaises(paperread.PaperError):
            paperread.Store(self.home, client=client).fetch("2610.27436v1")
        self.assertEqual(list(self.home.glob("papers/**/paper.json")), [])
        self.assertEqual(list(self.home.glob("papers/**/paper.pdf")), [])

    def test_transient_download_failure_does_not_silently_trigger_pdf_fallback(self):
        html_url = "https://arxiv.org/html/2610.27436v1"
        client = FakeClient({html_url: paperread.DownloadError("rate limited")})
        with self.assertRaises(paperread.PaperError):
            paperread.Store(self.home, client=client).fetch("2610.27436v1")
        self.assertEqual(client.calls, [html_url])

    def test_redirect_to_a_different_html_version_is_not_silently_accepted(self):
        html_url = "https://arxiv.org/html/2610.27436v1"
        ar5iv_url = "https://ar5iv.labs.arxiv.org/html/2610.27436v1"
        client = FakeClient({
            html_url: (HTML.encode(), "https://arxiv.org/html/2610.27436v2"),
            ar5iv_url: (HTML.encode(), ar5iv_url),
        })
        store = paperread.Store(self.home, client=client)
        store.fetch("2610.27436v1")
        _, data = store.load("2610.27436v1")
        self.assertEqual(data["version"], "v1")
        self.assertEqual(data["source"], "ar5iv")
        self.assertEqual(client.calls, [html_url, ar5iv_url])

    def test_refresh_replaces_source_but_preserves_reader_outline_and_log(self):
        path, _ = self.seed_paper()
        (path / "outline.md").write_text("Reader-authored outline", encoding="utf-8")
        (path / "log.jsonl").write_text('{"feedback": "Still unsure"}\n', encoding="utf-8")
        html_url = "https://arxiv.org/html/2610.27436v1"
        replacement = HTML.replace("A synthetic winding experiment", "Refreshed synthetic title")
        client = FakeClient({html_url: (replacement.encode(), html_url)})
        store = paperread.Store(self.home, client=client)
        store.fetch("2610.27436v1", refresh=True)
        _, data = store.load("2610.27436v1")
        self.assertEqual(data["title"], "Refreshed synthetic title")
        self.assertEqual((path / "outline.md").read_text(), "Reader-authored outline")
        self.assertEqual(json.loads((path / "log.jsonl").read_text())["feedback"], "Still unsure")
        self.assertEqual(client.calls, [html_url])

    def test_local_pdf_is_copied_and_can_be_loaded_by_cache_name(self):
        original = Path(self.temp.name) / "reader notes.pdf"
        content = b"%PDF-1.4\nSynthetic local fixture\n%%EOF\n"
        original.write_bytes(content)
        self.store.fetch(str(original))
        cached = list(self.home.glob("papers/*/paper.json"))
        self.assertEqual(len(cached), 1)
        path, data = self.store.load(cached[0].parent.name)
        self.assertEqual(data["source"], "local_pdf")
        self.assertEqual(data["paragraphs"], [])
        self.assertEqual((Path(path) / "paper.pdf").read_bytes(), content)
        self.assertEqual(original.read_bytes(), content)


class HttpClientTests(StoreCase):
    def test_request_spacing_is_shared_by_distinct_client_instances(self):
        clock = {"now": 1000.0}
        sent = []

        def sleep(seconds):
            self.assertGreaterEqual(seconds, 0)
            clock["now"] += seconds

        def respond(request, **kwargs):
            sent.append((clock["now"], request))
            response = mock.MagicMock()
            response.__enter__.return_value = response
            response.read.return_value = b"Synthetic response"
            response.geturl.return_value = request.full_url
            return response

        with mock.patch.object(paperread.time, "time", side_effect=lambda: clock["now"]), \
                mock.patch.object(paperread.time, "sleep", side_effect=sleep), \
                mock.patch.object(paperread, "urlopen", side_effect=respond):
            first = paperread.HttpClient(self.home)
            second = paperread.HttpClient(self.home)
            first_url = "https://arxiv.org/html/2610.27436v1"
            second_url = "https://arxiv.org/pdf/2610.27436v1"
            self.assertEqual(first.get(first_url), (b"Synthetic response", first_url))
            self.assertEqual(second.get(second_url), (b"Synthetic response", second_url))

        self.assertEqual(len(sent), 2)
        self.assertGreaterEqual(sent[1][0] - sent[0][0], 3.0)
        for _, request in sent:
            self.assertIn("paper", request.get_header("User-agent").lower())


class ProfileAndLogTests(StoreCase):
    def mark(self, concept, status, source=None):
        self.store.update_profile("mark", argparse.Namespace(
            concept=concept, status=status, source=source, paper=None, where=None,
        ))

    def test_empty_profile_is_private_and_valid(self):
        profile = self.store.profile()
        self.assertEqual(profile["areas"], {})
        self.assertEqual(profile["concepts"], {})

    def test_background_and_area_updates_survive_a_new_store_instance(self):
        self.store.update_profile("set-background", argparse.Namespace(text="Physics undergraduate"))
        self.store.update_profile(
            "set-area", argparse.Namespace(area="quantum-mechanics", level="studied")
        )
        profile = paperread.Store(self.home, client=FakeClient()).profile()
        self.assertEqual(profile["background"], "Physics undergraduate")
        self.assertEqual(profile["areas"]["quantum-mechanics"]["level"], "studied")
        self.assertIn("updated", profile["areas"]["quantum-mechanics"])

    def test_known_requires_reader_statement_and_explained_does_not_downgrade_known(self):
        with self.assertRaises(paperread.PaperError):
            self.mark("Berry phase", "known")
        self.mark("Berry phase", "known", "The reader said: I understand Berry phase.")
        self.mark("Berry phase", "explained")
        entry = self.store.profile()["concepts"]["Berry phase"]
        self.assertEqual(entry["status"], "known")
        self.assertIn("I understand", entry["source"])

    def test_reader_can_explicitly_correct_known_to_unsure(self):
        self.mark("Winding", "known", "The reader said: I know this.")
        self.mark("Winding", "unsure", "Actually, I am still uncertain.")
        self.assertEqual(self.store.profile()["concepts"]["Winding"]["status"], "unsure")

    def test_reexplaining_does_not_erase_a_readers_unsure_feedback(self):
        self.mark("Winding", "unsure", "I still do not understand.")
        self.mark("Winding", "explained")
        entry = self.store.profile()["concepts"]["Winding"]
        self.assertEqual(entry["status"], "unsure")
        self.assertEqual(entry["source"], "I still do not understand.")

    def test_log_appends_feedback_without_rewriting_previous_explanation(self):
        self.seed_paper()
        first = {"where": "S1.p1.s1", "explained": ["winding"], "feedback": None}
        second = {"where": "S1.p1.s1", "feedback": "I am still confused."}
        self.store.log_add("2610.27436v1", first)
        self.store.log_add("2610.27436v1", second)
        result = self.store.log_show("2610.27436v1", last=10)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["explained"], ["winding"])
        self.assertIsNone(result[0]["feedback"])
        self.assertEqual(result[1]["feedback"], "I am still confused.")
        self.assertIn("time", result[0])
        self.assertEqual(self.store.log_show("2610.27436v1", last=1), [result[1]])


class CliTests(StoreCase):
    def test_usage_and_invalid_input_errors_are_json_on_stdout(self):
        for args in (
            (), ("unknown-command",), ("fetch", "../../profile.json"),
            ("profile", "mark", "Berry phase", "known"),
            ("context", "2610.27436v1", "S1.p1", "--before", "-1"),
        ):
            with self.subTest(args=args):
                result = self.run_cli(*args)
                self.assertNotEqual(result.returncode, 0)
                payload = json.loads(result.stdout)
                self.assertIsInstance(payload["error"], str)
                self.assertNotIn("Traceback", result.stderr)

    def test_malformed_concept_record_reports_an_error_without_replacing_profile(self):
        self.home.mkdir(parents=True, exist_ok=True)
        path = self.home / "profile.json"
        original = '{"background": "", "areas": {}, "concepts": {"Berry": null}}'
        path.write_text(original, encoding="utf-8")
        result = self.run_cli("profile", "mark", "Berry", "explained")
        self.assertNotEqual(result.returncode, 0)
        self.assertIsInstance(json.loads(result.stdout)["error"], str)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_pdf_navigation_reports_json_errors(self):
        self.seed_paper(source="pdf_only")
        for args in (
            ("locate", "2610.27436v1", "winding"),
            ("context", "2610.27436v1", "S1.p1"),
            ("find", "2610.27436v1", "winding"),
        ):
            with self.subTest(args=args):
                result = self.run_cli(*args)
                self.assertNotEqual(result.returncode, 0)
                self.assertRegex(json.loads(result.stdout)["error"], "PDF|pdf")

    def test_log_add_accepts_json_from_stdin(self):
        self.seed_paper()
        result = self.run_cli(
            "log", "add", "2610.27436v1", "--json", "-",
            input_text=json.dumps({"where": "S1.p1.s1", "feedback": "Still unsure"}),
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        json.loads(result.stdout)
        shown = self.run_cli("log", "show", "2610.27436v1", "--last", "1")
        self.assertEqual(shown.returncode, 0, shown.stdout + shown.stderr)
        records = json.loads(shown.stdout)
        self.assertEqual(records[0]["feedback"], "Still unsure")

    def test_invalid_log_json_is_reported_without_writing_a_record(self):
        path, _ = self.seed_paper()
        result = self.run_cli(
            "log", "add", "2610.27436v1", "--json", "-", input_text="{broken"
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("error", json.loads(result.stdout))
        self.assertFalse((path / "log.jsonl").exists())

    def test_outline_path_reports_an_existing_reader_authored_outline(self):
        path, _ = self.seed_paper()
        (path / "outline.md").write_text("# A reader outline\n", encoding="utf-8")
        result = self.run_cli("outline-path", "2610.27436v1")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        output = json.loads(result.stdout)
        self.assertIn(str(path / "outline.md"), json.dumps(output))
        self.assertIn(True, output.values())


if __name__ == "__main__":
    unittest.main()
