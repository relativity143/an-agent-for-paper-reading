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
from urllib.parse import unquote, urlencode, urljoin, urlparse
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET


USER_AGENT = "paper-foundations/0.1 (+https://github.com/relativity143/an-agent-for-paper-reading)"
PARSER_VERSION = 2
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

    def log_add(self, paper, record):
        path = self.resolve(paper) / "log.jsonl"
        if not isinstance(record, dict):
            raise PaperError("A log entry must be a JSON object.")
        if record.get("type", "sentence") not in ("sentence", "overview"):
            raise PaperError("Log type must be sentence or overview.")
        if record.get("type") == "overview" and record.get("where") is not None:
            raise PaperError("An overview log must have where=null.")
        for key in ("gaps", "explained", "unexpanded_terms"):
            if key in record and (not isinstance(record[key], list) or not all(isinstance(x, str) for x in record[key])):
                raise PaperError("Log field %s must be an array of strings." % key)
        entry = {"time": now(), "type": "sentence", "where": None, "quote": "", "gaps": [], "explained": [],
                 "unexpanded_terms": [], "next": None, "feedback": None}
        entry.update(record)
        encoded = json.dumps(entry, ensure_ascii=False, allow_nan=False) + "\n"
        with file_lock(path.parent / ".log.lock"):
            with path.open("a", encoding="utf-8") as stream:
                stream.write(encoded)
        return entry

    def log_show(self, paper, last=10):
        path = self.resolve(paper) / "log.jsonl"
        if last < 0:
            raise PaperError("--last must be nonnegative; 0 reads all records.")
        if not path.exists():
            return []
        with file_lock(path.parent / ".log.lock"):
            with path.open(encoding="utf-8") as stream:
                lines = deque((line for line in stream if line.strip()), maxlen=last or None)
        try:
            records = [json.loads(line) for line in lines]
            if not all(isinstance(record, dict) for record in records):
                raise ValueError("Expected log objects")
            for record in records:
                record.setdefault("type", "sentence")
            return records
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


def reference(data, identifier):
    refs = data.get("references", [])
    exact = [r for r in refs if r["id"] == identifier]
    label = identifier.strip().strip("[]()")
    matches = exact or [r for r in refs if r["label"].strip().strip("[]()") == label]
    if len(matches) != 1:
        raise PaperError(("Ambiguous reference: " if matches else "Unknown reference: ") + identifier)
    return matches[0]


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        raise PaperError(message)


def command_parser():
    parser = JsonArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    fetch = commands.add_parser("fetch", help="Cache an arXiv paper or a local PDF")
    fetch.add_argument("input")
    fetch.add_argument("--refresh", action="store_true")
    for name in ["outline-path", "locate", "context", "find", "text", "ref"]:
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
    return parser


def run(args, store):
    if args.command == "fetch":
        return store.fetch(args.input, args.refresh)
    if args.command == "profile":
        return store.profile() if args.action == "show" else store.update_profile(args.action, args)
    if args.command == "log":
        if args.action == "show":
            return store.log_show(args.paper, args.last)
        return store.log_add(args.paper, json.loads(sys.stdin.read() if args.json == "-" else args.json))
    path, data = store.load(args.paper, require_html=args.command != "outline-path")
    if args.command == "outline-path":
        return {"path": str(path / "outline.md"), "exists": (path / "outline.md").is_file()}
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
