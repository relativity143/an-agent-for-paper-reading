"""Offline lookup checks; all service replies below are hand-authored fixtures."""
import copy
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock
from urllib.parse import quote, urlencode


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills" / "paper-foundations" / "scripts" / "paperread.py"
SPEC = importlib.util.spec_from_file_location("paperread_lookup", SCRIPT)
paperread = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = paperread
SPEC.loader.exec_module(paperread)
DOI = "10.0000/synthetic.(26)"
OPENALEX_URL = "https://api.openalex.org/works/https://doi.org/" + quote(DOI, safe="/")
CROSSREF_URL = "https://api.crossref.org/works/" + quote(DOI, safe="")
OPENALEX = {
    "id": "https://openalex.org/W000026",
    "doi": "https://doi.org/" + DOI,
    "title": "A synthetic lookup study",
    "authorships": [{"author": {"display_name": "A. Example"}}, {"author": {"display_name": "B. Example"}}],
    "publication_year": 2026,
    "abstract_inverted_index": {"states.": [4], "We": [0, 2], "compare.": [1], "measure": [3]},
    "primary_location": {"landing_page_url": "https://arxiv.org/abs/0000.00026v2", "pdf_url": None},
}
CROSSREF = {
    "status": "ok",
    "message": {
        "DOI": DOI.upper(),
        "title": ["A synthetic <i>lookup</i> study"],
        "author": [{"given": "A.", "family": "Example"}, {"name": "Example Collaboration"}],
        "published": {"date-parts": [[2026, 1, 1]]},
        "abstract": "<jats:abstract><jats:p>We test <jats:italic>edges</jats:italic> &amp; states.</jats:p>"
                    "<jats:p>Only synthetic evidence.</jats:p></jats:abstract>",
        "URL": "https://example.org/synthetic-work",
    },
}


def arxiv_url(identifier="0000.00026v2"):
    return "https://export.arxiv.org/api/query?" + urlencode({"id_list": identifier})


def atom(identifier="0000.00026v2", summary="Synthetic states &amp; edges.", doi=DOI):
    return ('<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">'
            '<entry><id>http://arxiv.org/abs/' + identifier + '</id><title> A synthetic lookup study </title>'
            '<author><name>A. Example</name></author><published>2025-12-01T00:00:00Z</published>'
            '<updated>2026-01-01T00:00:00Z</updated><summary>' + summary + '</summary>'
            '<arxiv:doi>' + doi + '</arxiv:doi><link href="https://arxiv.org/pdf/' + identifier + '"/>'
            '</entry></feed>').encode("utf-8")


class FakeClient:
    """Unplanned calls fail immediately and can never fall through to networking."""
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
        if isinstance(value, dict):
            value = json.dumps(value).encode("utf-8")
        return value, url


class DoiInputTests(unittest.TestCase):
    def test_canonical_spellings_share_identifier(self):
        for value in (DOI, DOI.upper(), "doi: " + DOI.upper(), "https://doi.org/" + DOI,
                      "http://dx.doi.org/" + DOI.upper(), "https://doi.org/" + quote(DOI, safe=""),
                      "[" + DOI + "]", "(" + DOI + ")", "<https://doi.org/" + DOI + ">"):
            with self.subTest(value=value):
                self.assertEqual(paperread.parse_doi(value), DOI)

    def test_legal_suffix_punctuation_is_not_trimmed(self):
        for suffix in ("sici(1995)9:2<3::aid-a>3.0.co;2-x", "suffix)", "suffix]", "suffix.", "a/b/c"):
            value = "10.1002/" + suffix
            with self.subTest(suffix=suffix):
                self.assertEqual(paperread.parse_doi(value), value)

    def test_url_encoding_and_url_query_are_distinct(self):
        self.assertEqual(paperread.parse_doi("https://doi.org/10.1000/a%3Fb%23c?from=citation#section"),
                         "10.1000/a?b#c")

    def test_invalid_or_unrelated_inputs_are_rejected(self):
        for value in (None, "", "10.12/short", "10.1000/", "10.1000/white space", "10.1000/a\x00b",
                      "../../profile.json", "https://example.org/10.1000/test",
                      "https://doi.org.evil.test/10.1000/test", "https://user@doi.org/10.1000/test",
                      "https://doi.org/10.1000/%FF"):
            with self.subTest(value=value), self.assertRaises(paperread.PaperError):
                paperread.parse_doi(value)


class LookupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        self.client = FakeClient()
        self.store = paperread.Store(self.home, self.client)

    def cached_files(self):
        return list((self.home / "lookup").glob("*.json"))

    def test_openalex_reconstruction_provenance_and_complete_output(self):
        self.client.routes[OPENALEX_URL] = OPENALEX
        result = self.store.lookup(DOI)
        self.assertEqual(result["title"], "A synthetic lookup study")
        self.assertEqual(result["authors"], ["A. Example", "B. Example"])
        self.assertEqual(result["year"], 2026)
        self.assertEqual(result["abstract"], "We compare. We measure states.")
        self.assertEqual(result["doi"], DOI)
        self.assertEqual(result["arxiv_id"], "0000.00026v2")
        self.assertEqual(result["source"], "openalex")
        self.assertEqual(result["abstract_source"], "openalex")
        self.assertEqual(result["sources"], ["openalex"])
        self.assertEqual(result["source_urls"], [OPENALEX_URL])
        self.assertFalse(result["cached"])
        self.assertRegex(result["fetched_at"], r"^\d{4}-\d{2}-\d{2}T.*Z$")
        self.assertEqual(result["warnings"], [])
        self.assertEqual(self.client.calls, [OPENALEX_URL])

    def test_canonical_doi_cache_is_reused_without_a_request(self):
        self.client.routes[OPENALEX_URL] = OPENALEX
        first = self.store.lookup(DOI)
        self.client.routes.clear()
        second = self.store.lookup("https://dx.doi.org/" + quote(DOI.upper(), safe=""))
        self.assertEqual(second, dict(first, cached=True))
        self.assertEqual(self.client.calls, [OPENALEX_URL])
        self.assertEqual(len(self.cached_files()), 1)
        self.assertRegex(self.cached_files()[0].name, r"^[0-9a-f]{64}\.json$")
        self.assertEqual(list((self.home / "lookup").glob("*.lock")), [])

    def test_refresh_replaces_metadata_and_keeps_cache_on_failure(self):
        self.client.routes[OPENALEX_URL] = OPENALEX
        first = self.store.lookup(DOI)
        newer = copy.deepcopy(OPENALEX)
        newer["title"] = "Updated synthetic title"
        self.client.routes[OPENALEX_URL] = newer
        refreshed = self.store.lookup(DOI, refresh=True)
        self.assertEqual(refreshed["title"], newer["title"])
        self.assertFalse(refreshed["cached"])
        self.assertNotEqual(refreshed["title"], first["title"])
        self.client.routes = {OPENALEX_URL: paperread.DownloadError("HTTP 503"),
                              CROSSREF_URL: paperread.DownloadError("HTTP 503")}
        with self.assertRaises(paperread.PaperError):
            self.store.lookup(DOI, refresh=True)
        self.assertEqual(self.store.lookup(DOI)["title"], newer["title"])

    def test_no_openalex_abstract_falls_back_to_crossref_jats(self):
        record = copy.deepcopy(OPENALEX)
        record["abstract_inverted_index"] = None
        self.client.routes = {OPENALEX_URL: record, CROSSREF_URL: CROSSREF}
        result = self.store.lookup(DOI)
        self.assertEqual(result["abstract"], "We test edges & states. Only synthetic evidence.")
        self.assertEqual(result["source"], "openalex")
        self.assertEqual(result["abstract_source"], "crossref")
        self.assertEqual(result["sources"], ["openalex", "crossref"])
        self.assertEqual(result["source_urls"], [OPENALEX_URL, CROSSREF_URL])
        self.assertIn("https://example.org/synthetic-work", result["links"])

    def test_fallback_fills_missing_fields_without_overwriting_openalex(self):
        record = copy.deepcopy(OPENALEX)
        record.update(abstract_inverted_index=None, authorships=[], publication_year=None, primary_location=None)
        self.client.routes = {OPENALEX_URL: record, CROSSREF_URL: CROSSREF}
        result = self.store.lookup(DOI)
        self.assertEqual(result["authors"], ["A. Example", "Example Collaboration"])
        self.assertEqual(result["year"], 2026)
        self.assertEqual(result["title"], record["title"])

    def test_openalex_failure_uses_verified_crossref_metadata(self):
        for failure in (paperread.DownloadError("HTTP 429"), b"not JSON", b"[]", b"{}"):
            with self.subTest(failure=failure):
                self.client.routes = {OPENALEX_URL: failure, CROSSREF_URL: CROSSREF}
                result = self.store.lookup(DOI, refresh=True)
                self.assertEqual(result["source"], "crossref")
                self.assertEqual(result["sources"], ["crossref"])
                self.assertEqual(result["authors"], ["A. Example", "Example Collaboration"])
                self.assertEqual(result["year"], 2026)
                self.assertTrue(result["warnings"])

    def test_openalex_wrong_or_conflicting_doi_cannot_supply_metadata(self):
        for changes in ({"doi": "10.0000/wrong"}, {"doi": None}, {"ids": {"doi": "10.0000/wrong"}}):
            with self.subTest(changes=changes):
                record = copy.deepcopy(OPENALEX)
                record.update(changes, title="Mismatched source title")
                self.client.routes = {OPENALEX_URL: record, CROSSREF_URL: CROSSREF}
                result = self.store.lookup(DOI, refresh=True)
                self.assertEqual(result["source"], "crossref")
                self.assertNotIn("Mismatched", result["title"])

    def test_malformed_service_arrays_are_reported_as_failures(self):
        record = copy.deepcopy(OPENALEX)
        record["authorships"] = 17
        self.client.routes = {OPENALEX_URL: record, CROSSREF_URL: CROSSREF}
        self.assertEqual(self.store.lookup(DOI)["source"], "crossref")
        for field in ("author", "link"):
            with self.subTest(field=field):
                crossref = copy.deepcopy(CROSSREF)
                crossref["message"][field] = 17
                self.client.routes[CROSSREF_URL] = crossref
                with self.assertRaisesRegex(paperread.PaperError, "invalid " + field):
                    self.store.lookup(DOI, refresh=True)

    def test_crossref_wrong_or_missing_doi_cannot_supply_an_abstract(self):
        record = copy.deepcopy(OPENALEX)
        record["abstract_inverted_index"] = None
        for doi in ("10.0000/wrong", None):
            with self.subTest(doi=doi):
                crossref = copy.deepcopy(CROSSREF)
                crossref["message"]["DOI"] = doi
                self.client.routes = {OPENALEX_URL: record, CROSSREF_URL: crossref}
                result = self.store.lookup(DOI, refresh=True)
                self.assertIsNone(result["abstract"])
                self.assertIsNone(result["abstract_source"])
                self.assertEqual(result["sources"], ["openalex"])
                self.assertNotIn("https://example.org/synthetic-work", result["links"])

    def test_successful_metadata_without_abstract_stays_null_and_is_cached(self):
        record, crossref = copy.deepcopy(OPENALEX), copy.deepcopy(CROSSREF)
        record["abstract_inverted_index"] = None
        crossref["message"].pop("abstract")
        self.client.routes = {OPENALEX_URL: record, CROSSREF_URL: crossref}
        result = self.store.lookup(DOI)
        self.assertIsNone(result["abstract"])
        self.assertIsNone(result["abstract_source"])
        self.assertTrue(any("No abstract" in warning for warning in result["warnings"]))
        self.client.routes.clear()
        self.assertTrue(self.store.lookup(DOI)["cached"])
        self.assertEqual(len(self.client.calls), 2)

    def test_both_failures_or_identity_errors_never_cache_success(self):
        for crossref in (paperread.DownloadError("HTTP 503"), b"[]", b"{", {"status": "error", "message": {}}):
            with self.subTest(crossref=crossref):
                self.client.routes = {OPENALEX_URL: paperread.DownloadError("HTTP 401"), CROSSREF_URL: crossref}
                with self.assertRaisesRegex(paperread.PaperError, "no verified metadata"):
                    self.store.lookup(DOI)
                self.assertEqual(self.cached_files(), [])
                self.assertEqual(list((self.home / "lookup").glob("*.lock")), [])

    def test_malformed_inverted_index_falls_back_without_inventing_words(self):
        for index in ([], {"A": [0], "B": [0]}, {"A": [0, 2]}, {"A": [-1]}, {"A": [True]}, {"A": ["0"]}):
            with self.subTest(index=index):
                record = copy.deepcopy(OPENALEX)
                record["abstract_inverted_index"] = index
                self.client.routes = {OPENALEX_URL: record, CROSSREF_URL: CROSSREF}
                result = self.store.lookup(DOI, refresh=True)
                self.assertEqual(result["abstract_source"], "crossref")
                self.assertTrue(any("abstract_inverted_index" in warning for warning in result["warnings"]))

    def test_crossref_xml_namespaces_entities_and_inline_boundaries(self):
        cases = {
            '<abstract xmlns="urn:jats"><p>Non<italic>Hermitian</italic> chains.</p><p>Second paragraph.</p></abstract>':
                "NonHermitian chains. Second paragraph.",
            '<jats:p>x &lt; 1 &amp; y &#8805; 0.</jats:p>': "x < 1 & y ≥ 0.",
            "Plain text abstract.": "Plain text abstract.",
        }
        for abstract, expected in cases.items():
            with self.subTest(abstract=abstract):
                crossref = copy.deepcopy(CROSSREF)
                crossref["message"]["abstract"] = abstract
                self.client.routes = {OPENALEX_URL: paperread.DownloadError("HTTP 404"), CROSSREF_URL: crossref}
                result = self.store.lookup(DOI, refresh=True)
                self.assertEqual(result["abstract"], expected)

    def test_arxiv_abstract_first_publication_year_and_actual_version(self):
        self.client.routes[arxiv_url("0000.00026")] = atom()
        result = self.store.lookup("arXiv:0000.00026")
        self.assertEqual(result["abstract"], "Synthetic states & edges.")
        self.assertEqual(result["year"], 2025)
        self.assertEqual(result["arxiv_id"], "0000.00026v2")
        self.assertEqual(result["doi"], DOI)
        self.assertEqual(result["source"], "arxiv")
        self.assertEqual(result["sources"], ["arxiv"])
        self.assertEqual(result["source_urls"], [arxiv_url("0000.00026")])
        self.assertIn("https://arxiv.org/abs/0000.00026v2", result["links"])
        self.client.routes.clear()
        self.assertTrue(self.store.lookup("https://arxiv.org/abs/0000.00026")["cached"])

    def test_arxiv_requested_version_and_legacy_identifiers(self):
        for identifier in ("0000.00026v2", "cond-mat/0000001v1"):
            with self.subTest(identifier=identifier):
                self.client.routes[arxiv_url(identifier)] = atom(identifier)
                result = self.store.lookup(identifier)
                self.assertEqual(result["arxiv_id"], identifier)
                self.assertEqual(result["source_urls"], [arxiv_url(identifier)])

    def test_arxiv_wrong_id_version_or_invalid_xml_never_caches(self):
        for response in (atom("0000.00099v2"), atom("0000.00026v1"), atom("0000.00026"), b"<bad>",
                         b'<feed xmlns="http://www.w3.org/2005/Atom"/>', atom("error")):
            with self.subTest(response=response[:40]):
                self.client.routes[arxiv_url()] = response
                with self.assertRaises(paperread.PaperError):
                    self.store.lookup("0000.00026v2")
                self.assertEqual(self.cached_files(), [])

    def test_arxiv_absent_summary_and_invalid_optional_doi(self):
        self.client.routes[arxiv_url()] = atom(summary="", doi="not-a-doi")
        result = self.store.lookup("0000.00026v2")
        self.assertIsNone(result["abstract"])
        self.assertIsNone(result["abstract_source"])
        self.assertIsNone(result["doi"])
        self.assertEqual(len(result["warnings"]), 2)

    def test_lookup_never_reads_reader_profile_or_logs(self):
        (self.home / "profile.json").write_text("private reader information", encoding="utf-8")
        self.client.routes[OPENALEX_URL] = OPENALEX
        with mock.patch.object(self.store, "profile", side_effect=AssertionError("profile read")):
            self.store.lookup(DOI)
        self.assertNotIn("private reader", " ".join(self.client.calls))
        self.assertTrue(all("mailto=" not in url and "api_key=" not in url for url in self.client.calls))
        self.assertNotIn("@", paperread.USER_AGENT)

    def test_invalid_input_makes_no_request_or_cache(self):
        for value in (None, "../../profile.json"):
            with self.subTest(value=value), self.assertRaises(paperread.PaperError):
                self.store.lookup(value)
        self.assertEqual(self.client.calls, [])
        self.assertFalse((self.home / "lookup").exists())

    def test_same_lookup_in_two_threads_uses_one_request(self):
        self.client.routes[OPENALEX_URL] = OPENALEX
        barrier = threading.Barrier(2)
        results, failures = [], []

        def run():
            try:
                barrier.wait(timeout=3)
                results.append(self.store.lookup(DOI))
            except Exception as error:
                failures.append(error)

        threads = [threading.Thread(target=run) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(failures, [])
        self.assertEqual(sorted(result["cached"] for result in results), [False, True])
        self.assertEqual(self.client.calls, [OPENALEX_URL])


if __name__ == "__main__":
    unittest.main()
