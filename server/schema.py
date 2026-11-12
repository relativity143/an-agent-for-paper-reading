"""Structural checks for M1 explanations, shared by all model backends.

Passing these checks establishes a renderable shape and, when supplied, local
reference membership. It does not establish the scientific truth of the text.
The model schema omits ``id`` and ``paper``: the service owns those fields.
"""

from copy import deepcopy
import re
from urllib.parse import urlsplit


class ValidationError(ValueError):
    """An explanation violates the frontend's structural contract."""


_STATUSES = ["new", "known", "explained", "unsure"]
_HTML_ID = re.compile(r"^[^\s\x00-\x1f\x7f<>\"'`#]+$")
_PDF_LOCATION = re.compile(
    r"^(?:(?:PDF\s*)?第\s*([1-9][0-9]*)\s*页|"
    r"(?:PDF\s+)?(?:page|p\.)\s*([1-9][0-9]*))"
    r"(?:\s|[：:，,；;（(]|$)", re.IGNORECASE
)


def _text(nullable=False, allow_empty=False):
    return {"type": ["string", "null"] if nullable else "string",
            "minLength": 0 if allow_empty else 1}


def _array(items, minimum=0):
    return {"type": "array", "items": items, "minItems": minimum}


def _object(properties, optional=()):
    return {
        "type": "object", "properties": properties,
        "required": [name for name in properties if name not in optional],
        "additionalProperties": False,
    }


def _location(label=False):
    properties = {
        "ids": _array(_text()),
        "page": {"type": ["integer", "null"], "minimum": 1},
        "quote": _text(nullable=True),
    }
    if label:
        properties["label"] = _text()
    return _object(properties)


def _located_text():
    return _object({"text": _text(), "where": _location()})


def _next_point():
    result = _object({"point": _text(), "where": _location()})
    result["type"] = ["object", "null"]
    return result


def explanation_schema(kind: str) -> dict:
    """Return the model-output JSON schema for ``overview`` or ``point``.

    HTML/PDF mode and membership in the actual paper cannot be encoded by this
    mode-independent schema; ``validate_explanation`` performs those checks.
    ``next`` may be null at the end, and progress counters may be unknown.
    """
    if kind == "point":
        result = _object({
            "type": {"type": "string", "const": "point"},
            "point": _text(),
            "section": _text(nullable=True),
            "where": _location(),
            "intro": _text(),
            "assumes": _array(_object({
                "concept": _text(),
                "status": {"type": "string", "enum": list(_STATUSES)},
                "explained_at": _text(nullable=True),
                "note": _text(),
            })),
            "concepts": _array(_object({
                "name": _text(),
                "in_this_paper": _text(),
                "in_this_paper_where": _array(_text(), minimum=1),
                "itself": _text(),
                "sources": _array(_object({"title": _text(), "url": _text()})),
            })),
            "together": _text(),
            "back_to_text": _text(),
            "unexpanded": _array(_text()),
            "progress": _object({
                "step": {"type": ["integer", "null"], "minimum": 1},
                "steps_total": {"type": ["integer", "null"], "minimum": 1},
                "text": _text(),
                "next": _next_point(),
            }),
            "highlights": _array(_location(label=True)),
        })
    elif kind == "overview":
        next_location = _located_text()
        next_location["type"] = ["object", "null"]
        result = _object({
            "type": {"type": "string", "const": "overview"},
            "title": _text(),
            "question": _located_text(),
            "model": _object({
                "summary": _text(),
                "items": _array(_object({
                    "label": _text(), "text": _text(), "where": _location(),
                })),
            }),
            "conclusions": _located_text(),
            "foundations": _array(_object({"name": _text(), "note": _text()})),
            "previously": _array(_object({
                "name": _text(), "paper": _text(),
                "status": {"type": "string", "enum": ["known", "explained", "unsure"]},
            })),
            "route": _object({
                "steps": _array(_object({
                    "step": {"type": "integer", "minimum": 1},
                    "name": _text(), "range": _text(),
                })),
                "skim": _text(nullable=True, allow_empty=True),
            }),
            "next": next_location,
            "highlights": _array(_location(label=True)),
        })
    else:
        raise ValidationError("type: expected 'overview' or 'point'")
    return result


def _fail(path, message):
    raise ValidationError(f"{path}: {message}")


def _check_shape(value, schema, path):
    """Validate the small JSON Schema vocabulary emitted above, without deps."""
    allowed = schema["type"]
    allowed = allowed if isinstance(allowed, list) else [allowed]
    checks = {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "integer": type(value) is int,
        "null": value is None,
    }
    if not any(checks[kind] for kind in allowed):
        _fail(path, "expected " + " or ".join(allowed))
    if value is None:
        return
    if "const" in schema and value != schema["const"]:
        _fail(path, "expected " + repr(schema["const"]))
    if "enum" in schema and value not in schema["enum"]:
        _fail(path, "expected one of " + ", ".join(schema["enum"]))
    if isinstance(value, str):
        if schema.get("minLength", 0) and not value.strip():
            _fail(path, "must not be empty or whitespace")
    elif type(value) is int:
        if value < schema.get("minimum", value):
            _fail(path, "must be at least " + str(schema["minimum"]))
    elif isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            _fail(path, "requires at least " + str(schema["minItems"]) + " item(s)")
        for index, item in enumerate(value):
            _check_shape(item, schema["items"], f"{path}[{index}]")
    elif isinstance(value, dict):
        for name in schema["required"]:
            if name not in value:
                _fail(path + "." + name, "required field is missing")
        for name, item in value.items():
            if name not in schema["properties"]:
                _fail(path, "unexpected field (only schema-defined fields are allowed)")
            _check_shape(item, schema["properties"][name], path + "." + name)


def _reference(value, path, valid_ids, pdf):
    if pdf:
        if not _PDF_LOCATION.match(value):
            _fail(path, "PDF reference must name a positive page, e.g. 'PDF 第 2 页'")
    else:
        if not _HTML_ID.fullmatch(value):
            _fail(path, "expected an HTML element ID without spaces or markup")
        if valid_ids is not None and value not in valid_ids:
            _fail(path, "unknown HTML element ID")


def _source_url(value, path):
    if re.search(r"[\s\x00-\x1f\x7f<>\"'`\\]", value):
        _fail(path, "source URL contains unsafe characters")
    try:
        parsed = urlsplit(value)
        valid = (parsed.scheme.lower() in {"http", "https"}
                 and bool(parsed.hostname) and not parsed.username
                 and not parsed.password and "@" not in parsed.netloc)
        parsed.port  # Force validation of malformed or out-of-range ports.
    except ValueError:
        valid = False
    if not valid:
        _fail(path, "source URL must be absolute HTTP(S), without credentials")


def _check_references(value, path, valid_ids, pdf):
    if isinstance(value, list):
        for index, item in enumerate(value):
            _check_references(item, f"{path}[{index}]", valid_ids, pdf)
        return
    if not isinstance(value, dict):
        return
    if all(key in value for key in ("ids", "page", "quote")):
        if pdf:
            if value["ids"]:
                _fail(path + ".ids", "PDF locations must not invent HTML IDs")
            if value["page"] is None:
                _fail(path + ".page", "PDF locations require a positive page")
            if value["quote"] is None:
                _fail(path + ".quote", "PDF locations require an original-text quote")
        else:
            if not value["ids"]:
                _fail(path + ".ids", "HTML locations require at least one ID")
            if value["page"] is not None:
                _fail(path + ".page", "HTML locations use IDs, not PDF page numbers")
            for index, identifier in enumerate(value["ids"]):
                _reference(identifier, f"{path}.ids[{index}]", valid_ids, pdf=False)
    for name, item in value.items():
        item_path = path + "." + name
        if name == "in_this_paper_where":
            for index, reference in enumerate(item):
                _reference(reference, f"{item_path}[{index}]", valid_ids, pdf)
        elif name == "explained_at" and item is not None:
            _reference(item, item_path, valid_ids, pdf)
        elif name == "sources":
            for index, source in enumerate(item):
                _source_url(source["url"], f"{item_path}[{index}].url")
        _check_references(item, item_path, valid_ids, pdf)


def validate_explanation(value, *, paper=None, valid_ids=None, pdf=False) -> dict:
    """Check an explanation and return an independent copy without mutating it.

    ``id`` and ``paper`` may be present on service-owned completed objects; they
    are optional here so adapters can also validate a model response before the
    service assigns them. When ``paper`` is supplied, any present paper field
    must match. Pass ``valid_ids`` to reject references absent from cached HTML.
    In PDF mode each location uses an empty IDs list, a positive page and quote;
    concept reference strings use explicit page labels instead of HTML IDs.
    The display-only assumes status ``new`` is accepted, never persisted here.
    """
    if not isinstance(value, dict):
        _fail("explanation", "expected object")
    schema = explanation_schema(value.get("type"))
    # Provider strict mode requires every property to appear in required, with
    # null representing an omitted value. Accept legacy/frontend omissions too.
    if value.get("type") == "point":
        schema["required"].remove("section")
    else:
        schema["properties"]["route"]["required"].remove("skim")
    schema["properties"].update({"id": _text(), "paper": _text()})
    _check_shape(value, schema, "explanation")
    if paper is not None and "paper" in value and value["paper"] != paper:
        _fail("explanation.paper", "does not match the requested paper")
    identifiers = None if valid_ids is None else set(valid_ids)
    _check_references(value, "explanation", identifiers, pdf)
    if value["type"] == "point":
        progress = value["progress"]
        if (progress["step"] is not None and progress["steps_total"] is not None
                and progress["step"] > progress["steps_total"]):
            _fail("explanation.progress.step", "must not exceed steps_total")
    return deepcopy(value)
