"""A secure, local-first browser workspace for Hermes.

The server is intentionally loopback-only and bearer-token protected.  It
provides durable notes (including editing and safe Markdown preview), a small
inbox, recent session metadata, a bounded read-only terminal, and system
information.  It does not expose arbitrary file reads or arbitrary command
execution.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import platform
import re
import secrets
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import parse_qs, urlparse

__all__ = [
    "WorkspaceStore",
    "main",
    "make_server",
    "render_markdown",
    "run",
]

MAX_BODY_BYTES = 1_000_000
MAX_NOTES = 5_000
MAX_TITLE = 240
MAX_CONTENT = 500_000
MAX_TAGS = 24
NOTE_ID_RE = re.compile(r"^[a-f0-9]{24}$")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _clean_text(value: Any, field: str, limit: int, *, required: bool = True) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    value = value.replace("\x00", "").strip()
    if required and not value:
        raise ValueError(f"{field} is required")
    if len(value) > limit:
        raise ValueError(f"{field} exceeds {limit} characters")
    return value


def _clean_tags(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("tags must be an array")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ValueError("each tag must be a string")
        tag = item.strip().lower()
        if not tag or len(tag) > 48:
            continue
        if tag not in result:
            result.append(tag)
    if len(result) > MAX_TAGS:
        raise ValueError(f"too many tags (maximum {MAX_TAGS})")
    return result


class WorkspaceStore:
    """SQLite-backed durable workspace state."""

    def __init__(self, root: str | os.PathLike[str]):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "workspace.sqlite3"
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.db_path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock, self._db:
            self._db.executescript(
                """
                CREATE TABLE IF NOT EXISTS notes (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    content TEXT NOT NULL DEFAULT '',
                    tags TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    deleted_at TEXT,
                    pinned INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS notes_updated_idx ON notes(updated_at DESC);
                CREATE TABLE IF NOT EXISTS inbox (
                    id TEXT PRIMARY KEY,
                    text TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS inbox_created_idx ON inbox(created_at DESC);
                CREATE TABLE IF NOT EXISTS chat_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    backend TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS chat_messages_id_idx ON chat_messages(id);
                CREATE TABLE IF NOT EXISTS note_versions (
                    note_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    content TEXT NOT NULL DEFAULT '',
                    tags TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (note_id, version)
                );
                """
            )
            # Migrate databases created before soft delete existed.
            columns = {row["name"] for row in self._db.execute("PRAGMA table_info(notes)")}
            if "deleted_at" not in columns:
                self._db.execute("ALTER TABLE notes ADD COLUMN deleted_at TEXT")
            if "pinned" not in columns:
                self._db.execute("ALTER TABLE notes ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0")

    def close(self) -> None:
        with self._lock:
            self._db.close()

    @staticmethod
    def _note(row: sqlite3.Row, *, include_content: bool = True) -> dict[str, Any]:
        note: dict[str, Any] = {
            "id": row["id"],
            "title": row["title"],
            "tags": json.loads(row["tags"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "version": row["version"],
            "deleted_at": row["deleted_at"] if "deleted_at" in row.keys() else None,
            "pinned": bool(row["pinned"]) if "pinned" in row.keys() else False,
        }
        if include_content:
            note["content"] = row["content"]
            note["html"] = render_markdown(row["content"])
        return note

    def _snapshot(self, note: dict[str, Any]) -> None:
        """Store an immutable copy of a note version."""
        with self._lock, self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO note_versions"
                "(note_id,version,title,content,tags,created_at) VALUES(?,?,?,?,?,?)",
                (
                    note["id"],
                    int(note["version"]),
                    note["title"],
                    note.get("content", ""),
                    json.dumps(note.get("tags", [])),
                    _now(),
                ),
            )

    def list_versions(self, note_id: str) -> list[dict[str, Any]]:
        """Every stored version of a note, newest first."""
        if not isinstance(note_id, str) or not NOTE_ID_RE.fullmatch(note_id):
            raise KeyError(note_id)
        rows = self._db.execute(
            "SELECT note_id,version,title,content,tags,created_at FROM note_versions"
            " WHERE note_id=? ORDER BY version DESC",
            (note_id,),
        ).fetchall()
        return [
            {
                "note_id": row["note_id"],
                "version": row["version"],
                "title": row["title"],
                "content": row["content"],
                "tags": json.loads(row["tags"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def restore_version(self, note_id: str, version: int) -> dict[str, Any]:
        """Restore a stored version as a new version of the note."""
        if not isinstance(note_id, str) or not NOTE_ID_RE.fullmatch(note_id):
            raise KeyError(note_id)
        row = self._db.execute(
            "SELECT title,content,tags FROM note_versions WHERE note_id=? AND version=?",
            (note_id, int(version)),
        ).fetchone()
        if row is None:
            raise KeyError(note_id)
        current = self.get_note(note_id, include_deleted=False)
        return self.update_note(
            note_id,
            row["title"],
            row["content"],
            json.loads(row["tags"]),
            int(current["version"]),
        )

    def create_note(
        self, title: str, content: str = "", tags: list[str] | None = None
    ) -> dict[str, Any]:
        title = _clean_text(title, "title", MAX_TITLE)
        content = _clean_text(content, "content", MAX_CONTENT, required=False)
        tags = _clean_tags(tags or [])
        note_id = secrets.token_hex(12)
        timestamp = _now()
        with self._lock, self._db:
            count = self._db.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
            if count >= MAX_NOTES:
                raise ValueError(f"note limit reached ({MAX_NOTES})")
            self._db.execute(
                "INSERT INTO notes(id,title,content,tags,created_at,updated_at,version) VALUES(?,?,?,?,?,?,1)",
                (note_id, title, content, json.dumps(tags), timestamp, timestamp),
            )
        note = self.get_note(note_id)
        self._snapshot(note)
        return note

    def get_note(self, note_id: str, *, include_deleted: bool = True) -> dict[str, Any]:
        if not isinstance(note_id, str) or not NOTE_ID_RE.fullmatch(note_id):
            raise KeyError(note_id)
        row = self._db.execute("SELECT * FROM notes WHERE id=?", (note_id,)).fetchone()
        if row is None:
            raise KeyError(note_id)
        if not include_deleted and row["deleted_at"]:
            raise KeyError(note_id)
        return self._note(row)

    def list_notes(
        self,
        query: str = "",
        tag: str = "",
        *,
        sort: str = "updated",
        include_content: bool = False,
        include_deleted: bool = False,
    ) -> list[dict[str, Any]]:
        query = str(query or "").strip()
        tag = str(tag or "").strip().lower()
        if include_deleted:
            rows = self._db.execute(
                "SELECT * FROM notes WHERE deleted_at IS NOT NULL ORDER BY deleted_at DESC, id ASC"
            ).fetchall()
        else:
            rows = self._db.execute(
                "SELECT * FROM notes WHERE deleted_at IS NULL ORDER BY updated_at DESC, id ASC"
            ).fetchall()
        result: list[dict[str, Any]] = []
        needle = query.casefold()
        for row in rows:
            note = self._note(row, include_content=True)
            if tag and tag not in note["tags"]:
                continue
            if needle:
                haystack = f"{note['title']}\n{note['content']}".casefold()
                if needle not in haystack:
                    continue
            if not include_content:
                note.pop("content", None)
                note.pop("html", None)
            result.append(note)
        sort = str(sort or "updated").strip().lower()
        if sort == "title":
            result.sort(key=lambda n: (n["title"].casefold(), n["id"]))
        elif sort == "created":
            result.sort(key=lambda n: (n["created_at"], n["id"]), reverse=True)
        else:
            result.sort(key=lambda n: (n["updated_at"], n["id"]), reverse=True)
        # Pinned notes always lead the list, keeping their relative order.
        result.sort(key=lambda n: not n.get("pinned"))
        return result

    def tag_summary(self) -> list[dict[str, Any]]:
        """Tag names with live counts for the filter control."""
        counts: dict[str, int] = {}
        for row in self._db.execute(
            "SELECT tags FROM notes WHERE deleted_at IS NULL"
        ).fetchall():
            for tag in json.loads(row["tags"]):
                counts[tag] = counts.get(tag, 0) + 1
        return [
            {"tag": tag, "count": count}
            for tag, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        ]

    def export_all(self) -> dict[str, Any]:
        """A complete, portable snapshot of the workspace."""
        return {
            "format": "hermes-workspace-v1",
            "exported_at": _now(),
            "notes": self.list_notes(include_content=True),
            "inbox": self.list_inbox(),
            "chat": self.list_chat_messages(),
        }

    def import_notes(self, notes: Any) -> int:
        """Import notes from an export payload. New ids are always assigned."""
        if not isinstance(notes, list):
            raise ValueError("notes must be an array")
        imported = 0
        for item in notes:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "").strip()
            if not title:
                continue
            content = item.get("content")
            if not isinstance(content, str):
                content = ""
            tags = item.get("tags")
            if not isinstance(tags, list):
                tags = []
            self.create_note(title[:MAX_TITLE], content[:MAX_CONTENT], tags)
            imported += 1
        return imported

    def update_note(
        self,
        note_id: str,
        title: str,
        content: str,
        tags: list[str] | None = None,
        version: int = 1,
    ) -> dict[str, Any]:
        title = _clean_text(title, "title", MAX_TITLE)
        content = _clean_text(content, "content", MAX_CONTENT, required=False)
        tags = _clean_tags(tags or [])
        if not isinstance(version, int) or isinstance(version, bool):
            raise ValueError("version must be an integer")
        timestamp = _now()
        with self._lock, self._db:
            row = self._db.execute(
                "SELECT version, deleted_at FROM notes WHERE id=?", (note_id,)
            ).fetchone()
            if row is None or not NOTE_ID_RE.fullmatch(str(note_id)):
                raise KeyError(note_id)
            if row["deleted_at"]:
                raise KeyError(note_id)
            if row["version"] != version:
                raise RuntimeError("version conflict")
            self._db.execute(
                "UPDATE notes SET title=?, content=?, tags=?, updated_at=?, version=version+1 WHERE id=?",
                (title, content, json.dumps(tags), timestamp, note_id),
            )
        note = self.get_note(note_id)
        self._snapshot(note)
        return note

    def delete_note(self, note_id: str, version: int) -> dict[str, Any]:
        """Move a note to trash. The row survives until it is purged."""
        if not isinstance(note_id, str) or not NOTE_ID_RE.fullmatch(note_id):
            raise KeyError(note_id)
        with self._lock, self._db:
            row = self._db.execute(
                "SELECT version, deleted_at FROM notes WHERE id=?", (note_id,)
            ).fetchone()
            if row is None:
                raise KeyError(note_id)
            if row["version"] != version:
                raise RuntimeError("version conflict")
            if row["deleted_at"]:
                return self.get_note(note_id)
            self._db.execute(
                "UPDATE notes SET deleted_at=?, updated_at=?, version=version+1 WHERE id=?",
                (_now(), _now(), note_id),
            )
        return self.get_note(note_id)

    def duplicate_note(self, note_id: str) -> dict[str, Any]:
        """Copy a note (content and tags) into a new note."""
        source = self.get_note(note_id, include_deleted=False)
        return self.create_note(
            f"{source['title']} (copy)".strip()[:MAX_TITLE],
            source.get("content", ""),
            list(source.get("tags", [])),
        )

    def rename_tag(self, old: str, new: str) -> int:
        """Rename a tag across every live note. Returns the number changed."""
        old = str(old or "").strip().lower()
        new = str(new or "").strip().lower()
        if not old:
            raise ValueError("old tag is required")
        if not new:
            raise ValueError("new tag is required")
        if new == old:
            return 0
        changed = 0
        with self._lock, self._db:
            rows = self._db.execute(
                "SELECT id, tags FROM notes WHERE deleted_at IS NULL"
            ).fetchall()
            for row in rows:
                tags = json.loads(row["tags"])
                if old not in tags:
                    continue
                renamed = [new if tag == old else tag for tag in tags]
                renamed = list(dict.fromkeys(renamed))
                self._db.execute(
                    "UPDATE notes SET tags=?, updated_at=? WHERE id=?",
                    (json.dumps(renamed), _now(), row["id"]),
                )
                changed += 1
        return changed

    def remove_tag(self, tag: str) -> int:
        """Remove a tag from every live note. Returns the number changed."""
        tag = str(tag or "").strip().lower()
        if not tag:
            raise ValueError("tag is required")
        changed = 0
        with self._lock, self._db:
            rows = self._db.execute(
                "SELECT id, tags FROM notes WHERE deleted_at IS NULL"
            ).fetchall()
            for row in rows:
                tags = json.loads(row["tags"])
                if tag not in tags:
                    continue
                remaining = [existing for existing in tags if existing != tag]
                self._db.execute(
                    "UPDATE notes SET tags=?, updated_at=? WHERE id=?",
                    (json.dumps(remaining), _now(), row["id"]),
                )
                changed += 1
        return changed

    def set_pin(self, note_id: str, pinned: bool) -> dict[str, Any]:
        """Pin or unpin a note. Pinned notes sort to the top of the list."""
        if not isinstance(note_id, str) or not NOTE_ID_RE.fullmatch(note_id):
            raise KeyError(note_id)
        with self._lock, self._db:
            row = self._db.execute(
                "SELECT deleted_at FROM notes WHERE id=?", (note_id,)
            ).fetchone()
            if row is None:
                raise KeyError(note_id)
            if row["deleted_at"]:
                raise KeyError(note_id)
            self._db.execute(
                "UPDATE notes SET pinned=?, updated_at=? WHERE id=?",
                (1 if pinned else 0, _now(), note_id),
            )
        return self.get_note(note_id)

    def restore_note(self, note_id: str, version: int) -> dict[str, Any]:
        """Bring a trashed note back with its content intact."""
        if not isinstance(note_id, str) or not NOTE_ID_RE.fullmatch(note_id):
            raise KeyError(note_id)
        with self._lock, self._db:
            row = self._db.execute(
                "SELECT version, deleted_at FROM notes WHERE id=?", (note_id,)
            ).fetchone()
            if row is None:
                raise KeyError(note_id)
            if row["version"] != version:
                raise RuntimeError("version conflict")
            if not row["deleted_at"]:
                return self.get_note(note_id)
            self._db.execute(
                "UPDATE notes SET deleted_at=NULL, updated_at=?, version=version+1 WHERE id=?",
                (_now(), note_id),
            )
        return self.get_note(note_id)

    def purge_note(self, note_id: str, version: int) -> bool:
        """Permanently remove a trashed note."""
        if not isinstance(note_id, str) or not NOTE_ID_RE.fullmatch(note_id):
            raise KeyError(note_id)
        with self._lock, self._db:
            row = self._db.execute(
                "SELECT version, deleted_at FROM notes WHERE id=?", (note_id,)
            ).fetchone()
            if row is None:
                raise KeyError(note_id)
            if row["version"] != version:
                raise RuntimeError("version conflict")
            self._db.execute("DELETE FROM notes WHERE id=?", (note_id,))
        return True

    def append_chat_message(self, role: str, content: str, backend: str = "") -> dict[str, Any]:
        role = _clean_text(role, "role", 16)
        if role not in {"user", "assistant", "system"}:
            raise ValueError("role must be user, assistant, or system")
        content = _clean_text(content, "content", MAX_CONTENT)
        timestamp = _now()
        with self._lock, self._db:
            cursor = self._db.execute(
                "INSERT INTO chat_messages(role,content,backend,created_at) VALUES(?,?,?,?)",
                (role, content, backend, timestamp),
            )
        return {
            "id": cursor.lastrowid,
            "role": role,
            "content": content,
            "backend": backend,
            "created_at": timestamp,
        }

    def list_chat_messages(self, limit: int = 100) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        rows = self._db.execute(
            "SELECT id,role,content,backend,created_at FROM chat_messages ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def clear_chat(self) -> int:
        with self._lock, self._db:
            cursor = self._db.execute("DELETE FROM chat_messages")
        return int(cursor.rowcount or 0)

    def create_inbox_item(self, text: str) -> dict[str, Any]:
        text = _clean_text(text, "text", 10_000)
        item_id = secrets.token_hex(12)
        timestamp = _now()
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO inbox(id,text,status,created_at,updated_at) VALUES(?,?,'open',?,?)",
                (item_id, text, timestamp, timestamp),
            )
        return self.get_inbox_item(item_id)

    def get_inbox_item(self, item_id: str) -> dict[str, Any]:
        row = self._db.execute("SELECT * FROM inbox WHERE id=?", (item_id,)).fetchone()
        if row is None:
            raise KeyError(item_id)
        return dict(row)

    def list_inbox(self, status: str = "") -> list[dict[str, Any]]:
        status = str(status or "").strip()
        if status:
            rows = self._db.execute(
                "SELECT * FROM inbox WHERE status=? ORDER BY created_at DESC", (status,)
            ).fetchall()
        else:
            rows = self._db.execute("SELECT * FROM inbox ORDER BY created_at DESC").fetchall()
        return [dict(row) for row in rows]

    def update_inbox(self, item_id: str, status: str) -> dict[str, Any]:
        status = _clean_text(status, "status", 32)
        if status not in {"open", "done", "archived"}:
            raise ValueError("status must be open, done, or archived")
        with self._lock, self._db:
            row = self._db.execute("SELECT id FROM inbox WHERE id=?", (item_id,)).fetchone()
            if row is None:
                raise KeyError(item_id)
            self._db.execute(
                "UPDATE inbox SET status=?, updated_at=? WHERE id=?", (status, _now(), item_id)
            )
        return self.get_inbox_item(item_id)


_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^\s)]+)(?:\s+\"[^\"]*\")?\)")
_CODE_FENCE_RE = re.compile(r"```(.*?)```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")


def _safe_link(url: str) -> str:
    compact = re.sub(r"[\s\x00-\x1f]+", "", url).casefold()
    if compact.startswith(("javascript:", "data:", "vbscript:", "file:")):
        return "#"
    if compact.startswith(("https://", "http://", "mailto:", "/", "#")):
        return html.escape(url, quote=True)
    return "#"


def _replace_links(text: str) -> str:
    """Replace Markdown links while respecting balanced parentheses in URLs.

    A regex cannot split ``[x](javascript:alert(1))`` reliably: it stops at
    the first ``)`` and leaves the remaining ``)`` inside the label.  This
    small scanner balances parentheses so the label and target stay exact.
    """
    out: list[str] = []
    cursor = 0
    while True:
        marker = text.find("](", cursor)
        if marker < 0:
            out.append(text[cursor:])
            break
        label_start = text.rfind("[", cursor, marker)
        if label_start < 0:
            out.append(text[cursor : marker + 2])
            cursor = marker + 2
            continue
        index = marker + 2
        depth = 1
        url_start = index
        while index < len(text):
            char = text[index]
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    break
            index += 1
        if depth != 0 or index >= len(text):
            out.append(text[cursor : marker + 2])
            cursor = marker + 2
            continue
        url = text[url_start:index]
        label = text[label_start + 1 : marker]
        out.append(text[cursor:label_start])
        out.append(f'<a href="{_safe_link(url)}" rel="noopener noreferrer">{label}</a>')
        cursor = index + 1
    return "".join(out)


def render_markdown(source: str) -> str:
    """Render a deliberately small, safe Markdown subset.

    All source text is HTML-escaped before formatting.  Only http(s), mailto,
    local and fragment links survive; scriptable schemes become inert anchors.
    """
    text = html.escape(str(source or ""), quote=False)
    placeholders: list[str] = []

    def fence(match: re.Match[str]) -> str:
        placeholders.append("<pre><code>" + match.group(1) + "</code></pre>")
        return f"\x00FENCE{len(placeholders) - 1}\x00"

    text = _CODE_FENCE_RE.sub(fence, text)

    def inline_code(match: re.Match[str]) -> str:
        return "<code>" + match.group(1) + "</code>"

    text = _INLINE_CODE_RE.sub(inline_code, text)
    lines: list[str] = []
    in_list = False
    for raw in text.splitlines():
        line = raw.rstrip()
        heading = re.match(r"^(#{1,6})\s+(.+)$", line)
        bullet = re.match(r"^[-*]\s+(.+)$", line)
        if heading:
            if in_list:
                lines.append("</ul>")
                in_list = False
            level = len(heading.group(1))
            lines.append(f"<h{level}>{heading.group(2)}</h{level}>")
        elif bullet:
            if not in_list:
                lines.append("<ul>")
                in_list = True
            lines.append(f"<li>{bullet.group(1)}</li>")
        else:
            if in_list:
                lines.append("</ul>")
                in_list = False
            if line:
                lines.append(f"<p>{line}</p>")
    if in_list:
        lines.append("</ul>")
    rendered = "\n".join(lines)

    rendered = _replace_links(rendered)
    rendered = re.sub(r"\*\*([^*\n]+)\*\*", r"<strong>\1</strong>", rendered)
    rendered = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<em>\1</em>", rendered)
    for index, value in enumerate(placeholders):
        rendered = rendered.replace(f"\x00FENCE{index}\x00", value)
    return rendered or "<p class=\"muted\">Nothing written yet.</p>"


_HTML_PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Hermes Workspace</title>
<style>
:root{color-scheme:dark;--bg:#0d1117;--panel:#161b22;--line:#30363d;--text:#e6edf3;--muted:#8b949e;--accent:#58a6ff;--danger:#f85149;--ok:#3fb950}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 ui-sans-serif,system-ui,sans-serif}button,input,textarea{font:inherit;color:inherit}.shell{display:grid;grid-template-columns:220px minmax(280px,1fr) minmax(360px,1.5fr);height:100vh}.rail,.list,.editor{min-width:0;overflow:auto;border-right:1px solid var(--line)}.rail{padding:18px 12px}.brand{font-weight:700;font-size:17px;margin:0 8px 20px}.rail button{display:block;width:100%;text-align:left;margin:4px 0;padding:9px 10px;border:0;border-radius:7px;background:transparent;color:var(--muted);cursor:pointer}.rail button.active,.rail button:hover{background:#21262d;color:var(--text)}.rail .status{margin:24px 8px;color:var(--muted);font-size:12px}.list{padding:16px}.list-head{display:flex;gap:8px;margin-bottom:12px}.list-head input{flex:1;min-width:0;padding:8px;background:var(--bg);border:1px solid var(--line);border-radius:6px}.list-head select{flex:1;min-width:0;padding:8px;background:var(--bg);border:1px solid var(--line);border-radius:6px}.import-label{padding:8px 12px;border:1px solid var(--line);border-radius:6px;cursor:pointer}.import-label input{display:none}.note-card{padding:10px;border:1px solid transparent;border-radius:7px;cursor:pointer}.note-card:hover{background:var(--panel)}.note-card.active{border-color:var(--accent);background:var(--panel)}.note-card strong{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.note-card small{color:var(--muted)}.editor{padding:18px 22px;border-right:0}.editor h1{font-size:20px;margin:0 0 12px}.toolbar{display:flex;gap:8px;align-items:center;margin-bottom:12px}.toolbar input{flex:1;padding:8px;background:var(--bg);border:1px solid var(--line);border-radius:6px}.toolbar button,.primary{padding:8px 12px;border:1px solid var(--line);border-radius:6px;background:#21262d;cursor:pointer}.toolbar .primary{background:#238636;border-color:#2ea043}.split{display:grid;grid-template-columns:1fr 1fr;gap:14px}.pane{min-width:0}.pane label{display:block;color:var(--muted);font-size:12px;margin-bottom:6px}textarea{width:100%;height:calc(100vh - 230px);resize:vertical;padding:12px;background:var(--bg);color:var(--text);border:1px solid var(--line);border-radius:8px;font:13px/1.55 ui-monospace,SFMono-Regular,monospace}.preview{height:calc(100vh - 230px);overflow:auto;padding:12px;border:1px solid var(--line);border-radius:8px}.preview pre{overflow:auto;background:var(--bg);padding:10px}.preview code{color:#79c0ff}.muted{color:var(--muted)}.shortcuts{font-size:12px;margin:0 0 10px}.error{color:var(--danger)}.success{color:var(--ok)}.chat-log{height:calc(100vh - 250px);overflow:auto;padding:10px;border:1px solid var(--line);border-radius:8px}.chat-log p{white-space:pre-wrap}.chat-form{display:flex;gap:8px;margin-top:10px}.chat-form input{flex:1;padding:9px;background:var(--bg);border:1px solid var(--line);border-radius:6px}.thinking{color:var(--accent);padding:8px 2px;font-size:13px}.thinking:after{content:'';animation:pulse 1.2s infinite}.chat-form button:disabled{opacity:.55;cursor:wait}@keyframes pulse{0%,100%{opacity:.3}50%{opacity:1}}.term{height:calc(100vh - 260px);overflow:auto;white-space:pre-wrap;padding:10px;background:#05070a;border:1px solid var(--line);border-radius:8px;font-family:ui-monospace,monospace}@media(max-width:900px){.shell{grid-template-columns:150px 1fr}.editor{grid-column:1/-1}.split{grid-template-columns:1fr}textarea,.preview,.chat-log,.term{height:360px}}
</style>
</head>
<body>
<div class="shell">
<nav class="rail"><h1 class="brand">Hermes</h1><button class="active" data-panel="notes">Notes</button><button data-panel="chat">Chat</button><button data-panel="inbox">Inbox</button><button data-panel="trash">Trash</button><button data-panel="sessions">Sessions</button><button data-panel="terminal">Terminal</button><button data-panel="system">System</button><div class="status" id="status">Connecting…</div></nav>
<section class="list" id="list-panel"><div class="list-head"><input id="note-search" placeholder="Search notes"></div><div class="list-head"><select id="note-tag-filter"><option value="">All tags</option></select><select id="note-sort"><option value="updated">Recently updated</option><option value="created">Newest first</option><option value="title">Title A–Z</option></select><button id="manage-tags" title="Rename or remove a tag">Tags</button></div><div id="note-list"></div></section>
<section class="editor" id="editor-panel">
<div id="notes-view"><h1>Notes</h1><div class="toolbar"><input id="note-title" placeholder="Note title"><input id="note-tags" placeholder="tags, comma separated"><button class="primary" id="save-note">Save</button><button id="new-note">New</button><button id="delete-note">Delete</button><button id="pin-note">Pin</button><button id="duplicate-note">Duplicate</button><button id="history-note">History</button><button id="ask-about-note">Ask about this note</button><button id="export-notes">Export</button><label class="import-label">Import<input id="import-notes" type="file" accept="application/json,.json"></label></div><p class="muted shortcuts">Shortcuts: ⌘S save · ⌘N new note · ⌘K search</p><div class="split"><div class="pane"><label for="note-content">MARKDOWN</label><textarea id="note-content" spellcheck="true"></textarea></div><div class="pane"><label>SAFE PREVIEW</label><div id="note-preview" class="preview"></div></div></div></div>
<div id="inbox-view" hidden><h1>Inbox</h1><div class="toolbar"><input id="inbox-input" placeholder="Capture a thought…"><button class="primary" id="add-inbox">Add</button></div><div id="inbox-list"></div></div>
<div id="history-view" hidden><h1>History</h1><p class="muted">Every saved version of this note.</p><div id="history-list"></div></div>
<div id="trash-view" hidden><h1>Trash</h1><p class="muted">Deleted notes stay here until you purge them.</p><div id="trash-list"></div></div>
<div id="sessions-view" hidden><h1>Recent sessions</h1><div id="sessions-list"></div></div>
<div id="terminal-view" hidden><h1>Read-only terminal</h1><div class="toolbar"><input id="term-input" value="pwd"><button id="run-term">Run</button></div><pre id="term-output" class="term"></pre></div>
<div id="system-view" hidden><h1>System</h1><pre id="system-output" class="term"></pre></div>
<div id="chat-view" hidden><h1>Chat <button id="clear-chat" style="font-size:12px">Clear history</button></h1><div id="chat-context" class="muted"></div><div id="chat-log" class="chat-log"></div><div id="chat-thinking" class="thinking" hidden>Hermes is thinking… <span id="chat-elapsed">0s</span></div><form id="chat-form" class="chat-form"><input id="chat-input" placeholder="Ask Hermes…"><button class="primary" id="chat-send">Send</button></form></div>
</section></div>
<script>
const $=s=>document.querySelector(s), $$=s=>[...document.querySelectorAll(s)];
const params=new URLSearchParams(location.search); let token=params.get('token')||sessionStorage.getItem('workspace-token')||'';
if(token){sessionStorage.setItem('workspace-token',token);params.delete('token');history.replaceState({},'',location.pathname+(params.size?'?'+params:''));}
let notes=[],selected=null,version=1;
async function api(path,options={}){options.headers={...(options.headers||{}),'Authorization':'Bearer '+token};if(options.method&&options.method!=='GET'){options.headers['Origin']=location.origin;options.headers['X-Workspace-CSRF']=token;options.headers['Content-Type']='application/json';}const r=await fetch(path,options);const data=await r.json().catch(()=>({}));if(!r.ok)throw new Error(data.error||('HTTP '+r.status));return data;}
function esc(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
function showError(e){$('#status').textContent=e.message||String(e);$('#status').className='status error';}
function draftKey(){return 'workspace-draft-'+(selected||'new');}
function saveDraft(){try{localStorage.setItem(draftKey(),JSON.stringify({title:$('#note-title').value,tags:$('#note-tags').value,content:$('#note-content').value,at:Date.now()}));}catch(e){}}
function clearDraft(){try{localStorage.removeItem(draftKey());}catch(e){}}
function restoreDraft(){try{const raw=localStorage.getItem(draftKey());if(!raw)return false;const d=JSON.parse(raw);if(!d||(!d.title&&!d.content))return false;$('#note-title').value=d.title||'';$('#note-tags').value=d.tags||'';$('#note-content').value=d.content||'';return true;}catch(e){return false;}}
async function loadTags(){const tags=(await api('/api/tags')).tags;const sel=$('#note-tag-filter');const current=sel.value;sel.innerHTML='<option value="">All tags</option>'+tags.map(t=>`<option value="${esc(t.tag)}">${esc(t.tag)} (${t.count})</option>`).join('');if(tags.some(t=>t.tag===current))sel.value=current;}
async function loadNotes(){notes=(await api('/api/notes?q='+encodeURIComponent($('#note-search').value)+'&tag='+encodeURIComponent($('#note-tag-filter').value)+'&sort='+encodeURIComponent($('#note-sort').value))).notes;await loadTags();$('#note-list').innerHTML=notes.map(n=>`<div class="note-card ${selected===n.id?'active':''}" data-id="${esc(n.id)}"><strong>${n.pinned?'📌 ':''}${esc(n.title)}</strong><small>${esc(n.tags.join(', '))} · v${n.version}</small></div>`).join('')||'<p class="muted">No notes yet.</p>';$$('.note-card').forEach(el=>el.onclick=()=>selectNote(el.dataset.id));}
async function showHistory(){if(!selected){showError(new Error('Select a note first'));return;}const versions=(await api('/api/notes/'+selected+'/versions')).versions;$('#history-list').innerHTML=versions.map(v=>`<div class="note-card"><strong>v${v.version} · ${esc(v.title)}</strong><small class="muted">${esc(v.created_at)} · ${esc(v.tags.join(', '))}</small><div><button data-restore-version="${v.version}">Restore</button></div></div>`).join('')||'<p class="muted">No versions stored.</p>';$$('#history-list [data-restore-version]').forEach(b=>b.onclick=async()=>{const r=await api('/api/notes/'+selected+'/versions/'+b.dataset.restoreVersion+'/restore',{method:'POST'});version=r.note.version;$('#note-title').value=r.note.title;$('#note-tags').value=r.note.tags.join(', ');$('#note-content').value=r.note.content;$('#note-preview').innerHTML=r.note.html;await showHistory();await loadNotes();$('#status').textContent='Restored v'+b.dataset.restoreVersion;$('#status').className='status success';});await switchPanel('history');}
async function duplicateNote(){if(!selected){showError(new Error('Select a note first'));return;}const r=await api('/api/notes/'+selected+'/duplicate',{method:'POST'});selected=r.note.id;version=r.note.version;$('#note-title').value=r.note.title;$('#note-tags').value=r.note.tags.join(', ');$('#note-content').value=r.note.content;$('#note-preview').innerHTML=r.note.html;await loadNotes();$('#status').textContent='Duplicated';$('#status').className='status success';}
async function manageTags(){const tag=$('#note-tag-filter').value;if(!tag){showError(new Error('Pick a tag in the filter first'));return;}const action=prompt('Tag actions for "'+tag+'"\nType a new name to rename it, or "-" to remove it from every note.','-');if(action===null)return;const value=action.trim();if(!value)return;try{if(value==='-'){const r=await api('/api/tags/'+encodeURIComponent(tag),{method:'DELETE'});$('#status').textContent='Removed tag from '+r.changed+' notes';}else{const r=await api('/api/tags/rename',{method:'POST',body:JSON.stringify({old:tag,new:value})});$('#status').textContent='Renamed tag on '+r.changed+' notes';}$('#status').className='status success';$('#note-tag-filter').value='';await loadTags();await loadNotes();}catch(e){showError(e);}}
async function togglePin(){if(!selected){showError(new Error('Select a note first'));return;}const note=notes.find(n=>n.id===selected);if(!note)return;const r=await api('/api/notes/'+selected+'/pin',{method:'POST',body:JSON.stringify({pinned:!note.pinned})});$('#pin-note').textContent=r.note.pinned?'Unpin':'Pin';await loadNotes();}
async function selectNote(id){selected=id;const {note}=await api('/api/notes/'+id);version=note.version;$('#note-title').value=note.title;$('#note-tags').value=note.tags.join(', ');$('#note-content').value=note.content;$('#note-preview').innerHTML=note.html;$('#pin-note').textContent=note.pinned?'Unpin':'Pin';if(restoreDraft()){$('#status').textContent='Unsaved draft restored';$('#status').className='status success';}await loadNotes();}
async function saveNote(){if(!$('#note-title').value.trim())return showError(new Error('Title is required'));const wasNew=!selected;const body={title:$('#note-title').value,content:$('#note-content').value,tags:$('#note-tags').value.split(',').map(x=>x.trim()).filter(Boolean),version};if(selected){const {note}=await api('/api/notes/'+selected,{method:'PUT',body:JSON.stringify(body)});version=note.version;$('#note-preview').innerHTML=note.html;}else{const {note}=await api('/api/notes',{method:'POST',body:JSON.stringify(body)});selected=note.id;version=note.version;$('#note-preview').innerHTML=note.html;}clearDraft();if(wasNew){try{localStorage.removeItem('workspace-draft-new');}catch(e){}}await loadNotes();$('#status').textContent='Saved';$('#status').className='status success';}
async function newNote(){selected=null;version=1;$('#note-title').value='';$('#note-tags').value='';$('#note-content').value='';$('#note-preview').innerHTML='';clearDraft();}
async function deleteNote(){if(!selected||!confirm('Move this note to trash?'))return;try{await api('/api/notes/'+selected,{method:'DELETE',body:JSON.stringify({version})});await newNote();await loadNotes();$('#status').textContent='Moved to trash';$('#status').className='status success';}catch(e){showError(e);}}
async function switchPanel(name){$$('.rail button').forEach(b=>b.classList.toggle('active',b.dataset.panel===name));for(const v of ['notes','chat','inbox','history','trash','sessions','terminal','system']) $('#'+v+'-view').hidden=v!==name;if(name==='chat'){await renderChatHistory();}$('#list-panel').hidden=name!=='notes';if(name==='inbox'){await renderInbox();}if(name==='trash'){const notes=(await api('/api/trash')).notes;$('#trash-list').innerHTML=notes.map(n=>`<div class="note-card"><strong>${esc(n.title)}</strong><small class="muted">deleted ${esc(n.deleted_at||'')}</small><div><button data-restore="${esc(n.id)}" data-version="${n.version}">Restore</button> <button data-purge="${esc(n.id)}" data-version="${n.version}">Purge</button></div></div>`).join('')||'<p class="muted">Trash is empty.</p>';$$('#trash-list [data-restore]').forEach(b=>b.onclick=async()=>{await api('/api/notes/'+b.dataset.restore+'/restore',{method:'POST',body:JSON.stringify({version:Number(b.dataset.version)})});await loadNotes();await switchPanel('trash');});$$('#trash-list [data-purge]').forEach(b=>b.onclick=async()=>{if(!confirm('Permanently delete this note?'))return;await api('/api/notes/'+b.dataset.purge+'/purge',{method:'DELETE',body:JSON.stringify({version:Number(b.dataset.version)})});await switchPanel('trash');});}if(name==='sessions'){$('#sessions-list').innerHTML=(await api('/api/sessions')).sessions.map(s=>`<p>${esc(s.title||s.id)} <small class="muted">${esc(s.source||'')}</small></p>`).join('');}if(name==='system'){$('#system-output').textContent=JSON.stringify((await api('/api/system')).system,null,2);}}
$('#note-tag-filter').onchange=()=>loadNotes().catch(showError);$('#note-sort').onchange=()=>loadNotes().catch(showError);$('#export-notes').onclick=async()=>{try{const data=await api('/api/export');const blob=new Blob([JSON.stringify(data,null,2)],{type:'application/json'});const url=URL.createObjectURL(blob);const a=document.createElement('a');a.href=url;a.download='hermes-workspace.json';document.body.appendChild(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);$('#status').textContent='Exported '+data.notes.length+' notes';$('#status').className='status success';}catch(e){showError(e);}};$('#import-notes').onchange=async e=>{const file=e.target.files&&e.target.files[0];if(!file)return;try{const payload=JSON.parse(await file.text());const result=await api('/api/import',{method:'POST',body:JSON.stringify({notes:payload.notes||payload})});await loadTags();await loadNotes();$('#status').textContent='Imported '+result.imported+' notes';$('#status').className='status success';}catch(err){showError(err);}e.target.value='';};$('#save-note').onclick=()=>saveNote().catch(showError);$('#pin-note').onclick=()=>togglePin().catch(showError);$('#duplicate-note').onclick=()=>duplicateNote().catch(showError);$('#history-note').onclick=()=>showHistory().catch(showError);$('#manage-tags').onclick=()=>manageTags().catch(showError);$('#new-note').onclick=newNote;$('#delete-note').onclick=()=>deleteNote().catch(showError);$('#note-search').oninput=()=>loadNotes().catch(showError);$('#note-title').oninput=saveDraft;$('#note-tags').oninput=saveDraft;let previewTimer=null;$('#note-content').oninput=()=>{saveDraft();clearTimeout(previewTimer);previewTimer=setTimeout(()=>api('/api/markdown/preview',{method:'POST',body:JSON.stringify({content:$('#note-content').value})}).then(r=>{$('#note-preview').innerHTML=r.html;}).catch(showError),180);};$$('.rail button').forEach(b=>b.onclick=()=>switchPanel(b.dataset.panel));
document.addEventListener('keydown',e=>{const mod=e.metaKey||e.ctrlKey;if(!mod)return;const key=e.key.toLowerCase();if(key==='s'){e.preventDefault();saveNote().catch(showError);}else if(key==='n'){e.preventDefault();newNote();}else if(key==='k'){e.preventDefault();$('#note-search').focus();}});
async function renderInbox(){const items=(await api('/api/inbox')).items;$('#inbox-list').innerHTML=items.map(i=>`<div class="note-card"><strong>${esc(i.text)}</strong><small class="muted">${esc(i.status)} · ${esc(i.created_at)}</small><div>${i.status!=='done'?`<button data-inbox-done="${esc(i.id)}">Done</button>`:''} ${i.status!=='archived'?`<button data-inbox-archive="${esc(i.id)}">Archive</button>`:''}</div></div>`).join('')||'<p class="muted">Nothing captured yet.</p>';$$('#inbox-list [data-inbox-done]').forEach(b=>b.onclick=async()=>{await api('/api/inbox/'+b.dataset.inboxDone,{method:'PATCH',body:JSON.stringify({status:'done'})});await renderInbox();});$$('#inbox-list [data-inbox-archive]').forEach(b=>b.onclick=async()=>{await api('/api/inbox/'+b.dataset.inboxArchive,{method:'PATCH',body:JSON.stringify({status:'archived'})});await renderInbox();});}
$('#add-inbox').onclick=async()=>{const text=$('#inbox-input').value.trim();if(!text)return;try{await api('/api/inbox',{method:'POST',body:JSON.stringify({text})});$('#inbox-input').value='';await renderInbox();}catch(e){showError(e);}};
$('#inbox-input').onkeydown=e=>{if(e.key==='Enter'){e.preventDefault();$('#add-inbox').click();}};
let pendingChatContext='';
async function renderChatHistory(){const data=await api('/api/chat');$('#chat-log').innerHTML=data.messages.map(m=>`<p><b>${m.role==='assistant'?'Hermes':'You'}:</b> ${esc(m.content)}</p>`).join('');$('#chat-log').scrollTop=$('#chat-log').scrollHeight;}
function noteContext(){const title=$('#note-title').value.trim();const body=$('#note-content').value;if(!title&&!body.trim())return '';return title+'\n\n'+body;}
$('#ask-about-note').onclick=async()=>{pendingChatContext=noteContext();if(!pendingChatContext){showError(new Error('Write or select a note first'));return;}$('#chat-context').textContent='Context: the current note will be shared with your next message.';$('#chat-input').placeholder='Ask about this note…';await switchPanel('chat');$('#chat-input').focus();};
$('#clear-chat').onclick=async()=>{await api('/api/chat',{method:'DELETE'});await renderChatHistory();};
$('#run-term').onclick=()=>api('/api/terminal',{method:'POST',body:JSON.stringify({input:$('#term-input').value})}).then(r=>$('#term-output').textContent=r.output).catch(showError);
let chatBusy=false,chatTimer=null;
function setChatBusy(busy){chatBusy=busy;$('#chat-send').disabled=busy;$('#chat-thinking').hidden=!busy;$('#chat-input').readOnly=busy;if(busy){const started=Date.now();$('#chat-elapsed').textContent='0s';chatTimer=setInterval(()=>{$('#chat-elapsed').textContent=Math.floor((Date.now()-started)/1000)+'s';},1000);}else{clearInterval(chatTimer);chatTimer=null;}}
async function streamChat(text,context){const response=await fetch('/api/chat/stream',{method:'POST',headers:{'Authorization':'Bearer '+token,'Origin':location.origin,'X-Workspace-CSRF':token,'Content-Type':'application/json'},body:JSON.stringify({message:text,context})});if(!response.ok||!response.body){const data=await response.json().catch(()=>({}));throw new Error(data.error||('HTTP '+response.status));}const reader=response.body.getReader();const decoder=new TextDecoder();const live=document.createElement('p');live.innerHTML='<b>Hermes:</b> ';$('#chat-log').appendChild(live);let buffer='';let full='';for(;;){const {value,done}=await reader.read();if(done)break;buffer+=decoder.decode(value,{stream:true});let index;while((index=buffer.indexOf('\n\n'))>=0){const raw=buffer.slice(0,index);buffer=buffer.slice(index+2);const line=raw.split('\n').find(l=>l.startsWith('data:'));if(!line)continue;const event=JSON.parse(line.slice(5).trim());if(event.error)throw new Error(event.error);if(event.delta){full+=event.delta;live.textContent='Hermes: '+full;$('#chat-log').scrollTop=$('#chat-log').scrollHeight;}}}return full;}
$('#chat-form').onsubmit=async e=>{e.preventDefault();if(chatBusy)return;const input=$('#chat-input');const text=input.value;if(!text.trim())return;$('#chat-log').insertAdjacentHTML('beforeend','<p><b>You:</b> '+esc(text)+'</p>');input.value='';const context=pendingChatContext;pendingChatContext='';$('#chat-context').textContent='';setChatBusy(true);try{await streamChat(text,context);await renderChatHistory();}catch(err){$('#chat-log').insertAdjacentHTML('beforeend','<p class="error">'+esc(err.message)+'</p>');input.value=text;}finally{setChatBusy(false);}};
(async()=>{try{$('#status').textContent='Ready';await loadNotes();}catch(e){showError(e);}})();
</script></body></html>"""


class _WorkspaceHandler(BaseHTTPRequestHandler):
    server_version = "HermesWorkspace/1.0"
    store: WorkspaceStore
    token: str
    host: str
    port: int

    def log_message(self, format: str, *args: Any) -> None:
        # Never put bearer tokens or note contents in the access log.
        print(f"[workspace] {self.address_string()} {format % args}", file=sys.stderr)

    def _security_headers(self, *, html: bool = False) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
        )
        # Content-Type is set by the caller: JSON, HTML, and the SSE stream
        # each declare their own media type.

    def _json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self._security_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, message: str) -> None:
        self._json(status, {"error": message})

    def _authorized(self) -> bool:
        header = self.headers.get("Authorization", "")
        prefix = "Bearer "
        if not header.startswith(prefix):
            return False
        return secrets.compare_digest(header[len(prefix) :], self.token)

    def _same_origin(self) -> bool:
        origin = self.headers.get("Origin", "")
        allowed = {f"http://{self.host}:{self.port}", f"http://localhost:{self.port}"}
        return origin in allowed

    def _mutation_allowed(self) -> bool:
        if not self._authorized():
            self._error(401, "authentication required")
            return False
        if not self._same_origin():
            self._error(403, "cross-origin mutation rejected")
            return False
        csrf = self.headers.get("X-Workspace-CSRF", "")
        if not secrets.compare_digest(csrf, self.token):
            self._error(403, "CSRF token missing or invalid")
            return False
        return True

    def _read_json(self) -> dict[str, Any]:
        raw_length = self.headers.get("Content-Length", "0")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise ValueError("invalid Content-Length") from exc
        if length <= 0:
            return {}
        if length > MAX_BODY_BYTES:
            raise ValueError("request body too large")
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("invalid JSON body") from exc
        if not isinstance(data, dict):
            raise ValueError("JSON body must be an object")
        return data

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path in {"/", "/index.html"}:
                # The first browser load receives the one-time token in the
                # URL; JavaScript moves it to sessionStorage and strips it.
                # A strict HttpOnly cookie keeps later reloads authenticated.
                query_token = (parse_qs(parsed.query).get("token") or [""])[0]
                cookie_token = ""
                raw_cookie = self.headers.get("Cookie", "")
                if raw_cookie:
                    try:
                        cookie = SimpleCookie()
                        cookie.load(raw_cookie)
                        morsel = cookie.get("workspace_session")
                        cookie_token = morsel.value if morsel else ""
                    except Exception:
                        cookie_token = ""
                authorized = (
                    self._authorized()
                    or secrets.compare_digest(query_token, self.token)
                    or secrets.compare_digest(cookie_token, self.token)
                )
                if not authorized:
                    self._error(401, "authentication required")
                    return
                body = _HTML_PAGE.encode("utf-8")
                self.send_response(200)
                self._security_headers(html=True)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                if query_token and secrets.compare_digest(query_token, self.token):
                    self.send_header(
                        "Set-Cookie",
                        f"workspace_session={self.token}; HttpOnly; SameSite=Strict; Path=/",
                    )
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if not self._authorized():
                self._error(401, "authentication required")
                return
            query = parse_qs(parsed.query)
            if path == "/api/notes":
                items = self.store.list_notes(
                    query=(query.get("q") or [""])[0],
                    tag=(query.get("tag") or [""])[0],
                    sort=(query.get("sort") or ["updated"])[0],
                    include_content=True,
                )
                self._json(200, {"notes": items})
                return
            if path == "/api/tags":
                self._json(200, {"tags": self.store.tag_summary()})
                return
            if path == "/api/export":
                payload = json.dumps(self.store.export_all(), ensure_ascii=False, indent=2)
                body = payload.encode("utf-8")
                self.send_response(200)
                self._security_headers()
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Disposition", 'attachment; filename="hermes-workspace.json"')
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if path.startswith("/api/notes/") and path.endswith("/versions"):
                note_id = path.removeprefix("/api/notes/").removesuffix("/versions")
                try:
                    self._json(200, {"versions": self.store.list_versions(note_id)})
                except KeyError:
                    self._error(404, "note not found")
                return
            if path.startswith("/api/notes/"):
                note_id = path.removeprefix("/api/notes/")
                try:
                    self._json(200, {"note": self.store.get_note(note_id)})
                except KeyError:
                    self._error(404, "note not found")
                return
            if path == "/api/trash":
                self._json(
                    200,
                    {"notes": self.store.list_notes(include_deleted=True, include_content=True)},
                )
                return
            if path == "/api/inbox":
                self._json(
                    200, {"items": self.store.list_inbox((query.get("status") or [""])[0])}
                )
                return
            if path == "/api/sessions":
                self._json(200, {"sessions": _recent_sessions()})
                return
            if path == "/api/system":
                self._json(200, {"system": _system_info()})
                return
            if path == "/api/chat":
                self._json(200, {"messages": self.store.list_chat_messages()})
                return
            if path == "/api/files":
                self._error(404, "not found")
                return
            self._error(404, "not found")
        except Exception as exc:
            self._error(500, "internal error")
            print(f"[workspace] request failed: {exc!r}", file=sys.stderr)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if not self._mutation_allowed():
            return
        try:
            data = self._read_json()
            if path == "/api/notes":
                note = self.store.create_note(
                    data.get("title", ""), data.get("content", ""), data.get("tags", [])
                )
                self._json(201, {"note": note})
                return
            if path == "/api/inbox":
                self._json(201, {"item": self.store.create_inbox_item(data.get("text", ""))})
                return
            if path.startswith("/api/notes/") and "/versions/" in path and path.endswith("/restore"):
                note_id, _, tail = path.removeprefix("/api/notes/").partition("/versions/")
                version = int(tail.removesuffix("/restore"))
                note = self.store.restore_version(note_id, version)
                self._json(200, {"note": note})
                return
            if path.startswith("/api/notes/") and path.endswith("/duplicate"):
                note_id = path.removeprefix("/api/notes/").removesuffix("/duplicate")
                self._json(201, {"note": self.store.duplicate_note(note_id)})
                return
            if path.startswith("/api/notes/") and path.endswith("/pin"):
                note_id = path.removeprefix("/api/notes/").removesuffix("/pin")
                pinned = bool(data.get("pinned", True))
                self._json(200, {"note": self.store.set_pin(note_id, pinned)})
                return
            if path.startswith("/api/notes/") and path.endswith("/restore"):
                note_id = path.removeprefix("/api/notes/").removesuffix("/restore")
                note = self.store.restore_note(note_id, data.get("version", 1))
                self._json(200, {"note": note})
                return
            if path == "/api/import":
                imported = self.store.import_notes(data.get("notes"))
                self._json(200, {"imported": imported})
                return
            if path == "/api/tags/rename":
                changed = self.store.rename_tag(data.get("old", ""), data.get("new", ""))
                self._json(200, {"changed": changed})
                return
            if path == "/api/markdown/preview":
                content = _clean_text(data.get("content", ""), "content", MAX_CONTENT, required=False)
                self._json(200, {"html": render_markdown(content)})
                return
            if path == "/api/terminal":
                self._json(200, _bounded_terminal(data.get("input", "")))
                return
            if path == "/api/chat/stream":
                self._stream_chat(data)
                return
            if path == "/api/chat":
                self._json(
                    200,
                    _chat_turn(
                        self.store,
                        data.get("message", ""),
                        context=data.get("context", ""),
                    ),
                )
                return
            self._error(404, "not found")
        except KeyError:
            self._error(404, "note not found")
        except RuntimeError:
            self._error(409, "note changed in another tab; reload before saving")
        except ValueError as exc:
            self._error(400, str(exc))
        except Exception as exc:
            print(f"[workspace] mutation failed: {exc!r}", file=sys.stderr)
            self._error(500, "internal error")

    def _stream_chat(self, data: dict[str, Any]) -> None:
        """Stream a chat reply as Server-Sent Events."""
        message = _clean_text(data.get("message", ""), "message", 100_000)
        context = _clean_text(data.get("context", ""), "context", MAX_CONTENT, required=False)
        if context:
            self.store.append_chat_message("user", f"Shared note:\n{context}", backend="context")
        self.store.append_chat_message("user", message)
        history = self.store.list_chat_messages(limit=20)
        self.send_response(200)
        self._security_headers()
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        # The body has no content length: the client reads until EOF, so the
        # connection must close when the stream ends.
        self.send_header("Connection", "close")
        self.close_connection = True
        self.end_headers()
        collected: list[str] = []

        def emit(payload: dict[str, Any]) -> None:
            self.wfile.write(f"data: {json.dumps(payload)}\n\n".encode("utf-8"))
            self.wfile.flush()

        try:
            for delta in _stream_deltas(history):
                if not delta:
                    continue
                collected.append(delta)
                emit({"delta": delta})
        except Exception as exc:
            emit({"error": str(exc)})
            return
        reply = "".join(collected).strip()
        if reply:
            self.store.append_chat_message("assistant", reply, backend="stream")
        emit({"done": True, "reply": reply})

    def do_PUT(self) -> None:
        path = urlparse(self.path).path
        if not self._mutation_allowed():
            return
        try:
            data = self._read_json()
            if path.startswith("/api/notes/"):
                note_id = path.removeprefix("/api/notes/")
                note = self.store.update_note(
                    note_id,
                    data.get("title", ""),
                    data.get("content", ""),
                    data.get("tags", []),
                    data.get("version", 1),
                )
                self._json(200, {"note": note})
                return
            self._error(404, "not found")
        except KeyError:
            self._error(404, "note not found")
        except RuntimeError:
            self._error(409, "note changed in another tab; reload before saving")
        except ValueError as exc:
            self._error(400, str(exc))
        except Exception as exc:
            print(f"[workspace] mutation failed: {exc!r}", file=sys.stderr)
            self._error(500, "internal error")

    def do_DELETE(self) -> None:
        path = urlparse(self.path).path
        if not self._mutation_allowed():
            return
        try:
            if path.startswith("/api/tags/"):
                tag = path.removeprefix("/api/tags/")
                changed = self.store.remove_tag(tag)
                self._json(200, {"changed": changed})
                return
            if path == "/api/chat":
                cleared = self.store.clear_chat()
                self._json(200, {"cleared": cleared})
                return
            if path.startswith("/api/notes/"):
                data = self._read_json()
                if path.endswith("/purge"):
                    note_id = path.removeprefix("/api/notes/").removesuffix("/purge")
                    self.store.purge_note(note_id, data.get("version", 1))
                    self._json(200, {"purged": True})
                    return
                note_id = path.removeprefix("/api/notes/")
                note = self.store.delete_note(note_id, data.get("version", 1))
                self._json(200, {"deleted": True, "note": note})
                return
            self._error(404, "not found")
        except KeyError:
            self._error(404, "note not found")
        except RuntimeError:
            self._error(409, "note changed in another tab; reload before deleting")
        except ValueError as exc:
            self._error(400, str(exc))
        except Exception as exc:
            print(f"[workspace] mutation failed: {exc!r}", file=sys.stderr)
            self._error(500, "internal error")

    def do_PATCH(self) -> None:
        if not self._mutation_allowed():
            return
        path = urlparse(self.path).path
        try:
            data = self._read_json()
            if path.startswith("/api/inbox/"):
                item_id = path.removeprefix("/api/inbox/")
                self._json(200, {"item": self.store.update_inbox(item_id, data.get("status", ""))})
                return
            self._error(404, "not found")
        except KeyError:
            self._error(404, "inbox item not found")
        except ValueError as exc:
            self._error(400, str(exc))


def _recent_sessions(limit: int = 30) -> list[dict[str, Any]]:
    home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
    db_path = home / "state.db"
    if not db_path.exists():
        return []
    try:
        db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=1)
        db.row_factory = sqlite3.Row
        rows = db.execute(
            "SELECT id,title,source,started_at,last_activity_at FROM sessions ORDER BY COALESCE(last_activity_at,started_at) DESC LIMIT ?",
            (limit,),
        ).fetchall()
        db.close()
        return [dict(row) for row in rows]
    except (sqlite3.Error, OSError):
        return []


def _system_info() -> dict[str, Any]:
    home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
    return {
        "platform": platform.platform(),
        "python": platform.python_version(),
        "hostname": platform.node(),
        "cwd": str(Path.cwd()),
        "hermes_home": str(home),
        "time_utc": datetime.now(UTC).isoformat(timespec="seconds"),
    }


_ALLOWED_TERMINAL = {
    "pwd": [],
    "whoami": [],
    "uname": ["-a"],
    "date": [],
    "ls": ["-la"],
    "git": ["status", "--short", "--branch"],
}


def _bounded_terminal(command: str) -> dict[str, Any]:
    command = _clean_text(command, "input", 512)
    parts = command.split()
    if not parts:
        raise ValueError("command is required")
    program, args = parts[0], parts[1:]
    allowed = _ALLOWED_TERMINAL.get(program)
    if allowed is None:
        raise ValueError(f"command not allowed: {program}")
    # Only the exact documented argument vector is accepted; this prevents a
    # read-only command from being turned into an arbitrary file/command read.
    if args != allowed:
        raise ValueError(f"arguments not allowed for {program}")
    try:
        result = subprocess.run(
            [program, *args], capture_output=True, text=True, timeout=10, check=False,
            cwd=os.environ.get("HERMES_WORKSPACE_CWD", str(Path.cwd())),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"output": f"command failed: {exc}", "exit_code": 1}
    output = (result.stdout + result.stderr).strip()
    return {"output": output, "exit_code": result.returncode}


def _hermes_chat_command(message: str) -> list[str]:
    """One-shot Hermes chat using the user's own configured provider.

    Using the CLI keeps provider resolution, OAuth, and model selection in
    Hermes where they belong: the workspace never has to read or copy an API
    key. ``-m hermes_cli.main`` is used with the package root on PYTHONPATH
    so this also works from a source checkout.
    """
    return [sys.executable, "-m", "hermes_cli.main", "chat", "-q", message]


def _run_hermes_provider(messages: list[dict[str, str]]) -> dict[str, Any]:
    """Call Hermes' auxiliary LLM router inside this process.

    ``agent.auxiliary_client.call_llm`` resolves the user's configured
    provider, model, and credentials. Running in-process avoids spawning a
    second Hermes installation, which the shared-profile guard rejects.
    """
    try:
        from agent.auxiliary_client import call_llm
    except ImportError as exc:
        raise ValueError(f"Hermes provider runtime unavailable: {exc}") from exc
    model = os.environ.get("HERMES_WORKSPACE_MODEL", "").strip() or None
    try:
        response = call_llm(
            task="workspace_chat",
            model=model,
            messages=messages,
            timeout=120,
        )
    except Exception as exc:
        raise ValueError(f"Hermes provider call failed: {exc}") from exc
    choice = getattr(response, "choices", [None])[0]
    content = getattr(getattr(choice, "message", None), "content", None)
    if isinstance(content, list):
        content = "".join(
            str(part.get("text", "")) for part in content if isinstance(part, dict)
        )
    if not content:
        raise ValueError("Hermes provider returned no content")
    return {"reply": str(content).strip(), "backend": "hermes-provider"}


def _run_hermes_cli_chat(message: str) -> dict[str, Any]:
    env = os.environ.copy()
    package_root = str(Path(__file__).resolve().parent.parent)
    env["PYTHONPATH"] = package_root + os.pathsep + env.get("PYTHONPATH", "")
    env["HERMES_SESSION_SOURCE"] = "web-workspace"
    model = os.environ.get("HERMES_WORKSPACE_MODEL", "").strip()
    command = _hermes_chat_command(message)
    if model:
        command += ["-m", model]
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=180, check=False, env=env
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError(f"could not start Hermes chat: {exc}") from exc
    reply = result.stdout.strip()
    if result.returncode != 0 or not reply:
        detail = (result.stderr or result.stdout or "no output").strip()[-500:]
        raise ValueError(f"Hermes chat failed (exit {result.returncode}): {detail}")
    return {"reply": reply, "backend": "hermes-cli"}


def _stream_hermes_provider(messages: list[dict[str, str]]) -> Iterator[str]:
    """Yield text deltas from Hermes' auxiliary router (stream=True)."""
    try:
        from agent.auxiliary_client import call_llm
    except ImportError as exc:
        raise ValueError(f"Hermes provider runtime unavailable: {exc}") from exc
    model = os.environ.get("HERMES_WORKSPACE_MODEL", "").strip() or None
    try:
        stream = call_llm(
            task="workspace_chat",
            model=model,
            messages=messages,
            timeout=120,
            stream=True,
        )
    except Exception as exc:
        raise ValueError(f"Hermes provider call failed: {exc}") from exc
    if hasattr(stream, "choices"):
        # Some adapters answer with a completed response instead of a stream.
        choice = getattr(stream, "choices", [None])[0]
        content = getattr(getattr(choice, "message", None), "content", None)
        if isinstance(content, list):
            content = "".join(
                str(part.get("text", "")) for part in content if isinstance(part, dict)
            )
        if content:
            yield str(content)
        return
    for chunk in stream:
        choices = getattr(chunk, "choices", None)
        if not choices:
            continue
        delta = getattr(choices[0], "delta", None)
        text = getattr(delta, "content", None)
        if text is None:
            text = getattr(getattr(choices[0], "message", None), "content", None)
        if isinstance(text, list):
            text = "".join(str(part.get("text", "")) for part in text if isinstance(part, dict))
        if text:
            yield str(text)


def _stream_openai(messages: list[dict[str, str]]) -> Iterator[str]:
    """Yield text deltas from an OpenAI-compatible streaming endpoint."""
    base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    api_key = os.environ.get("OPENAI_API_KEY", "")
    model = os.environ.get("HERMES_WORKSPACE_MODEL", "gpt-4o-mini")
    if not api_key:
        raise ValueError("chat is not configured: set OPENAI_API_KEY")
    payload = json.dumps(
        {"model": model, "messages": messages, "stream": True}
    ).encode()
    req = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    try:
        response = urllib.request.urlopen(req, timeout=120)
    except Exception as exc:
        raise ValueError(f"model request failed: {exc}") from exc
    with response:
        for raw in response:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            payload_text = line[5:].strip()
            if payload_text == "[DONE]":
                return
            try:
                chunk = json.loads(payload_text)
            except json.JSONDecodeError:
                continue
            for choice in chunk.get("choices", []):
                delta = choice.get("delta") or {}
                text = delta.get("content")
                if text:
                    yield str(text)


def _stream_deltas(messages: list[dict[str, str]]) -> Iterator[str]:
    """Stream deltas from the configured backend, falling back to one delta."""
    backend = os.environ.get("HERMES_WORKSPACE_CHAT_BACKEND", "hermes").strip().lower()
    if backend == "openai":
        yield from _stream_openai(messages)
        return
    try:
        yield from _stream_hermes_provider(messages)
        return
    except ValueError:
        if backend != "auto":
            raise
    result = _chat_with_messages(messages)
    yield result["reply"]


def _last_user_message(messages: list[dict[str, str]]) -> str:
    for item in reversed(messages):
        if item.get("role") == "user":
            return str(item.get("content", ""))
    return str(messages[-1].get("content", "")) if messages else ""


def _chat_with_messages(messages: list[dict[str, str]]) -> dict[str, Any]:
    if not messages:
        raise ValueError("at least one message is required")
    backend = os.environ.get("HERMES_WORKSPACE_CHAT_BACKEND", "hermes").strip().lower()
    last_message = _last_user_message(messages)
    if backend == "cli":
        return _run_hermes_cli_chat(last_message)
    if backend in {"hermes", "auto"}:
        try:
            return _run_hermes_provider(messages)
        except ValueError:
            if backend != "auto":
                raise
            # ``auto`` falls through to the CLI, then to a configured HTTP key.
            try:
                return _run_hermes_cli_chat(last_message)
            except ValueError:
                if not os.environ.get("OPENAI_API_KEY"):
                    raise
    if not os.environ.get("OPENAI_API_KEY"):
        raise ValueError(
            "chat is not configured: Hermes CLI chat failed and OPENAI_API_KEY is not set"
        )
    base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    api_key = os.environ.get("OPENAI_API_KEY", "")
    model = os.environ.get("HERMES_WORKSPACE_MODEL", "gpt-4o-mini")
    if not api_key:
        raise ValueError("chat is not configured: set OPENAI_API_KEY")
    payload = json.dumps(
        {"model": model, "messages": messages, "stream": False}
    ).encode()
    req = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=payload,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as response:
            data = json.loads(response.read().decode())
        reply = data["choices"][0]["message"]["content"]
        return {"reply": str(reply)}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:500]
        raise ValueError(f"model request failed ({exc.code}): {detail}") from exc
    except (urllib.error.URLError, KeyError, IndexError, json.JSONDecodeError) as exc:
        raise ValueError(f"model request failed: {exc}") from exc


def _chat_reply(message: str) -> dict[str, Any]:
    """Single-turn helper retained for direct callers and tests."""
    message = _clean_text(message, "message", 100_000)
    return _chat_with_messages([{"role": "user", "content": message}])


def _chat_turn(store: WorkspaceStore, message: str, context: str = "") -> dict[str, Any]:
    """Persist a chat turn, then call the provider with prior history."""
    message = _clean_text(message, "message", 100_000)
    context = _clean_text(context, "context", MAX_CONTENT, required=False)
    if context:
        store.append_chat_message("user", f"Shared note:\n{context}", backend="context")
    store.append_chat_message("user", message)
    history = store.list_chat_messages(limit=20)
    result = _chat_with_messages(history)
    store.append_chat_message("assistant", result["reply"], backend=result.get("backend", ""))
    return result


def make_server(
    host: str = "127.0.0.1",
    port: int = 8787,
    *,
    store: WorkspaceStore | None = None,
    token: str | None = None,
) -> ThreadingHTTPServer:
    if host not in {"127.0.0.1", "::1", "localhost"}:
        raise ValueError("workspace server must bind to loopback")
    if store is None:
        home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
        store = WorkspaceStore(home / "workspace")
    if token is None:
        token = secrets.token_urlsafe(32)
    if not token or len(token) < 16:
        raise ValueError("workspace token must contain at least 16 characters")

    class Handler(_WorkspaceHandler):
        pass

    Handler.store = store
    Handler.token = token
    Handler.host = host
    Handler.port = port
    httpd = ThreadingHTTPServer((host, port), Handler)
    # Port 0 asks the OS to choose; the browser sees the bound port, so the
    # same-origin allowlist must use that value rather than the requested 0.
    Handler.port = int(httpd.server_address[1])
    httpd.daemon_threads = True
    httpd.workspace_store = store  # type: ignore[attr-defined]
    httpd.workspace_token = token  # type: ignore[attr-defined]
    return httpd


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the local Hermes browser workspace")
    parser.add_argument("--host", default="127.0.0.1", choices=["127.0.0.1", "localhost"])
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--root", default=None, help="workspace data directory")
    parser.add_argument("--token", default=None, help="bearer token (generated when omitted)")
    parser.add_argument("--no-browser", action="store_true", help="do not open the browser")
    parser.add_argument("--check", action="store_true", help="verify the workspace and exit")
    return parser


def run(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = Path(args.root) if args.root else Path(
        os.environ.get("HERMES_HOME", Path.home() / ".hermes")
    ) / "workspace"
    store = WorkspaceStore(root)
    token = args.token or secrets.token_urlsafe(32)
    try:
        httpd = make_server(args.host, args.port, store=store, token=token)
    except OSError as exc:
        print(f"Cannot bind workspace server: {exc}", file=sys.stderr)
        store.close()
        return 1
    actual_port = httpd.server_address[1]
    url = f"http://{args.host}:{actual_port}/?token={token}"
    print(f"Hermes Workspace: {url}")
    print(f"Data: {store.root}")
    if not args.no_browser:
        try:
            import webbrowser

            webbrowser.open(url)
        except Exception:
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        store.close()
    return 0


def default_workspace_root() -> Path:
    """Where the workspace keeps its data unless --root says otherwise."""
    return Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")) / "workspace"


def check_workspace(root: Path | str | None = None) -> dict[str, Any]:
    """Verify the workspace can run: database, schema, renderer, loopback bind."""
    target = Path(root) if root else default_workspace_root()
    checks: list[dict[str, Any]] = []

    def record(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    try:
        target.mkdir(parents=True, exist_ok=True)
        store = WorkspaceStore(target)
    except Exception as exc:
        record("database", False, f"{type(exc).__name__}: {exc}")
        return {"ok": False, "root": str(target), "checks": checks}
    record("database", True, f"opened {store.db_path}")
    try:
        tables = {
            row["name"]
            for row in store._db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        missing = {"notes", "inbox", "chat_messages"} - tables
        record("schema", not missing, "missing tables: " + ", ".join(sorted(missing)) if missing else "notes, inbox, chat_messages present")
        counts = {
            "notes": len(store.list_notes()),
            "trashed": len(store.list_notes(include_deleted=True)),
            "inbox": len(store.list_inbox()),
            "chat": len(store.list_chat_messages()),
        }
        record("data", True, ", ".join(f"{k}={v}" for k, v in counts.items()))
        rendered = render_markdown("# check\n\n**ok** <script>x</script>")
        record("renderer", "<h1>check</h1>" in rendered and "<script>" not in rendered, "markdown renderer escapes unsafe markup")
    finally:
        store.close()

    try:
        import socket

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            bound = probe.getsockname()[1]
        record("loopback", bool(bound), f"can bind 127.0.0.1:{bound}")
    except Exception as exc:
        record("loopback", False, f"{type(exc).__name__}: {exc}")

    return {"ok": all(check["ok"] for check in checks), "root": str(target), "checks": checks}


def cmd_workspace(args) -> int:
    """CLI adapter for ``hermes workspace``."""
    if getattr(args, "check", False):
        result = check_workspace(getattr(args, "root", None))
        print(f"Workspace check: {result['root']}")
        for check in result["checks"]:
            print(f"  [{'ok' if check['ok'] else 'FAIL'}] {check['name']}: {check['detail']}")
        print("Result: " + ("ok" if result["ok"] else "problems found"))
        return 0 if result["ok"] else 1
    argv: list[str] = ["--host", str(getattr(args, "host", "127.0.0.1"))]
    argv += ["--port", str(getattr(args, "port", 8787))]
    for name in ("root", "token"):
        value = getattr(args, name, None)
        if value:
            argv += [f"--{name}", str(value)]
    if getattr(args, "no_browser", False):
        argv.append("--no-browser")
    return run(argv)


def main(argv: list[str] | None = None) -> int:
    return run(argv)


if __name__ == "__main__":
    raise SystemExit(main())
