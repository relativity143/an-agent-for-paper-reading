"""Pure validation and replacement of per-paper M3 map positions and notes.

The service owns persistence, locking and HTTP conflict responses. These helpers
never read concepts, prompts, a reader profile, a log or the filesystem.
"""

import re


_LAYOUT_KEYS = {"version", "nodes", "notes"}
_NODE_KEYS = {"name", "x", "y"}
_NOTE_KEYS = {"id", "x", "y", "text"}
_NOTE_ID = re.compile(r"[A-Za-z0-9_-]{1,40}")


def empty_layout():
    """Return a new unsaved map, with no shared mutable containers."""
    return {"version": 0, "nodes": [], "notes": []}


def _shape(value, keys, label):
    if not isinstance(value, dict):
        raise ValueError(f"{label}必须是对象。")
    if set(value) != keys:
        raise ValueError(f"{label}字段缺失或含有不支持的字段。")


def _version(value):
    if type(value) is not int or value < 0:
        raise ValueError("version 必须是非负整数。")
    return value


def _coordinate(value, axis, maximum):
    if type(value) is not int or not 0 <= value <= maximum:
        raise ValueError(f"{axis} 坐标必须是 0 到 {maximum} 的整数。")
    return value


def _coordinates(item):
    return {"x": _coordinate(item["x"], "x", 1440),
            "y": _coordinate(item["y"], "y", 900)}


def _nodes(value):
    if not isinstance(value, list) or len(value) > 400:
        raise ValueError("nodes 必须是最多 400 条的数组。")
    result, seen = [], set()
    for item in value:
        _shape(item, _NODE_KEYS, "地图节点")
        name = item["name"]
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80:
            raise ValueError("概念名字去掉首尾空白后必须为 1 到 80 个字符。")
        name = name.strip()
        key = name.casefold()
        if key in seen:
            raise ValueError("概念名字不能重复（不区分大小写）。")
        seen.add(key)
        result.append({"name": name, **_coordinates(item)})
    return result


def _notes(value):
    if not isinstance(value, list) or len(value) > 60:
        raise ValueError("notes 必须是最多 60 条的数组。")
    result, seen = [], set()
    for item in value:
        _shape(item, _NOTE_KEYS, "地图批注")
        identifier = item["id"]
        if not isinstance(identifier, str) or not _NOTE_ID.fullmatch(identifier):
            raise ValueError("批注 id 必须是 1 到 40 个 ASCII 字母、数字、下划线或短横线。")
        if identifier in seen:
            raise ValueError("批注 id 不能重复。")
        seen.add(identifier)
        text = item["text"]
        if not isinstance(text, str) or not text.strip() or not 1 <= len(text) <= 300:
            raise ValueError("批注 text 必须是 1 到 300 个字符的非空白文字。")
        # Preserve the reader's line breaks and surrounding whitespace.
        result.append({"id": identifier, **_coordinates(item), "text": text})
    return result


def normalize_layout(value):
    """Validate the complete public map shape and return an independent copy."""
    _shape(value, _LAYOUT_KEYS, "地图")
    return {"version": _version(value["version"]),
            "nodes": _nodes(value["nodes"]), "notes": _notes(value["notes"])}


def validate_saved_layout(value):
    """Reject malformed saved data without ever replacing it with an empty map."""
    try:
        return normalize_layout(value)
    except ValueError as exc:
        raise ValueError("地图文件损坏，已保留原文件：" + str(exc)) from exc


def replace_layout(current, incoming):
    """Replace map content, bumping once for changes and ignoring list order.

    A no-op returns the saved order. A changed map retains the incoming order.
    The service checks versions under its lock and maps conflicts to HTTP 409;
    this guard also prevents accidental stale updates by direct callers.
    """
    current = validate_saved_layout(current)
    incoming = normalize_layout(incoming)
    if incoming["version"] != current["version"]:
        raise ValueError("地图版本已变化，请重新读取后再保存。")
    old_nodes = {item["name"].casefold(): item for item in current["nodes"]}
    new_nodes = {item["name"].casefold(): item for item in incoming["nodes"]}
    old_notes = {item["id"]: item for item in current["notes"]}
    new_notes = {item["id"]: item for item in incoming["notes"]}
    if old_nodes == new_nodes and old_notes == new_notes:
        return current
    incoming["version"] = current["version"] + 1
    return incoming
