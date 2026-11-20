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
from typing import Any
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
                    version INTEGER NOT NULL DEFAULT 1
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
                """
            )

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
        }
        if include_content:
            note["content"] = row["content"]
            note["html"] = render_markdown(row["content"])
        return note

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
        return self.get_note(note_id)

    def get_note(self, note_id: str) -> dict[str, Any]:
        if not isinstance(note_id, str) or not NOTE_ID_RE.fullmatch(note_id):
            raise KeyError(note_id)
        row = self._db.execute("SELECT * FROM notes WHERE id=?", (note_id,)).fetchone()
        if row is None:
            raise KeyError(note_id)
        return self._note(row)

    def list_notes(
        self, query: str = "", tag: str = "", *, include_content: bool = False
    ) -> list[dict[str, Any]]:
        query = str(query or "").strip()
        tag = str(tag or "").strip().lower()
        rows = self._db.execute(
            "SELECT * FROM notes ORDER BY updated_at DESC, id ASC"
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
        return result

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
            row = self._db.execute("SELECT version FROM notes WHERE id=?", (note_id,)).fetchone()
            if row is None or not NOTE_ID_RE.fullmatch(str(note_id)):
                raise KeyError(note_id)
            if row["version"] != version:
                raise RuntimeError("version conflict")
            self._db.execute(
                "UPDATE notes SET title=?, content=?, tags=?, updated_at=?, version=version+1 WHERE id=?",
                (title, content, json.dumps(tags), timestamp, note_id),
            )
        return self.get_note(note_id)

    def delete_note(self, note_id: str, version: int) -> bool:
        if not isinstance(note_id, str) or not NOTE_ID_RE.fullmatch(note_id):
            raise KeyError(note_id)
        with self._lock, self._db:
            row = self._db.execute("SELECT version FROM notes WHERE id=?", (note_id,)).fetchone()
            if row is None:
                raise KeyError(note_id)
            if row["version"] != version:
                raise RuntimeError("version conflict")
            self._db.execute("DELETE FROM notes WHERE id=?", (note_id,))
        return True

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
:root{color-scheme:dark;--bg:#0d1117;--panel:#161b22;--line:#30363d;--text:#e6edf3;--muted:#8b949e;--accent:#58a6ff;--danger:#f85149;--ok:#3fb950}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 ui-sans-serif,system-ui,sans-serif}button,input,textarea{font:inherit;color:inherit}.shell{display:grid;grid-template-columns:220px minmax(280px,1fr) minmax(360px,1.5fr);height:100vh}.rail,.list,.editor{min-width:0;overflow:auto;border-right:1px solid var(--line)}.rail{padding:18px 12px}.brand{font-weight:700;font-size:17px;margin:0 8px 20px}.rail button{display:block;width:100%;text-align:left;margin:4px 0;padding:9px 10px;border:0;border-radius:7px;background:transparent;color:var(--muted);cursor:pointer}.rail button.active,.rail button:hover{background:#21262d;color:var(--text)}.rail .status{margin:24px 8px;color:var(--muted);font-size:12px}.list{padding:16px}.list-head{display:flex;gap:8px;margin-bottom:12px}.list-head input{flex:1;min-width:0;padding:8px;background:var(--bg);border:1px solid var(--line);border-radius:6px}.note-card{padding:10px;border:1px solid transparent;border-radius:7px;cursor:pointer}.note-card:hover{background:var(--panel)}.note-card.active{border-color:var(--accent);background:var(--panel)}.note-card strong{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.note-card small{color:var(--muted)}.editor{padding:18px 22px;border-right:0}.editor h1{font-size:20px;margin:0 0 12px}.toolbar{display:flex;gap:8px;align-items:center;margin-bottom:12px}.toolbar input{flex:1;padding:8px;background:var(--bg);border:1px solid var(--line);border-radius:6px}.toolbar button,.primary{padding:8px 12px;border:1px solid var(--line);border-radius:6px;background:#21262d;cursor:pointer}.toolbar .primary{background:#238636;border-color:#2ea043}.split{display:grid;grid-template-columns:1fr 1fr;gap:14px}.pane{min-width:0}.pane label{display:block;color:var(--muted);font-size:12px;margin-bottom:6px}textarea{width:100%;height:calc(100vh - 230px);resize:vertical;padding:12px;background:var(--bg);color:var(--text);border:1px solid var(--line);border-radius:8px;font:13px/1.55 ui-monospace,SFMono-Regular,monospace}.preview{height:calc(100vh - 230px);overflow:auto;padding:12px;border:1px solid var(--line);border-radius:8px}.preview pre{overflow:auto;background:var(--bg);padding:10px}.preview code{color:#79c0ff}.muted{color:var(--muted)}.error{color:var(--danger)}.success{color:var(--ok)}.chat-log{height:calc(100vh - 250px);overflow:auto;padding:10px;border:1px solid var(--line);border-radius:8px}.chat-log p{white-space:pre-wrap}.chat-form{display:flex;gap:8px;margin-top:10px}.chat-form input{flex:1;padding:9px;background:var(--bg);border:1px solid var(--line);border-radius:6px}.term{height:calc(100vh - 260px);overflow:auto;white-space:pre-wrap;padding:10px;background:#05070a;border:1px solid var(--line);border-radius:8px;font-family:ui-monospace,monospace}@media(max-width:900px){.shell{grid-template-columns:150px 1fr}.editor{grid-column:1/-1}.split{grid-template-columns:1fr}textarea,.preview,.chat-log,.term{height:360px}}
</style>
</head>
<body>
<div class="shell">
<nav class="rail"><h1 class="brand">Hermes</h1><button class="active" data-panel="notes">Notes</button><button data-panel="inbox">Inbox</button><button data-panel="sessions">Sessions</button><button data-panel="terminal">Terminal</button><button data-panel="system">System</button><div class="status" id="status">Connecting…</div></nav>
<section class="list" id="list-panel"><div class="list-head"><input id="note-search" placeholder="Search notes"></div><div id="note-list"></div></section>
<section class="editor" id="editor-panel">
<div id="notes-view"><h1>Notes</h1><div class="toolbar"><input id="note-title" placeholder="Note title"><input id="note-tags" placeholder="tags, comma separated"><button class="primary" id="save-note">Save</button><button id="new-note">New</button><button id="delete-note">Delete</button></div><div class="split"><div class="pane"><label for="note-content">MARKDOWN</label><textarea id="note-content" spellcheck="true"></textarea></div><div class="pane"><label>SAFE PREVIEW</label><div id="note-preview" class="preview"></div></div></div></div>
<div id="inbox-view" hidden><h1>Inbox</h1><div id="inbox-list"></div></div>
<div id="sessions-view" hidden><h1>Recent sessions</h1><div id="sessions-list"></div></div>
<div id="terminal-view" hidden><h1>Read-only terminal</h1><div class="toolbar"><input id="term-input" value="pwd"><button id="run-term">Run</button></div><pre id="term-output" class="term"></pre></div>
<div id="system-view" hidden><h1>System</h1><pre id="system-output" class="term"></pre></div>
<div id="chat-view" hidden><h1>Chat</h1><div id="chat-log" class="chat-log"></div><form id="chat-form" class="chat-form"><input id="chat-input" placeholder="Ask Hermes…"><button class="primary">Send</button></form></div>
</section></div>
<script>
const $=s=>document.querySelector(s), $$=s=>[...document.querySelectorAll(s)];
const params=new URLSearchParams(location.search); let token=params.get('token')||sessionStorage.getItem('workspace-token')||'';
if(token){sessionStorage.setItem('workspace-token',token);params.delete('token');history.replaceState({},'',location.pathname+(params.size?'?'+params:''));}
let notes=[],selected=null,version=1;
async function api(path,options={}){options.headers={...(options.headers||{}),'Authorization':'Bearer '+token};if(options.method&&options.method!=='GET'){options.headers['Origin']=location.origin;options.headers['X-Workspace-CSRF']=token;options.headers['Content-Type']='application/json';}const r=await fetch(path,options);const data=await r.json().catch(()=>({}));if(!r.ok)throw new Error(data.error||('HTTP '+r.status));return data;}
function esc(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
function showError(e){$('#status').textContent=e.message||String(e);$('#status').className='status error';}
async function loadNotes(){notes=(await api('/api/notes?q='+encodeURIComponent($('#note-search').value))).notes;$('#note-list').innerHTML=notes.map(n=>`<div class="note-card ${selected===n.id?'active':''}" data-id="${esc(n.id)}"><strong>${esc(n.title)}</strong><small>${esc(n.tags.join(', '))} · v${n.version}</small></div>`).join('')||'<p class="muted">No notes yet.</p>';$$('.note-card').forEach(el=>el.onclick=()=>selectNote(el.dataset.id));}
async function selectNote(id){selected=id;const {note}=await api('/api/notes/'+id);version=note.version;$('#note-title').value=note.title;$('#note-tags').value=note.tags.join(', ');$('#note-content').value=note.content;$('#note-preview').innerHTML=note.html;await loadNotes();}
async function saveNote(){if(!$('#note-title').value.trim())return showError(new Error('Title is required'));const body={title:$('#note-title').value,content:$('#note-content').value,tags:$('#note-tags').value.split(',').map(x=>x.trim()).filter(Boolean),version};if(selected){const {note}=await api('/api/notes/'+selected,{method:'PUT',body:JSON.stringify(body)});version=note.version;$('#note-preview').innerHTML=note.html;}else{const {note}=await api('/api/notes',{method:'POST',body:JSON.stringify(body)});selected=note.id;version=note.version;$('#note-preview').innerHTML=note.html;}await loadNotes();$('#status').textContent='Saved';$('#status').className='status success';}
async function newNote(){selected=null;version=1;$('#note-title').value='';$('#note-tags').value='';$('#note-content').value='';$('#note-preview').innerHTML='';}
async function deleteNote(){if(!selected||!confirm('Delete this note?'))return;try{await api('/api/notes/'+selected,{method:'DELETE',body:JSON.stringify({version})});await newNote();await loadNotes();}catch(e){showError(e);}}
async function switchPanel(name){$$('.rail button').forEach(b=>b.classList.toggle('active',b.dataset.panel===name));for(const v of ['notes','inbox','sessions','terminal','system']) $('#'+v+'-view').hidden=v!==name;$('#list-panel').hidden=name!=='notes';if(name==='inbox'){$('#inbox-list').innerHTML=(await api('/api/inbox')).items.map(i=>`<p>${esc(i.text)} <small class="muted">${esc(i.status)}</small></p>`).join('');}if(name==='sessions'){$('#sessions-list').innerHTML=(await api('/api/sessions')).sessions.map(s=>`<p>${esc(s.title||s.id)} <small class="muted">${esc(s.source||'')}</small></p>`).join('');}if(name==='system'){$('#system-output').textContent=JSON.stringify((await api('/api/system')).system,null,2);}}
$('#save-note').onclick=()=>saveNote().catch(showError);$('#new-note').onclick=newNote;$('#delete-note').onclick=()=>deleteNote().catch(showError);$('#note-search').oninput=()=>loadNotes().catch(showError);let previewTimer=null;$('#note-content').oninput=()=>{clearTimeout(previewTimer);previewTimer=setTimeout(()=>api('/api/markdown/preview',{method:'POST',body:JSON.stringify({content:$('#note-content').value})}).then(r=>{$('#note-preview').innerHTML=r.html;}).catch(showError),180);};$$('.rail button').forEach(b=>b.onclick=()=>switchPanel(b.dataset.panel));
$('#run-term').onclick=()=>api('/api/terminal',{method:'POST',body:JSON.stringify({input:$('#term-input').value})}).then(r=>$('#term-output').textContent=r.output).catch(showError);
$('#chat-form').onsubmit=async e=>{e.preventDefault();const input=$('#chat-input');const text=input.value;if(!text.trim())return;$('#chat-log').insertAdjacentHTML('beforeend','<p><b>You:</b> '+esc(text)+'</p>');input.value='';try{const r=await api('/api/chat',{method:'POST',body:JSON.stringify({message:text})});$('#chat-log').insertAdjacentHTML('beforeend','<p><b>Hermes:</b> '+esc(r.reply)+'</p>');}catch(e){$('#chat-log').insertAdjacentHTML('beforeend','<p class="error">'+esc(e.message)+'</p>');}};
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
        if html:
            self.send_header("Content-Type", "text/html; charset=utf-8")
        else:
            self.send_header("Content-Type", "application/json; charset=utf-8")

    def _json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self._security_headers()
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
                    include_content=True,
                )
                self._json(200, {"notes": items})
                return
            if path.startswith("/api/notes/"):
                note_id = path.removeprefix("/api/notes/")
                try:
                    self._json(200, {"note": self.store.get_note(note_id)})
                except KeyError:
                    self._error(404, "note not found")
                return
            if path == "/api/inbox":
                self._json(200, {"items": self.store.list_inbox()})
                return
            if path == "/api/sessions":
                self._json(200, {"sessions": _recent_sessions()})
                return
            if path == "/api/system":
                self._json(200, {"system": _system_info()})
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
            if path == "/api/markdown/preview":
                content = _clean_text(data.get("content", ""), "content", MAX_CONTENT, required=False)
                self._json(200, {"html": render_markdown(content)})
                return
            if path == "/api/terminal":
                self._json(200, _bounded_terminal(data.get("input", "")))
                return
            if path == "/api/chat":
                self._json(200, _chat_reply(data.get("message", "")))
                return
            self._error(404, "not found")
        except ValueError as exc:
            self._error(400, str(exc))
        except Exception as exc:
            print(f"[workspace] mutation failed: {exc!r}", file=sys.stderr)
            self._error(500, "internal error")

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
            if path.startswith("/api/notes/"):
                note_id = path.removeprefix("/api/notes/")
                data = self._read_json()
                self.store.delete_note(note_id, data.get("version", 1))
                self._json(200, {"deleted": True})
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


def _chat_reply(message: str) -> dict[str, Any]:
    message = _clean_text(message, "message", 100_000)
    base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    api_key = os.environ.get("OPENAI_API_KEY", "")
    model = os.environ.get("HERMES_WORKSPACE_MODEL", "gpt-4o-mini")
    if not api_key:
        raise ValueError("chat is not configured: set OPENAI_API_KEY")
    payload = json.dumps(
        {"model": model, "messages": [{"role": "user", "content": message}], "stream": False}
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


def cmd_workspace(args) -> int:
    """CLI adapter for ``hermes workspace``."""
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
