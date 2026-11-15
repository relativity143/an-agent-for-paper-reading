"""Read-only summaries of cached papers; a broken entry cannot hide others."""
from datetime import datetime, timezone
import json
from pathlib import Path
import re


def _read(path):
    if path.is_symlink():
        raise ValueError("A cache record cannot be a symlink")
    with path.open(encoding="utf-8") as stream:
        return json.load(stream)


def _time(value):
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("Invalid reading date")
    date = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if date.tzinfo is None:
        raise ValueError("Reading date has no timezone")
    return date.astimezone(timezone.utc)


def _previous_read(path, displayed):
    log = path / "log.jsonl"
    if not log.exists():
        return None
    if log.is_symlink():
        raise ValueError("Invalid log path")
    latest = None
    with log.open(encoding="utf-8") as stream:
        for line in stream:
            if not line.strip():
                continue
            entry = json.loads(line)
            if not isinstance(entry, dict):
                raise ValueError("Invalid reading log")
            if entry.get("explanation_id") in displayed:
                date = _time(entry.get("time"))
                if date is not None and (latest is None or date > latest):
                    latest = date
    return latest


def paper_catalog(store):
    result = []
    root = Path(store.papers)
    if not root.is_dir() or root.is_symlink():
        return {"papers": []}
    for path in root.iterdir():
        if path.is_symlink() or not path.is_dir() or not re.fullmatch(r"[\w.\-]+", path.name):
            continue
        try:
            data = _read(path / "paper.json")
            if (not isinstance(data, dict) or not isinstance(data.get("title"), str)
                    or not isinstance(data.get("authors"), list)
                    or not all(isinstance(author, str) for author in data["authors"])
                    or data.get("source") not in {"arxiv_html", "ar5iv", "pdf_only", "local_pdf"}):
                continue
            state_path = path / "agent-state.json"
            state = _read(state_path) if state_path.exists() else {"explanations": [], "displayed": []}
            if (not isinstance(state, dict) or not isinstance(state.get("explanations"), list)
                    or not all(isinstance(item, dict) and isinstance(item.get("id"), str)
                               for item in state["explanations"])
                    or not isinstance(state.get("displayed"), list)
                    or not all(isinstance(item, str) for item in state["displayed"])):
                continue
            last_read = _time(state.get("last_read")) or _previous_read(path, set(state["displayed"]))
            result.append({"paper": path.name, "title": data["title"], "authors": data["authors"],
                           "source": "arxiv_html" if data["source"] == "ar5iv" else data["source"],
                           "explanations": len(state["explanations"]),
                           "last_read": last_read.isoformat() if last_read else None})
        except (OSError, ValueError, TypeError, OverflowError, RecursionError):
            continue
    result.sort(key=lambda item: (item["last_read"] is None,
                                 -_time(item["last_read"]).timestamp() if item["last_read"] else 0,
                                 item["paper"]))
    return {"papers": result}
