#!/usr/bin/env python3
"""Paper acquisition, source navigation and private reader records (Python 3.9+).

Only the host agent explains papers. This helper never calls a model API.
"""

import argparse
from collections import deque
from contextlib import contextmanager
from datetime import datetime, timezone
from difflib import SequenceMatcher
import hashlib
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import shutil
import ssl
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlencode, urljoin, urlparse
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET


USER_AGENT = "paper-foundations/0.1 (+https://github.com/relativity143/an-agent-for-paper-reading)"
PARSER_VERSION = 2
LOG_TYPES = ("sentence", "overview", "deep", "field", "recap")
MAX_DOWNLOAD = 64 * 1024 * 1024
ARXIV_RE = re.compile(r"(?P<id>\d{4}\.\d{4,5}|[a-z][a-z.\-]*/\d{7})(?P<version>v[1-9]\d*)?", re.I)
SKIP_CLASSES = {"ltx_bibliography", "ltx_page_navbar", "ltx_page_logo", "ltx_authors", "ltx_dates"}
SECTION_CLASSES = {"ltx_section", "ltx_subsection", "ltx_subsubsection", "ltx_appendix"}
VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}


class PaperError(Exception):
    pass


class DownloadError(PaperError):
    def __init__(self, message, fallback_allowed=False):
        super().__init__(message)
        self.fallback_allowed = fallback_allowed


def paper_identity(value):
    """Compare papers across arXiv URL/version/cache-name forms without fetching."""
    candidate = value.strip()
    old_cache = re.fullmatch(r"([a-z][a-z.\-]*)_(\d{7})(v[1-9]\d*)?", candidate, re.I)
    if old_cache:
        candidate = old_cache[1] + "/" + old_cache[2] + (old_cache[3] or "")
    try:
        identifier, _ = parse_arxiv(candidate)
        return "arxiv:" + identifier
    except PaperError:
        if not re.fullmatch(r"[\w.\-]+", candidate) or candidate in {".", ".."}:
            raise PaperError("Expected an arXiv ID/URL or a cached paper name.")
        return "local:" + candidate


def validate_field_record(record):
    if record.get("where") is not None:
        raise PaperError("A field log must have where=null.")
    kind = record.get("kind")
    if kind not in ("position", "next"):
        raise PaperError("Field kind must be position or next.")
    items = record.get("items")
    if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
        raise PaperError("Field items must be an array of objects.")
    if not items:
        # A field record marks a delivered position or reading list; with nothing verified, log nothing.
        raise PaperError("A field log needs at least one verified item; if none could be verified, do not log it.")
    roles = ("foundational", "predecessor", "contrast") if kind == "position" else ("notes", "foundational", "recent")
    for item in items:
        label = item.get("label")
        if "label" not in item or (label is not None and not isinstance(label, str)):
            raise PaperError("Field items require a text label (next may use null).")
        if kind == "position" and (not isinstance(label, str) or not label.strip()):
            raise PaperError("A position item requires its reference label.")
        if not isinstance(item.get("title"), str) or not item["title"].strip():
            raise PaperError("Field item title must be nonempty text.")
        if item.get("role") not in roles:
            raise PaperError("Invalid role for field kind " + kind + ".")
        if item.get("checked") not in ("abstract", "fulltext"):
            raise PaperError("Field checked must be abstract or fulltext.")
        link = item.get("link")
        try:
            parsed = urlparse(link.strip()) if isinstance(link, str) else None
            valid_link = (parsed is not None and parsed.scheme in ("http", "https")
                          and parsed.hostname and not any(c.isspace() for c in link.strip()))
        except ValueError:
            valid_link = False
        if not valid_link:
            raise PaperError("Field item link must be an HTTP(S) source URL.")
        if kind == "next" or "missing" in item:
            missing = item.get("missing")
            if not isinstance(missing, list) or not all(isinstance(x, str) for x in missing):
                raise PaperError("Field item missing must be an array of strings.")


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def normalize(text):
    return " ".join(text.split())


def parse_arxiv(value):
    value = value.strip()
    if value.lower().startswith("arxiv:"):
        value = value[6:]
    if "://" in value:
        url = urlparse(value)
        if url.scheme not in {"http", "https"} or url.hostname not in {"arxiv.org", "www.arxiv.org", "export.arxiv.org"}:
            raise PaperError("Only arxiv.org abs/pdf/html URLs are supported.")
        match = re.fullmatch(r"/(?:abs|pdf|html)/(.+?)/?", unquote(url.path))
        if not match:
            raise PaperError("Expected an arXiv /abs/, /pdf/ or /html/ URL.")
        value = match[1]
    if value.endswith(".pdf"):
        value = value[:-4]
    match = ARXIV_RE.fullmatch(value)
    if not match:
        raise PaperError("Expected an arXiv ID, arXiv URL, or an existing local PDF.")
    return match["id"].lower(), (match["version"] or "").lower()


def cache_key(arxiv_id, version):
    return arxiv_id.replace("/", "_") + version


class Node:
    def __init__(self, tag, attrs=(), parent=None):
        self.tag = tag
        self.attrs = dict(attrs)
        self.classes = set((self.attrs.get("class") or "").split())
        self.parent = parent
        self.children = []
        self.order = 0
        self.end_order = 0

    def has(self, cls):
        return cls in self.classes


class DocumentParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("root")
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = Node(tag, attrs, self.stack[-1])
        self.stack[-1].children.append(node)
        if tag not in VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def walk(node):
    yield node
    for child in node.children:
        if isinstance(child, Node):
            yield from walk(child)


def ancestor(node, predicate):
    parent = node.parent
    while parent:
        if predicate(parent):
            return parent
        parent = parent.parent
    return None


def skipped(node):
    return node.tag in {"script", "style", "noscript", "nav"} or bool(node.classes & SKIP_CLASSES)


def ignored(node):
    return skipped(node) or ancestor(node, skipped) is not None


def is_section(node):
    return node.tag == "section" and bool(node.classes & SECTION_CLASSES)


def is_figure(node):
    return node.tag == "figure" and bool(node.classes & {"ltx_figure", "ltx_table"})


def section_of(node):
    parent = ancestor(node, is_section)
    return parent.attrs.get("id") if parent else None


def equation_nodes(node):
    """Keep numbered rows of a group; do not duplicate its containing equation."""
    if node.has("ltx_equationgroup"):
        rows = [n for n in walk(node) if n is not node and n.has("ltx_equation") and n.attrs.get("id")]
        return rows or ([node] if node.attrs.get("id") else [])
    if node.has("ltx_equation"):
        if ancestor(node, lambda p: p.has("ltx_equationgroup")):
            return []  # handled by the group
        return [node] if node.attrs.get("id") else []
    return []


def render(node, include_notes=False, display_placeholder=True, figure_placeholder=False):
    if isinstance(node, str):
        return node
    if skipped(node) or (node.has("ltx_note") and not include_notes):
        return ""
    if node.tag == "math":
        latex = node.attrs.get("alttext")
        return "$" + latex + "$" if latex else "⟨math: missing alttext⟩"
    if display_placeholder and (node.has("ltx_equation") or node.has("ltx_equationgroup")):
        equations = equation_nodes(node)
        return " " + " ".join("⟨" + n.attrs["id"] + "⟩" for n in equations) + " "
    if node.tag == "figure":
        return " ⟨%s⟩ " % node.attrs.get("id", "figure-%s" % node.order) if figure_placeholder and is_figure(node) else ""
    if node.has("ltx_note_mark"):
        return ""
    if node.tag == "br":
        return " "
    body = "".join(render(c, include_notes, display_placeholder, figure_placeholder) for c in node.children)
    return body + ("\n" if node.tag in {"p", "div", "section", "h1", "h2", "h3", "h4", "li", "figcaption"} else "")


ABBREVIATIONS = re.compile(r"(?:\be\.g\.|\bi\.e\.|\bet\s+al\.|\bFigs?\.|\bEqs?\.|\bRefs?\.|\bSecs?\.|\bvs\.|\bcf\.|\bapprox\.|\bresp\.)$", re.I)


def split_sentences(text):
    text = normalize(text)
    protected = [(m.start(), m.end()) for m in re.finditer(r"\$[^$]*\$|⟨[^⟩]*⟩", text)]
    cuts = []
    for match in re.finditer(r"[.?!]\s+(?=[A-Z(\[$])", text):
        position = match.start()
        if any(start <= position < end for start, end in protected):
            continue
        if text[position] == "." and ABBREVIATIONS.search(text[:position + 1]):
            continue
        cuts.append(match.end())
    start, result = 0, []
    for end in cuts + [len(text)]:
        sentence = text[start:end].strip()
        if sentence:
            result.append(sentence)
        start = end
    return result


def own_caption_nodes(figure):
    return [n for n in walk(figure) if n.tag == "figcaption"
            and ancestor(n, lambda p: p.tag == "figure") is figure
            and not ancestor(n, lambda p: p.tag == "figcaption")]


def own_caption(figure):
    return normalize(" ".join(render(n) for n in own_caption_nodes(figure)))


def bibliography(nodes):
    entries = []
    for node in nodes:
        if not node.has("ltx_bibitem") or not ancestor(node, lambda p: p.has("ltx_bibliography")):
            continue
        children = list(walk(node))
        tag = next((n for n in children if n.has("ltx_tag_bibitem")), None)
        label = normalize(render(tag)) if tag else ""
        text = normalize(render(node))
        if label and text.startswith(label):
            text = text[len(label):].strip()
        links = []
        for child in children:
            href = (child.attrs.get("href") or "").strip()
            if child.tag != "a" or not href or href.startswith("#"):
                continue
            url = urljoin("https://arxiv.org/", href)
            if urlparse(url).scheme in {"https", "http"} and url not in links:
                links.append(url)
        entries.append({"id": node.attrs.get("id", "bib-%s" % node.order),
                        "label": label, "text": text, "links": links})
    return entries


def parse_html(html):
    parser = DocumentParser()
    parser.feed(html)
    parser.close()
    all_nodes = list(walk(parser.root))
    main = next((n for n in all_nodes if n.has("ltx_page_main")), None)
    if main is None:
        raise PaperError("This page is not LaTeXML paper HTML (missing ltx_page_main).")
    counter = 0

    def number(node):
        nonlocal counter
        node.order = counter
        counter += 1
        for child in node.children:
            if isinstance(child, Node):
                number(child)
        node.end_order = counter - 1

    number(main)
    nodes = [n for n in walk(main) if not ignored(n)]
    title_node = next((n for n in nodes if n.has("ltx_title_document")), None)
    authors = [normalize(render(n)) for n in all_nodes if n.has("ltx_personname")]
    if not authors:
        authors = [n.attrs["content"] for n in all_nodes if n.tag == "meta" and n.attrs.get("name") == "citation_author" and n.attrs.get("content")]
    result = {"title": normalize(render(title_node)) if title_node else "", "authors": list(dict.fromkeys(authors)),
              "sections": [], "paragraphs": [], "equations": [], "figures": [], "parse_warnings": [],
              "references": bibliography(list(walk(main))), "parser_version": PARSER_VERSION}
    seen = set()
    paragraph_ids = {}
    for node in nodes:
        if node.tag == "math" and not node.attrs.get("alttext"):
            result["parse_warnings"].append("Math without alttext at source order %s; read the original formula." % node.order)
        if is_section(node):
            heading = next((n for n in walk(node) if n.tag in {"h2", "h3", "h4", "h5"} and section_of(n) == node.attrs.get("id")), None)
            level = 3 if node.has("ltx_subsubsection") else 2 if node.has("ltx_subsection") else 1
            result["sections"].append({"id": node.attrs.get("id", "section-%s" % node.order), "title": normalize(render(heading)) if heading else "", "level": level, "order": node.order, "end_order": node.end_order})
        is_abstract = node.has("ltx_abstract")
        if (is_abstract or node.has("ltx_para")) and not ancestor(node, lambda p: p.has("ltx_abstract") or p.has("ltx_para") or p.tag == "figure" or p.has("ltx_note")):
            identifier = "abstract" if is_abstract else node.attrs.get("id", "para-%s" % node.order)
            if identifier in seen:
                raise PaperError("Duplicate paragraph ID: " + identifier)
            seen.add(identifier)
            paragraph_ids[node] = identifier
            children = [c for c in node.children if not (isinstance(c, Node) and c.tag in {"h2", "h3", "h4"})]
            text = normalize("".join(render(c) for c in children))
            notes = [normalize(render(n, include_notes=True, display_placeholder=False)) for n in walk(node) if n.has("ltx_note") and not ancestor(n, lambda p: p.has("ltx_note"))]
            result["paragraphs"].append({"id": identifier, "section": section_of(node), "text": text,
                                         "sentences": split_sentences(text), "equations": [], "notes": notes,
                                         "order": node.order, "end_order": node.end_order})
            reading_text = normalize("".join(render(c, figure_placeholder=True) for c in children))
            if reading_text != text:
                result["paragraphs"][-1]["reading_text"] = reading_text
        if ancestor(node, lambda p: p.has("ltx_note")):
            continue
        for eq in equation_nodes(node):
            maths = [n.attrs["alttext"] for n in walk(eq) if n.tag == "math" and n.attrs.get("alttext")]
            parent = ancestor(eq, lambda p: p in paragraph_ids)
            paragraph = paragraph_ids.get(parent)
            result["equations"].append({"id": eq.attrs["id"], "latex": r" \\ ".join(maths), "paragraph": paragraph, "section": section_of(eq), "order": eq.order})
        if is_figure(node) and not ancestor(node, is_figure):
            subcaptions = [{"id": n.attrs.get("id", "figure-%s" % n.order),
                            "caption": own_caption(n), "order": (own_caption_nodes(n) or [n])[0].order}
                           for n in walk(node) if n is not node and n.tag == "figure"]
            result["figures"].append({"id": node.attrs.get("id", "figure-%s" % node.order),
                                      "caption": own_caption(node), "subcaptions": sorted(subcaptions, key=lambda c: c["order"]),
                                      "section": section_of(node), "order": node.order})
    for paragraph in result["paragraphs"]:
        paragraph["equations"] = [eq["id"] for eq in result["equations"] if eq["paragraph"] == paragraph["id"]]
    return result


def read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as error:
        raise PaperError("Cannot read %s: %s" % (path, error)) from error


def atomic_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=str(path.parent), prefix="." + path.name, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
        os.replace(str(temporary), str(path))
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


def write_json(path, value):
    atomic_write(path, (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8"))


@contextmanager
def file_lock(path, timeout=10):
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    while True:
        try:
            descriptor = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            break
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise PaperError("Busy lock %s. If a previous process crashed, confirm it has stopped before removing this lock." % path)
            time.sleep(0.1)
    try:
        with os.fdopen(descriptor, "w") as stream:
            stream.write(str(os.getpid()))
        yield
    finally:
        path.unlink()


class HttpClient:
    def __init__(self, home):
        self.home = home

    def get(self, url):
        # Share the delay across CLI invocations and both host agents.
        with file_lock(self.home / ".request.lock", timeout=45):
            stamp = self.home / ".last_request"
            try:
                last = float(stamp.read_text()) if stamp.exists() else 0
            except ValueError:
                last = 0
            time.sleep(min(3, max(0, 3 - (time.time() - last))))
            try:
                request = Request(url, headers={"User-Agent": USER_AGENT})
                context = ssl.create_default_context()
                # python.org macOS installs can lack their bundled CA file.
                # Use the OS CA bundle only when no roots or explicit override exist.
                if (sys.platform == "darwin" and not context.get_ca_certs()
                        and not os.environ.get("SSL_CERT_FILE") and not os.environ.get("SSL_CERT_DIR")
                        and Path("/etc/ssl/cert.pem").is_file()):
                    context.load_verify_locations(cafile="/etc/ssl/cert.pem")
                with urlopen(request, timeout=30, context=context) as response:
                    data = response.read(MAX_DOWNLOAD + 1)
                    if len(data) > MAX_DOWNLOAD:
                        raise DownloadError("Download exceeds 64 MiB: " + url)
                    return data, response.geturl()
            except HTTPError as error:
                raise DownloadError("HTTP %s for %s" % (error.code, url), error.code in {404, 406, 410, 422, 500, 502, 504}) from error
            except (URLError, TimeoutError) as error:
                raise DownloadError("Could not download %s: %s" % (url, error)) from error
            finally:
                atomic_write(stamp, str(time.time()).encode("ascii"))


def api_metadata(client, identifier, version=""):
    data, _ = client.get("https://export.arxiv.org/api/query?" + urlencode({"id_list": identifier + version}))
    try:
        root = ET.fromstring(data)
        ns = {"a": "http://www.w3.org/2005/Atom"}
        entry = root.find("a:entry", ns)
        if entry is None:
            raise PaperError("arXiv returned no entry for " + identifier)
        found_id, found_version = parse_arxiv(entry.findtext("a:id", default="", namespaces=ns))
        if found_id != identifier or not found_version or (version and version != found_version):
            raise PaperError("arXiv metadata does not identify the requested paper version.")
        return {"arxiv_id": found_id, "version": found_version,
                "title": normalize(entry.findtext("a:title", default="", namespaces=ns)),
                "authors": [normalize(a.findtext("a:name", default="", namespaces=ns)) for a in entry.findall("a:author", ns)]}
    except ET.ParseError as error:
        raise PaperError("arXiv metadata was not valid Atom XML.") from error


def parse_doi(value):
    """Normalize an identifier, preserving punctuation that belongs to its suffix."""
    if not isinstance(value, str):
        raise PaperError("Expected a DOI or doi.org URL.")
    value = value.strip()
    # Strip surrounding citation delimiters only, never trailing DOI punctuation.
    for opening, closing in (("(", ")"), ("[", "]"), ("<", ">"), ('"', '"'), ("'", "'")):
        if value.startswith(opening) and value.endswith(closing):
            value = value[1:-1].strip()
            break
    if value.lower().startswith("doi:"):
        value = value[4:].strip()
    if "://" in value:
        try:
            parsed = urlparse(value)
            if (parsed.scheme not in {"http", "https"}
                    or parsed.hostname not in {"doi.org", "dx.doi.org", "www.doi.org"}
                    or parsed.username or parsed.password):
                raise PaperError("Only doi.org or dx.doi.org DOI URLs are supported.")
            value = parsed.path.lstrip("/")
        except ValueError as error:
            raise PaperError("Invalid DOI URL.") from error
    try:
        value = unquote(value, errors="strict")
    except UnicodeDecodeError as error:
        raise PaperError("Invalid DOI URL encoding.") from error
    if not re.fullmatch(r"10\.\d{4,9}/[^\s\x00-\x1f\x7f]+", value, re.I):
        raise PaperError("Expected a DOI beginning with 10.<registrant>/<suffix>.")
    return value.lower()


def lookup_json(data, service):
    try:
        result = json.loads(data)
    except (ValueError, UnicodeDecodeError) as error:
        raise PaperError(service + " returned invalid JSON.") from error
    if not isinstance(result, dict):
        raise PaperError(service + " returned no metadata object.")
    return result


def lookup_list(record, field, service):
    value = record.get(field)
    if value is None:
        return []
    if not isinstance(value, list):
        raise PaperError(service + " returned an invalid " + field + " list.")
    return value


def lookup_year(value):
    if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 9999:
        return value
    return None


def lookup_links(values):
    result = []
    for value in values:
        if not isinstance(value, str):
            continue
        try:
            parsed = urlparse(value)
            valid = parsed.scheme in {"https", "http"} and parsed.hostname and not parsed.username
        except ValueError:
            valid = False
        if valid and value not in result:
            result.append(value)
    return result


def lookup_arxiv_link(links):
    for link in links:
        try:
            identifier, version = parse_arxiv(link)
            return identifier + version
        except PaperError:
            pass
    return None


def inverted_abstract(index):
    if index is None or index == {}:
        return None
    if not isinstance(index, dict):
        raise PaperError("OpenAlex abstract_inverted_index is not an object.")
    words = {}
    for word, positions in index.items():
        if not isinstance(word, str) or not word or not isinstance(positions, list) or not positions:
            raise PaperError("OpenAlex abstract_inverted_index has invalid tokens.")
        for position in positions:
            if type(position) is not int or position < 0 or position in words:
                raise PaperError("OpenAlex abstract_inverted_index has invalid or duplicate positions.")
            words[position] = word
    if not words or max(words) + 1 != len(words):
        raise PaperError("OpenAlex abstract_inverted_index has missing positions.")
    return normalize(" ".join(words[position] for position in sorted(words))) or None


class MetadataTextParser(HTMLParser):
    """Read JATS fragments, including undeclared namespace prefixes and entities."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag.split(":")[-1] in {"p", "title", "sec", "abstract", "br", "div", "li"}:
            self.parts.append(" ")

    def handle_endtag(self, tag):
        self.handle_starttag(tag, ())

    def handle_data(self, data):
        self.parts.append(data)


def metadata_text(value):
    if not isinstance(value, str):
        return None
    parser = MetadataTextParser()
    parser.feed(value)
    parser.close()
    return normalize("".join(parser.parts)) or None


def lookup_openalex(data, doi):
    record = lookup_json(data, "OpenAlex")
    ids = record.get("ids") if isinstance(record.get("ids"), dict) else {}
    identifiers = [value for value in (record.get("doi"), ids.get("doi")) if value]
    if not identifiers or any(parse_doi(value) != doi for value in identifiers):
        raise PaperError("OpenAlex metadata does not identify the requested DOI.")
    title = record.get("title") or record.get("display_name")
    if not isinstance(title, str) or not normalize(title):
        raise PaperError("OpenAlex returned no title for the requested DOI.")
    authors = []
    for authorship in lookup_list(record, "authorships", "OpenAlex"):
        author = authorship.get("author") if isinstance(authorship, dict) else None
        name = author.get("display_name") if isinstance(author, dict) else None
        if isinstance(name, str) and normalize(name):
            authors.append(normalize(name))
    location = record.get("primary_location")
    location = location if isinstance(location, dict) else {}
    links = lookup_links(["https://doi.org/" + quote(doi, safe="/"), record.get("id"),
                          location.get("landing_page_url"), location.get("pdf_url")])
    warnings = []
    try:
        abstract = inverted_abstract(record.get("abstract_inverted_index"))
    except PaperError as error:
        abstract = None
        warnings.append(str(error))
    return {"title": normalize(title), "authors": authors, "year": lookup_year(record.get("publication_year")),
            "abstract": abstract, "doi": doi, "arxiv_id": lookup_arxiv_link(links), "links": links,
            "source": "openalex", "abstract_source": "openalex" if abstract else None, "warnings": warnings}


def lookup_crossref(data, doi):
    payload = lookup_json(data, "Crossref")
    record = payload.get("message")
    if payload.get("status") != "ok" or not isinstance(record, dict):
        raise PaperError("Crossref returned no successful metadata record.")
    if parse_doi(record.get("DOI")) != doi:
        raise PaperError("Crossref metadata does not identify the requested DOI.")
    titles = record.get("title")
    title = metadata_text(titles[0]) if isinstance(titles, list) and titles else None
    if not title:
        raise PaperError("Crossref returned no title for the requested DOI.")
    authors = []
    for author in lookup_list(record, "author", "Crossref"):
        if not isinstance(author, dict):
            continue
        name = author.get("name") or " ".join(part for part in (author.get("given"), author.get("family"))
                                               if isinstance(part, str))
        if isinstance(name, str) and normalize(name):
            authors.append(normalize(name))
    year = None
    for field in ("published", "published-print", "published-online", "issued"):
        date = record.get(field)
        parts = date.get("date-parts") if isinstance(date, dict) else None
        if isinstance(parts, list) and parts and isinstance(parts[0], list) and parts[0]:
            year = lookup_year(parts[0][0])
            if year is not None:
                break
    links = lookup_links(["https://doi.org/" + quote(doi, safe="/"), record.get("URL")] +
                         [item.get("URL") for item in lookup_list(record, "link", "Crossref") if isinstance(item, dict)])
    abstract = metadata_text(record.get("abstract"))
    return {"title": title, "authors": authors, "year": year, "abstract": abstract, "doi": doi,
            "arxiv_id": lookup_arxiv_link(links), "links": links, "source": "crossref",
            "abstract_source": "crossref" if abstract else None, "warnings": []}


def lookup_arxiv(data, identifier, version):
    ns = {"a": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
    try:
        root = ET.fromstring(data)
    except ET.ParseError as error:
        raise PaperError("arXiv metadata was not valid Atom XML.") from error
    entries = root.findall("a:entry", ns)
    if len(entries) != 1:
        raise PaperError("arXiv returned no unique entry for the requested paper.")
    entry = entries[0]
    found_id, found_version = parse_arxiv(entry.findtext("a:id", default="", namespaces=ns))
    if found_id != identifier or not found_version or (version and found_version != version):
        raise PaperError("arXiv metadata does not identify the requested paper version.")
    title = normalize(entry.findtext("a:title", default="", namespaces=ns))
    if not title:
        raise PaperError("arXiv returned no title for the requested paper.")
    summary = entry.find("a:summary", ns)
    abstract = (normalize("".join(summary.itertext())) or None) if summary is not None else None
    published = entry.findtext("a:published", default="", namespaces=ns)
    year = lookup_year(int(published[:4])) if re.match(r"^\d{4}-", published) else None
    warnings, doi = [], None
    doi_value = entry.findtext("arxiv:doi", default="", namespaces=ns)
    if doi_value:
        try:
            doi = parse_doi(doi_value)
        except PaperError:
            warnings.append("arXiv returned an invalid DOI; it was omitted.")
    links = lookup_links(["https://arxiv.org/abs/" + found_id + found_version] +
                         [link.get("href") for link in entry.findall("a:link", ns)] +
                         (["https://doi.org/" + quote(doi, safe="/")] if doi else []))
    return {"title": title, "authors": [normalize(author.findtext("a:name", default="", namespaces=ns))
                                        for author in entry.findall("a:author", ns)
                                        if normalize(author.findtext("a:name", default="", namespaces=ns))],
            "year": year, "abstract": abstract, "doi": doi, "arxiv_id": found_id + found_version,
            "links": links, "source": "arxiv", "abstract_source": "arxiv" if abstract else None,
            "warnings": warnings}


class Store:
    def __init__(self, home=None, client=None):
        self.home = Path(home or os.environ.get("PAPERREAD_HOME", "~/.paperread")).expanduser().resolve()
        self.papers = self.home / "papers"
        self.client = client if client is not None else HttpClient(self.home)

    def latest_cache(self, identifier):
        prefix = cache_key(identifier, "")
        matches = []
        if self.papers.is_dir():
            for path in self.papers.glob(prefix + "v*"):
                suffix = path.name[len(prefix):]
                if re.fullmatch(r"v[1-9]\d*", suffix) and (path / "paper.json").is_file():
                    matches.append((int(suffix[1:]), path))
        return max(matches)[1] if matches else None

    def resolve(self, paper):
        try:
            identifier, version = parse_arxiv(paper)
        except PaperError:
            if not re.fullmatch(r"[\w.\-]+", paper) or paper in {".", ".."}:
                raise PaperError("Invalid cache name. Run fetch first and use its paper field.")
            path = self.papers / paper
        else:
            path = self.papers / cache_key(identifier, version) if version else self.latest_cache(identifier)
        if path is None or not (path / "paper.json").is_file():
            raise PaperError("Paper not cached. Run fetch first: " + paper)
        return path

    def load(self, paper, require_html=False):
        path = self.resolve(paper)
        with file_lock(path / ".fetch.lock", timeout=45):
            data = self.upgrade_cache(path)
        if require_html and data.get("source") in {"pdf_only", "local_pdf"}:
            raise PaperError("No structured text for this PDF. The host agent must read %s directly and use page locations such as p3." % (path / "paper.pdf"))
        return path, data

    def upgrade_cache(self, path):
        """Caller holds .fetch.lock; rebuild derived data without fetching or touching logs."""
        data = read_json(path / "paper.json")
        if data.get("source") not in {"arxiv_html", "ar5iv"} or data.get("parser_version", 0) >= PARSER_VERSION:
            return data
        source = path / "source.html"
        if not source.is_file():
            raise PaperError("Old HTML cache has no source.html. Run fetch %s --refresh to rebuild it." % path.name)
        raw = source.read_bytes()
        if data.get("source_sha256") and hashlib.sha256(raw).hexdigest() != data["source_sha256"]:
            raise PaperError("Cached source.html hash does not match paper.json. Run fetch %s --refresh." % path.name)
        parsed = parse_html(raw.decode("utf-8"))
        # The API can supply better metadata than the HTML. Keep it and source provenance.
        parsed["title"] = data.get("title") or parsed["title"]
        parsed["authors"] = data.get("authors") or parsed["authors"]
        data.update(parsed)
        write_json(path / "paper.json", data)
        return data

    def summary(self, path, cached):
        data = read_json(path / "paper.json")
        return {"paper": path.name, "arxiv_id": data.get("arxiv_id"), "version": data.get("version"),
                "title": data["title"], "source": data["source"], "source_url": data.get("source_url"),
                "paragraph_count": len(data["paragraphs"]), "equation_count": len(data["equations"]),
                "figure_count": len(data["figures"]), "reference_count": len(data.get("references", [])), "cached": cached,
                "cache_dir": str(path), "paper_json": str(path / "paper.json"),
                "source_path": str(path / ("paper.pdf" if data["source"] in {"pdf_only", "local_pdf"} else "source.html")),
                "outline_path": str(path / "outline.md"), "outline_exists": (path / "outline.md").is_file(),
                "warnings": data.get("fetch_warnings", []) + data.get("parse_warnings", [])}

    def fetch(self, value, refresh=False):
        local = Path(value).expanduser()
        if local.is_file():
            return self.import_pdf(local, refresh)
        identifier, version = parse_arxiv(value)
        existing = self.papers / cache_key(identifier, version) if version else self.latest_cache(identifier)
        if not refresh and existing and (existing / "paper.json").is_file():
            with file_lock(existing / ".fetch.lock", timeout=45):
                self.upgrade_cache(existing)
                return self.summary(existing, True)
        metadata = {"arxiv_id": identifier, "version": version, "title": "", "authors": []}
        if not version:
            metadata = api_metadata(self.client, identifier)
            version = metadata["version"]
        path = self.papers / cache_key(identifier, version)
        with file_lock(path / ".fetch.lock", timeout=45):
            if not refresh and (path / "paper.json").is_file():
                self.upgrade_cache(path)
                return self.summary(path, True)
            warnings = []
            for source, url in [("arxiv_html", "https://arxiv.org/html/" + identifier + version),
                                ("ar5iv", "https://ar5iv.labs.arxiv.org/html/" + identifier + version)]:
                try:
                    data, final_url = self.client.get(url)
                except DownloadError as error:
                    if not error.fallback_allowed:
                        raise
                    warnings.append(str(error))
                    continue
                final = urlparse(final_url)
                # A redirect to an abstract or another version is not usable paper HTML.
                if final.hostname not in {"arxiv.org", "www.arxiv.org", "ar5iv.labs.arxiv.org", "ar5iv.org"} or unquote(final.path).rstrip("/") != "/html/" + identifier + version:
                    warnings.append("Skipped HTML redirect with unverified version: " + final_url)
                    continue
                try:
                    parsed = parse_html(data.decode("utf-8"))
                except (UnicodeError, PaperError) as error:
                    warnings.append(str(error))
                    continue
                if not parsed["paragraphs"]:
                    warnings.append("No usable paragraphs in " + final_url)
                    continue
                parsed.update({"arxiv_id": identifier, "version": version, "source": source,
                               "source_url": final_url, "fetched_at": now(), "fetch_warnings": warnings,
                               "source_sha256": hashlib.sha256(data).hexdigest()})
                parsed["title"] = metadata["title"] or parsed["title"] or identifier + version
                parsed["authors"] = metadata["authors"] or parsed["authors"]
                atomic_write(path / "source.html", data)
                write_json(path / "paper.json", parsed)
                return self.summary(path, False)
            pdf_url = "https://arxiv.org/pdf/" + identifier + version
            data, final_url = self.client.get(pdf_url)
            final = urlparse(final_url)
            pdf_path = unquote(final.path).removesuffix(".pdf")
            if final.hostname not in {"arxiv.org", "www.arxiv.org", "export.arxiv.org"} or pdf_path != "/pdf/" + identifier + version:
                raise PaperError("PDF redirect does not identify the requested paper version: " + final_url)
            if not data.lstrip().startswith(b"%PDF-"):
                raise PaperError("The PDF endpoint did not return a PDF; nothing was marked as cached.")
            atomic_write(path / "paper.pdf", data)
            metadata.update({"title": metadata["title"] or identifier + version, "source": "pdf_only",
                             "source_url": final_url, "fetched_at": now(), "sections": [], "paragraphs": [],
                             "equations": [], "figures": [], "references": [], "fetch_warnings": warnings,
                             "source_sha256": hashlib.sha256(data).hexdigest()})
            write_json(path / "paper.json", metadata)
            return self.summary(path, False)

    def import_pdf(self, path, refresh=False):
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            if not stream.read(1024).lstrip().startswith(b"%PDF-"):
                raise PaperError("The local file is not a PDF.")
            stream.seek(0)
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        stem = re.sub(r"[^\w.\-]+", "-", path.stem).strip(".-")[:100] or "paper"
        target = self.papers / (stem + "-" + digest.hexdigest()[:8])
        with file_lock(target / ".fetch.lock"):
            if not refresh and (target / "paper.json").is_file():
                return self.summary(target, True)
            with tempfile.NamedTemporaryFile(dir=str(target), delete=False) as stream:
                temporary = Path(stream.name)
            try:
                shutil.copyfile(str(path), str(temporary))
                os.replace(str(temporary), str(target / "paper.pdf"))
            finally:
                if temporary.exists():
                    temporary.unlink()
            write_json(target / "paper.json", {"arxiv_id": None, "version": None, "title": path.stem, "authors": [],
                       "source": "local_pdf", "source_url": path.resolve().as_uri(), "fetched_at": now(),
                       "source_sha256": digest.hexdigest(), "sections": [], "paragraphs": [], "equations": [], "figures": [], "references": []})
        return self.summary(target, False)

    def lookup(self, value, refresh=False):
        if not isinstance(value, str):
            raise PaperError("Expected a DOI or arXiv identifier.")
        try:
            identifier, version = parse_arxiv(value)
            canonical, kind = "arxiv:" + identifier + version, "arxiv"
        except PaperError:
            identifier, version = parse_doi(value), ""
            canonical, kind = "doi:" + identifier, "doi"
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        path = self.home / "lookup" / (digest + ".json")
        with file_lock(path.with_suffix(".lock"), timeout=90):
            if path.exists() and not refresh:
                cached = read_json(path)
                if (not isinstance(cached, dict) or cached.get("input") != canonical
                        or not isinstance(cached.get("result"), dict)):
                    raise PaperError("Invalid lookup cache; use lookup --refresh to rebuild it.")
                return dict(cached["result"], cached=True)
            sources, source_urls, warnings = [], [], []
            if kind == "arxiv":
                url = "https://export.arxiv.org/api/query?" + urlencode({"id_list": identifier + version})
                data, _ = self.client.get(url)
                result = lookup_arxiv(data, identifier, version)
                sources.append("arxiv")
                source_urls.append(url)
            else:
                result = None
                services = (("openalex", "https://api.openalex.org/works/https://doi.org/" +
                             quote(identifier, safe="/"), lookup_openalex),
                            ("crossref", "https://api.crossref.org/works/" + quote(identifier, safe=""), lookup_crossref))
                for service, url, parser in services:
                    source_urls.append(url)
                    try:
                        data, _ = self.client.get(url)
                        metadata = parser(data, identifier)
                    except PaperError as error:
                        warnings.append(service + ": " + str(error))
                        continue
                    sources.append(service)
                    if result is None:
                        result = metadata
                    else:
                        for field in ("abstract", "abstract_source", "authors", "year", "arxiv_id"):
                            if not result.get(field) and metadata.get(field):
                                result[field] = metadata[field]
                        result["links"] = lookup_links(result["links"] + metadata["links"])
                        result["warnings"].extend(metadata["warnings"])
                    if result["abstract"]:
                        break
                if result is None:
                    raise PaperError("Lookup failed; no verified metadata was cached. " + " ".join(warnings))
            result["warnings"].extend(warnings)
            if result["abstract"] is None:
                result["warnings"].append("No abstract was available; open a source link before assessing the work.")
            result.update(sources=sources, source_urls=source_urls, fetched_at=now(), cached=False)
            write_json(path, {"input": canonical, "result": result})
            return result

    def profile(self):
        path = self.home / "profile.json"
        data = read_json(path) if path.exists() else {"background": "", "areas": {}, "concepts": {}}
        if not isinstance(data, dict) or not isinstance(data.get("areas"), dict) or not isinstance(data.get("concepts"), dict):
            raise PaperError("Invalid profile.json; existing reader records were left unchanged.")
        if not isinstance(data.get("background"), str):
            raise PaperError("Invalid profile background; expected text.")
        for area in data["areas"].values():
            if not isinstance(area, dict) or area.get("level") not in {"studied", "heard", "none"}:
                raise PaperError("Invalid profile area record; expected studied/heard/none.")
        for concept in data["concepts"].values():
            if not isinstance(concept, dict) or concept.get("status") not in {"known", "unsure", "explained"}:
                raise PaperError("Invalid profile concept record; expected known/unsure/explained.")
        return data

    def update_profile(self, action, args):
        with file_lock(self.home / ".profile.lock"):
            profile = self.profile()
            date = now()[:10]
            if action == "set-background":
                profile["background"] = args.text
            elif action == "set-area":
                profile["areas"][args.area] = {"level": args.level, "updated": date}
            else:
                if args.status == "known" and not (args.source and args.source.strip()):
                    raise PaperError("known requires --source with the reader's explicit confirmation. Explained does not mean understood.")
                old = profile["concepts"].get(args.concept, {})
                if old.get("status") in {"known", "unsure"} and args.status == "explained":
                    return profile
                record = {"status": args.status, "updated": date}
                if args.source:
                    record["source"] = args.source
                if args.paper:
                    record["paper"] = self.resolve(args.paper).name
                if args.where:
                    record["where"] = args.where
                profile["concepts"][args.concept] = record
            write_json(self.home / "profile.json", profile)
            return profile

    def profile_concepts(self, status=None, exclude_paper=None, paper=None):
        if status is not None and status not in ("known", "explained", "unsure"):
            raise PaperError("Concept status must be known, explained or unsure.")
        if paper is not None and exclude_paper is not None:
            raise PaperError("--paper and --exclude-paper cannot be used together.")
        included = paper_identity(paper) if paper is not None else None
        excluded = paper_identity(exclude_paper) if exclude_paper is not None else None
        records = []
        for concept, record in self.profile()["concepts"].items():
            if status is not None and record["status"] != status:
                continue
            origin = record.get("paper")
            if included and (not origin or paper_identity(origin) != included):
                continue
            if excluded and origin and paper_identity(origin) == excluded:
                continue
            entry = dict(record, concept=concept)
            entry.setdefault("paper", None)
            entry.setdefault("where", None)
            records.append(entry)
        return records

    def log_add(self, paper, record):
        path = self.resolve(paper) / "log.jsonl"
        if not isinstance(record, dict):
            raise PaperError("A log entry must be a JSON object.")
        record_type = record.get("type", "sentence")
        if record_type not in LOG_TYPES:
            raise PaperError("Log type must be one of: " + ", ".join(LOG_TYPES) + ".")
        if record_type in ("overview", "recap") and record.get("where") is not None:
            raise PaperError("A %s log must have where=null." % record_type)
        if record_type == "deep":
            if type(record.get("segment")) is not int or record["segment"] < 1:
                raise PaperError("A deep log requires a positive integer segment (starting at 1).")
        elif "segment" in record:
            raise PaperError("Only deep logs may include segment.")
        if record_type == "field":
            validate_field_record(record)
        for key in ("gaps", "explained", "unexpanded_terms", "uses", "covered", "terms"):
            if key in record and (not isinstance(record[key], list) or not all(isinstance(x, str) for x in record[key])):
                raise PaperError("Log field %s must be an array of strings." % key)
        entry = {"time": now(), "type": "sentence", "where": None, "quote": "", "gaps": [], "explained": [],
                 "unexpanded_terms": [], "next": None, "feedback": None}
        if record_type == "deep":
            entry["uses"] = []
        if record_type == "recap":
            entry.update(covered=[], terms=[])
        entry.update(record)
        encoded = json.dumps(entry, ensure_ascii=False, allow_nan=False) + "\n"
        with file_lock(path.parent / ".log.lock"):
            with path.open("a", encoding="utf-8") as stream:
                stream.write(encoded)
        return entry

    def log_show(self, paper, last=10, record_type=None):
        path = self.resolve(paper) / "log.jsonl"
        if last < 0:
            raise PaperError("--last must be nonnegative; 0 reads all records.")
        if record_type is not None and record_type not in LOG_TYPES:
            raise PaperError("Log type must be one of: " + ", ".join(LOG_TYPES) + ".")
        if not path.exists():
            return []
        try:
            records = deque(maxlen=last or None)
            with file_lock(path.parent / ".log.lock"):
                with path.open(encoding="utf-8") as stream:
                    for line in stream:
                        if not line.strip():
                            continue
                        record = json.loads(line)
                        if not isinstance(record, dict):
                            raise ValueError("Expected log objects")
                        record.setdefault("type", "sentence")
                        # Filter before taking the tail, so intervening types do not hide progress.
                        if record_type is None or record["type"] == record_type:
                            records.append(record)
            return list(records)
        except ValueError as error:
            raise PaperError("Invalid JSON in log.jsonl; records were left unchanged.") from error


def match_score(query, text):
    def score(a, b):
        a, b = normalize(a).casefold(), normalize(b).casefold()
        if not a or not b:
            return 0.0
        if a in b:
            return 1.0
        # Compare a copied fragment with its best approximate window in the sentence.
        matcher = SequenceMatcher(None, a, b, autojunk=False)
        windows = [b[max(0, block.b - block.a):max(0, block.b - block.a) + len(a)]
                   for block in matcher.get_matching_blocks()]
        return max([matcher.ratio()] + [SequenceMatcher(None, a, window, autojunk=False).ratio()
                                       for window in windows])
    remove_math = lambda s: re.sub(r"\$[^$]*\$|⟨[^⟩]*⟩", " ", s)
    return max(score(query, text), score(remove_math(query), remove_math(text)))


def locate(data, query):
    if not query.strip():
        raise PaperError("The sentence fragment must not be empty.")
    candidates = []
    for paragraph in data["paragraphs"]:
        for index, sentence in enumerate(paragraph["sentences"], 1):
            candidates.append({"id": paragraph["id"] + ".s" + str(index), "paragraph": paragraph["id"],
                               "section": paragraph["section"], "text": sentence,
                               "score": round(match_score(query, sentence), 4)})
    return sorted(candidates, key=lambda item: -item["score"])[:3]


def context(data, identifier, before=1, after=1):
    if before < 0 or after < 0:
        raise PaperError("Context counts must be nonnegative.")
    paragraphs = data["paragraphs"]
    for index, paragraph in enumerate(paragraphs):
        if paragraph["id"] == identifier:
            return paragraphs[max(0, index - before):index + after + 1]
    raise PaperError("Unknown paragraph: " + identifier)


def find(data, query, after=None):
    if not query.strip():
        raise PaperError("The search term must not be empty.")
    boundary = -1
    if after:
        para = next((p for p in data["paragraphs"] if p["id"] == after), None)
        if para is None:
            raise PaperError("Unknown --after paragraph: " + after)
        boundary = para["end_order"]
    results = []
    for collection, kind, field in [("paragraphs", "paragraph", "text"), ("equations", "equation", "latex"), ("figures", "figure", "caption")]:
        for item in data[collection]:
            if item["order"] <= boundary:
                continue
            match = re.search(re.escape(query), item[field], flags=re.I)
            if match:
                results.append({"id": item["id"], "type": kind, "section": item.get("section"),
                                "snippet": item[field][max(0, match.start() - 60):match.end() + 60],
                                "order": item["order"]})
    for figure in data["figures"]:
        for caption in figure.get("subcaptions", []):
            match = re.search(re.escape(query), caption["caption"], flags=re.I)
            if caption["order"] > boundary and match:
                results.append({"id": caption["id"], "parent": figure["id"], "type": "figure",
                                "section": figure.get("section"), "order": caption["order"],
                                "snippet": caption["caption"][max(0, match.start() - 60):match.end() + 60]})
    return sorted(results, key=lambda item: item["order"])


def paper_text(data, section=None):
    lower, upper = -1, float("inf")
    if section is not None:
        selected = next((s for s in data["sections"] if s["id"] == section), None)
        if selected is None:
            raise PaperError("Unknown section: " + section)
        lower, upper = selected["order"], selected["end_order"]
    events = [(item["order"], kind, item)
              for kind in ("sections", "paragraphs", "equations", "figures")
              for item in data[kind] if lower <= item["order"] <= upper]
    equations = {e["id"]: e for e in data["equations"]}
    figures = {f["id"]: f for f in data["figures"]}
    embedded = set()

    def equation_text(item):
        return "[%s] $$%s$$" % (item["id"], item["latex"])

    def figure_text(item):
        lines = ["[%s] %s" % (item["id"], item["caption"])]
        lines.extend("[%s] %s" % (c["id"], c["caption"]) for c in item.get("subcaptions", []))
        return "\n".join(lines)

    def expand(match):
        identifier = match[1]
        if identifier in equations:
            content = equation_text(equations[identifier])
        elif identifier in figures:
            content = figure_text(figures[identifier])
        else:
            return match[0]
        embedded.add(identifier)
        return "\n" + content + "\n"

    lines = [data["title"]] if section is None else []
    for _, kind, item in sorted(events, key=lambda event: event[0]):
        if item["id"] in embedded:
            continue
        if kind == "paragraphs":
            # Expand display math/figures where they occur, before the paragraph's trailing text.
            body = re.sub(r"⟨([^⟩]+)⟩", expand, item.get("reading_text", item["text"]))
            lines.append("[%s] %s" % (item["id"], body))
            lines.extend("[%s note %s] %s" % (item["id"], i, note)
                         for i, note in enumerate(item.get("notes", []), 1))
        elif kind == "sections":
            lines.append("[%s] %s" % (item["id"], item["title"]))
        elif kind == "equations":
            lines.append(equation_text(item))
        else:
            lines.append(figure_text(item))
    return "\n\n".join(lines)


def reference_identifiers(item):
    doi, arxiv_id = None, None

    def inspect(value):
        nonlocal doi, arxiv_id
        if doi is None:
            try:
                doi = parse_doi(value)
            except PaperError:
                pass
        if arxiv_id is None:
            try:
                identifier, version = parse_arxiv(value)
                arxiv_id = identifier + version
            except PaperError:
                pass

    # Exact href values take precedence over punctuation-sensitive prose extraction.
    for link in item.get("links", []):
        inspect(link)
    text = item.get("text", "")
    for match in re.finditer(r"(?:https?://(?:www\.)?arxiv\.org/(?:abs|html|pdf)/|10\.\d{4,9}/)[^\s\"']+", text, re.I):
        value = match[0].rstrip(".,;:")
        for closing, opening in ((")", "("), ("]", "["), ("}", "{"), (">", "<")):
            while value.endswith(closing) and value.count(closing) > value.count(opening):
                value = value[:-1]
        inspect(value)
    match = re.search(r"\barxiv\s*:?\s*(\d{4}\.\d{4,5}(?:v[1-9]\d*)?|[a-z][a-z.\-]*/\d{7}(?:v[1-9]\d*)?)\b", text, re.I)
    if match:
        inspect(match[1])
    if arxiv_id is None and doi and doi.startswith("10.48550/arxiv."):
        inspect(doi[len("10.48550/arxiv."):])
    return {"doi": doi, "arxiv_id": arxiv_id}


def reference(data, identifier):
    refs = data.get("references", [])
    exact = [r for r in refs if r["id"] == identifier]
    label = identifier.strip().strip("[]()")
    matches = exact or [r for r in refs if r["label"].strip().strip("[]()") == label]
    if len(matches) != 1:
        raise PaperError(("Ambiguous reference: " if matches else "Unknown reference: ") + identifier)
    return dict(matches[0], **reference_identifiers(matches[0]))


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        raise PaperError(message)


def command_parser():
    parser = JsonArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    fetch = commands.add_parser("fetch", help="Cache an arXiv paper or a local PDF")
    fetch.add_argument("input")
    fetch.add_argument("--refresh", action="store_true")
    lookup = commands.add_parser("lookup", help="Look up and cache DOI or arXiv metadata")
    lookup.add_argument("input")
    lookup.add_argument("--refresh", action="store_true")
    for name in ["outline-path", "glossary-path", "locate", "context", "find", "text", "ref"]:
        command = commands.add_parser(name)
        command.add_argument("paper")
        if name in {"locate", "find"}:
            command.add_argument("query")
        if name == "find":
            command.add_argument("--after")
        if name == "text":
            command.add_argument("--section", help="Include this section and its subsections")
        if name == "ref":
            command.add_argument("identifier", help="Reference number, displayed label, or HTML ID")
        if name == "context":
            command.add_argument("paragraph")
            command.add_argument("--before", type=int, default=1)
            command.add_argument("--after", type=int, default=1)
    profile = commands.add_parser("profile").add_subparsers(dest="action", required=True)
    profile.add_parser("show")
    concepts = profile.add_parser("concepts", help="List concept records without the rest of the profile")
    concepts.add_argument("--status", choices=["known", "explained", "unsure"])
    paper_filter = concepts.add_mutually_exclusive_group()
    paper_filter.add_argument("--exclude-paper", help="Exclude this paper, including other arXiv versions")
    paper_filter.add_argument("--paper", help="Only include this paper, including other arXiv versions")
    background = profile.add_parser("set-background")
    background.add_argument("text")
    area = profile.add_parser("set-area")
    area.add_argument("area")
    area.add_argument("level", choices=["studied", "heard", "none"])
    mark = profile.add_parser("mark")
    mark.add_argument("concept")
    mark.add_argument("status", choices=["known", "unsure", "explained"])
    mark.add_argument("--paper")
    mark.add_argument("--where")
    mark.add_argument("--source", help="Reader's own words; required for known")
    log = commands.add_parser("log").add_subparsers(dest="action", required=True)
    add = log.add_parser("add")
    add.add_argument("paper")
    add.add_argument("--json", required=True, help="JSON object, or - to read JSON from stdin")
    show = log.add_parser("show")
    show.add_argument("paper")
    show.add_argument("--last", type=int, default=10, help="Last N records; 0 reads all records")
    show.add_argument("--type", dest="record_type", choices=LOG_TYPES,
                      help="Filter log type before selecting the last N records")
    return parser


def run(args, store):
    if args.command == "fetch":
        return store.fetch(args.input, args.refresh)
    if args.command == "lookup":
        return store.lookup(args.input, args.refresh)
    if args.command == "profile":
        if args.action == "concepts":
            return store.profile_concepts(args.status, args.exclude_paper, args.paper)
        return store.profile() if args.action == "show" else store.update_profile(args.action, args)
    if args.command == "log":
        if args.action == "show":
            return store.log_show(args.paper, args.last, args.record_type)
        return store.log_add(args.paper, json.loads(sys.stdin.read() if args.json == "-" else args.json))
    artifact_names = {"outline-path": "outline.md", "glossary-path": "glossary.md"}
    path, data = store.load(args.paper, require_html=args.command not in artifact_names)
    if args.command in artifact_names:
        artifact = path / artifact_names[args.command]
        return {"path": str(artifact), "exists": artifact.is_file()}
    if args.command == "locate":
        return locate(data, args.query)
    if args.command == "context":
        return context(data, args.paragraph, args.before, args.after)
    if args.command == "text":
        return paper_text(data, args.section)
    if args.command == "ref":
        return reference(data, args.identifier)
    return find(data, args.query, args.after)


def main(argv=None):
    try:
        args = command_parser().parse_args(argv)
        result = run(args, Store())
        print(result if args.command == "text" else json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0
    except (PaperError, OSError, ValueError) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    sys.exit(main())
