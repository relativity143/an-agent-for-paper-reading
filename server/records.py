"""Pure, per-paper M2 concept-record validation and transitions.

The service owns locking, persistence, version conflicts and receipt deduplication.
These helpers never inspect a reader profile, a log or the filesystem.
"""

from copy import deepcopy
from datetime import datetime, timezone
import re


_STATUSES = {"known", "explained", "unsure"}
_ITEM_KEYS = {"name", "status", "explained_in", "where", "note", "updated"}
_TIMESTAMP = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"
    r"(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?$"
)


def empty_record():
    return {"version": 0, "concepts": []}


def _shape(value, required, optional=()):
    if not isinstance(value, dict):
        raise ValueError("必须提供对象。")
    keys = set(value)
    if not set(required) <= keys or keys - set(required) - set(optional):
        raise ValueError("字段缺失或含有不支持的字段。")


def _version(value):
    if type(value) is not int or value < 0:
        raise ValueError("version 必须是非负整数。")
    return value


def _name(value):
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 80:
        raise ValueError("概念名字去掉首尾空白后必须为 1 到 80 个字符。")
    return value.strip()


def _status(value):
    if not isinstance(value, str) or value not in _STATUSES:
        raise ValueError("概念状态必须是 known、explained 或 unsure。")
    return value


def _note(value):
    if value is not None and (not isinstance(value, str) or len(value) > 200):
        raise ValueError("note 必须是 null 或不超过 200 个字符的文字。")
    return value


def _nullable_text(value):
    if value is not None and (not isinstance(value, str) or not value.strip()):
        raise ValueError("讲解来源和位置必须是 null 或非空文字。")
    return value


def _timestamp(value):
    if not isinstance(value, str) or not _TIMESTAMP.fullmatch(value):
        raise ValueError("updated 必须是 ISO 时间戳。")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("updated 必须是 ISO 时间戳。") from exc
    # ISO timestamps without an offset are interpreted as UTC. Service writes
    # always include Z; accepting both forms keeps chronological ordering exact.
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def _items(value, *, persisted=False):
    if not isinstance(value, list) or len(value) > 300:
        raise ValueError("concepts 必须是最多 300 条的数组。")
    result, seen = [], set()
    for item in value:
        _shape(item, _ITEM_KEYS if persisted else {"name", "status"},
               () if persisted else {"note"})
        name = _name(item["name"])
        key = name.casefold()
        if key in seen:
            raise ValueError("概念名字不能重复（不区分大小写）。")
        seen.add(key)
        normalized = {"name": name, "status": _status(item["status"]),
                      "note": _note(item.get("note"))}
        if persisted:
            normalized.update(
                explained_in=_nullable_text(item["explained_in"]),
                where=_nullable_text(item["where"]), updated=item["updated"],
            )
            _timestamp(item["updated"])
        result.append(normalized)
    return result


def _sort(items):
    return sorted(items, key=lambda item: _timestamp(item["updated"]), reverse=True)


def validate_record(value):
    """Validate the complete persisted public shape and return a sorted copy."""
    try:
        _shape(value, {"version", "concepts"})
        return {"version": _version(value["version"]),
                "concepts": _sort(_items(value["concepts"], persisted=True))}
    except ValueError as exc:
        raise ValueError("概念记录损坏：" + str(exc)) from exc


def normalize_put(body):
    """Return the supplied version and normalized, editable record fields."""
    _shape(body, {"version", "concepts"})
    return _version(body["version"]), _items(body["concepts"])


def _finish(current, concepts):
    if len(concepts) > 300:
        raise ValueError("概念记录最多 300 条；请先整理已有记录。")
    previous = {item["name"].casefold(): item for item in current["concepts"]}
    changed = {item["name"].casefold(): item for item in concepts} != previous
    return {"version": current["version"] + int(changed), "concepts": _sort(concepts)}


def _update(item, fields, now):
    if any(item.get(key) != value for key, value in fields.items()):
        item.update(fields, updated=now)


def _new(name, status, now, *, source=None, note=None):
    explanation, where = source if source is not None else (None, None)
    return {"name": name, "status": status, "explained_in": explanation,
            "where": where, "note": note, "updated": now}


def replace_record(current, items, now):
    """Replace editable fields, retaining sources for casefold-matched names."""
    current = validate_record(current)
    _timestamp(now)
    items = _items(items)
    previous = {item["name"].casefold(): item for item in current["concepts"]}
    result = []
    for values in items:
        item = previous.get(values["name"].casefold())
        if item is None:
            item = _new(values["name"], values["status"], now, note=values["note"])
        else:
            item = deepcopy(item)
            _update(item, values, now)
        result.append(item)
    return _finish(current, result)


def _expanded(explanation):
    if explanation.get("type") != "point":
        return []
    concepts = explanation.get("concepts")
    if not isinstance(concepts, list):
        raise ValueError("讲解的 concepts 必须是数组。")
    names, seen = [], set()
    for concept in concepts:
        if not isinstance(concept, dict):
            raise ValueError("讲解概念必须是对象。")
        name = _name(concept.get("name"))
        if name.casefold() not in seen:
            names.append(name)
            seen.add(name.casefold())
        if len(names) > 300:
            raise ValueError("一份讲解不能展开超过 300 个概念。")
    return names


def _explanation_id(value):
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 128:
        raise ValueError("explanation 必须是 1 到 128 个字符的讲解编号。")
    return value.strip()


def _source(explanation):
    identifier = _explanation_id(explanation.get("id"))
    where = explanation.get("where")
    if not isinstance(where, dict):
        raise ValueError("讲解缺少有效位置。")
    ids, page = where.get("ids"), where.get("page")
    if (isinstance(ids, list) and ids and page is None
            and all(isinstance(item, str) and item.strip() for item in ids)):
        # explained_at accepts one real HTML ID, never a joined list of IDs.
        return identifier, ids[0]
    if ids == [] and type(page) is int and page > 0:
        return identifier, f"PDF 第 {page} 页"
    raise ValueError("讲解缺少有效的 HTML 或 PDF 位置。")


def mark_displayed(current, explanation, now):
    """Record only concepts expanded in a displayed point explanation."""
    current = validate_record(current)
    _timestamp(now)
    names = _expanded(explanation)
    if not names:
        return current
    source = _source(explanation)
    result = {item["name"].casefold(): deepcopy(item) for item in current["concepts"]}
    for name in names:
        key = name.casefold()
        if key not in result:
            result[key] = _new(name, "explained", now, source=source)
        else:
            _update(result[key], {"explained_in": source[0], "where": source[1]}, now)
    return _finish(current, list(result.values()))


def normalize_feedback(body):
    """Validate feedback, preserving omitted notes so existing notes survive."""
    _shape(body, {"explanation", "kind"}, {"concept", "note"})
    identifier = _explanation_id(body["explanation"])
    kind = body["kind"]
    if not isinstance(kind, str) or kind not in {"unclear", "stuck", "known"}:
        raise ValueError("kind 必须是 unclear、stuck 或 known。")
    result = {"explanation": identifier, "kind": kind}
    if kind == "unclear":
        if "concept" in body:
            raise ValueError("unclear 反馈不能带 concept。")
    else:
        result["concept"] = _name(body.get("concept"))
    if "note" in body:
        result["note"] = _note(body["note"])
    return result


def apply_feedback(current, explanation, body, now):
    """Apply reader status and note changes without replacing existing sources.

    A newly named concept gets the referenced source only when this point really
    expanded it; arbitrary reader-added concepts have null source fields. Unclear
    leaves known entries entirely unchanged. An omitted note keeps the old note;
    explicit null clears it. Receipts/log deduplication belongs to the service.
    """
    current = validate_record(current)
    body = normalize_feedback(body)
    _timestamp(now)
    if explanation.get("id") != body["explanation"]:
        raise ValueError("反馈的讲解编号不匹配。")
    expanded = _expanded(explanation)
    expanded_keys = {name.casefold() for name in expanded}
    names = expanded if body["kind"] == "unclear" else [body["concept"]]
    status = "known" if body["kind"] == "known" else "unsure"
    result = {item["name"].casefold(): deepcopy(item) for item in current["concepts"]}
    for name in names:
        key = name.casefold()
        item = result.get(key)
        if item is not None:
            if body["kind"] == "unclear" and item["status"] == "known":
                continue
            fields = {"status": status}
            if "note" in body:
                fields["note"] = body["note"]
            _update(item, fields, now)
        else:
            source = _source(explanation) if key in expanded_keys else None
            result[key] = _new(name, status, now, source=source, note=body.get("note"))
    return _finish(current, list(result.values()))
