from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from hermes_cli import web_workspace


@pytest.fixture()
def server(tmp_path: Path):
    store = web_workspace.WorkspaceStore(tmp_path / "workspace")
    httpd = web_workspace.make_server("127.0.0.1", 0, store=store, token="test-token-1234567890")
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        yield base, store
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=3)
        store.close()


def request(base: str, path: str, *, method: str = "GET", payload=None, token=True, origin=True, csrf=True):
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = "Bearer test-token-1234567890"
    if origin:
        headers["Origin"] = base
    if csrf and method not in {"GET", "HEAD"}:
        headers["X-Workspace-CSRF"] = "test-token-1234567890"
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            body = response.read().decode()
            return response.status, json.loads(body) if body else None, response.headers
    except urllib.error.HTTPError as exc:
        body = exc.read().decode()
        return exc.code, json.loads(body) if body else None, exc.headers


def test_create_read_update_and_search_note(server):
    base, _store = server
    status, created, _ = request(base, "/api/notes", method="POST", payload={
        "title": "Research plan", "content": "# Initial\n\nalpha", "tags": ["research"],
    })
    assert status == 201
    note_id = created["note"]["id"]

    status, fetched, _ = request(base, f"/api/notes/{note_id}")
    assert status == 200
    assert fetched["note"]["content"] == "# Initial\n\nalpha"
    assert fetched["note"]["version"] == 1

    status, updated, _ = request(base, f"/api/notes/{note_id}", method="PUT", payload={
        "title": "Research plan — revised", "content": "# Revised\n\nbeta **works**",
        "tags": ["research", "active"], "version": 1,
    })
    assert status == 200
    assert updated["note"]["title"] == "Research plan — revised"
    assert updated["note"]["content"].startswith("# Revised")
    assert updated["note"]["tags"] == ["research", "active"]
    assert updated["note"]["version"] == 2
    assert "<h1>Revised</h1>" in updated["note"]["html"]
    assert "<strong>works</strong>" in updated["note"]["html"]

    status, listed, _ = request(base, "/api/notes?q=beta&tag=active")
    assert status == 200
    assert [n["id"] for n in listed["notes"]] == [note_id]
    assert listed["notes"][0]["content"].startswith("# Revised")


def test_note_update_rejects_stale_version_and_bad_input(server):
    base, _ = server
    _, created, _ = request(base, "/api/notes", method="POST", payload={"title": "A", "content": "one"})
    note_id = created["note"]["id"]
    assert request(base, f"/api/notes/{note_id}", method="PUT", payload={
        "title": "stale", "content": "bad", "version": 0,
    })[0] == 409
    assert request(base, f"/api/notes/{note_id}", method="PUT", payload={"title": "", "content": "x", "version": 1})[0] == 400
    assert request(base, f"/api/notes/{note_id}", method="PUT", payload={"title": "x", "content": 4, "version": 1})[0] == 400
    assert request(base, f"/api/notes/{note_id}", method="PUT", payload={"content": "x", "version": 1})[0] == 400
    assert request(base, "/api/notes/not-a-note")[0] == 404


def test_mutations_require_bearer_and_same_origin(server):
    base, _ = server
    assert request(base, "/api/notes", method="POST", payload={"title": "x"}, token=False)[0] == 401
    assert request(base, "/api/notes", method="POST", payload={"title": "x"}, origin=False)[0] == 403
    status, _, _ = request(base, "/api/notes", method="POST", payload={"title": "x"}, csrf=False)
    assert status == 403  # no CSRF token, even with bearer + Origin
    req = urllib.request.Request(base + "/api/notes", data=b"{}", method="POST", headers={
        "Authorization": "Bearer test-token-1234567890", "Origin": "https://attacker.invalid",
        "X-Workspace-CSRF": "test-token-1234567890", "Content-Type": "application/json",
    })
    with pytest.raises(urllib.error.HTTPError) as caught:
        urllib.request.urlopen(req, timeout=5)
    assert caught.value.code == 403


def test_csrf_allows_valid_mutation_and_security_headers(server):
    base, _ = server
    req = urllib.request.Request(base + "/api/notes", data=json.dumps({"title": "safe"}).encode(), method="POST", headers={
        "Authorization": "Bearer test-token-1234567890", "Origin": base, "X-Workspace-CSRF": "test-token-1234567890",
        "Content-Type": "application/json",
    })
    with urllib.request.urlopen(req, timeout=5) as response:
        assert response.status == 201
    with urllib.request.urlopen(base + "/?token=test-token-1234567890", timeout=5) as response:
        html = response.read().decode()
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert "default-src 'self'" in response.headers["Content-Security-Policy"]
        assert 'id="note-content"' in html
        assert 'id="save-note"' in html
        assert 'id="note-preview"' in html
        assert "test-token-1234567890" not in html


def test_delete_moves_note_to_trash_and_restore_brings_it_back(server):
    base, _ = server
    _, created, _ = request(base, "/api/notes", method="POST", payload={"title": "Keep me", "content": "body"})
    note_id = created["note"]["id"]

    status, deleted, _ = request(base, f"/api/notes/{note_id}", method="DELETE", payload={"version": 1})
    assert status == 200
    assert deleted["deleted"] is True
    assert deleted["note"]["deleted_at"]
    assert deleted["note"]["version"] == 2

    # Hidden from the normal list, visible in trash.
    _, listed, _ = request(base, "/api/notes")
    assert note_id not in [n["id"] for n in listed["notes"]]
    _, trash, _ = request(base, "/api/trash")
    assert [n["id"] for n in trash["notes"]] == [note_id]
    assert trash["notes"][0]["content"] == "body"

    # A trashed note cannot be edited until it is restored.
    assert request(base, f"/api/notes/{note_id}", method="PUT", payload={
        "title": "nope", "content": "x", "version": 2,
    })[0] == 404

    status, restored, _ = request(base, f"/api/notes/{note_id}/restore", method="POST", payload={"version": 2})
    assert status == 200
    assert restored["note"]["deleted_at"] is None
    assert restored["note"]["content"] == "body"
    _, listed, _ = request(base, "/api/notes")
    assert note_id in [n["id"] for n in listed["notes"]]


def test_purge_removes_note_permanently(server):
    base, _ = server
    _, created, _ = request(base, "/api/notes", method="POST", payload={"title": "Temporary", "content": "x"})
    note_id = created["note"]["id"]
    _, deleted, _ = request(base, f"/api/notes/{note_id}", method="DELETE", payload={"version": 1})
    trashed_version = deleted["note"]["version"]
    status, purged, _ = request(base, f"/api/notes/{note_id}/purge", method="DELETE", payload={"version": trashed_version})
    assert status == 200
    assert purged["purged"] is True
    assert request(base, f"/api/notes/{note_id}")[0] == 404
    _, trash, _ = request(base, "/api/trash")
    assert trash["notes"] == []


def test_trash_restore_rejects_stale_versions(server):
    base, _ = server
    _, created, _ = request(base, "/api/notes", method="POST", payload={"title": "Versioned", "content": "x"})
    note_id = created["note"]["id"]
    _, deleted, _ = request(base, f"/api/notes/{note_id}", method="DELETE", payload={"version": 1})
    assert deleted["note"]["version"] == 2
    assert request(base, f"/api/notes/{note_id}/restore", method="POST", payload={"version": 1})[0] == 409
    assert request(base, f"/api/notes/{note_id}/restore", method="POST", payload={"version": 2})[0] == 200


def test_browser_reload_keeps_authentication_with_strict_cookie(server):
    base, _ = server
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())
    with opener.open(base + "/?token=test-token-1234567890", timeout=5) as response:
        assert response.status == 200
        cookie_header = response.headers.get("Set-Cookie", "")
    assert "workspace_session=" in cookie_header
    assert "HttpOnly" in cookie_header
    assert "SameSite=Strict" in cookie_header
    # A reload has no token in the URL; the cookie alone must authenticate.
    with opener.open(base + "/", timeout=5) as response:
        assert response.status == 200
        assert "id=\"note-content\"" in response.read().decode()


def test_live_preview_endpoint_matches_saved_render(server):
    base, _ = server
    source = "# Live\n\n**safe** <script>x</script>"
    status, preview, _ = request(base, "/api/markdown/preview", method="POST", payload={"content": source})
    assert status == 200
    assert "<h1>Live</h1>" in preview["html"]
    assert "<strong>safe</strong>" in preview["html"]
    assert "<script>" not in preview["html"]
    _, created, _ = request(base, "/api/notes", method="POST", payload={"title": "same", "content": source})
    assert created["note"]["html"] == preview["html"]


def test_markdown_escapes_html_and_unsafe_links():
    rendered = web_workspace.render_markdown("# Hi\n<script>alert(1)</script>\n[x](javascript:alert(1))")
    assert "<script>" not in rendered
    assert "javascript:" not in rendered
    assert "&lt;script&gt;" in rendered
    assert "<h1>Hi</h1>" in rendered
    # Balanced parentheses stay inside the target; the label stays exact.
    assert ">x</a>" in rendered
    assert "x)" not in rendered
    assert '<a href="#" rel="noopener noreferrer">x</a>' in rendered


def test_store_survives_reopen(tmp_path: Path):
    root = tmp_path / "persistent"
    store = web_workspace.WorkspaceStore(root)
    note = store.create_note("Persistent", "body", ["one"])
    store.close()
    reopened = web_workspace.WorkspaceStore(root)
    try:
        assert reopened.get_note(note["id"])["content"] == "body"
        assert reopened.update_note(note["id"], "Persistent 2", "new body", ["two"], 1)["version"] == 2
    finally:
        reopened.close()


def test_browser_script_has_valid_javascript(tmp_path):
    import re
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:
        return  # JavaScript runtime is optional in minimal CI images
    html = web_workspace._HTML_PAGE
    scripts = re.findall(r"<script>(.*?)</script>", html, re.DOTALL)
    assert len(scripts) == 1
    script = tmp_path / "workspace-client.js"
    script.write_text(scripts[0], encoding="utf-8")
    result = subprocess.run(
        [node, "--check", str(script)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr


def test_cli_parser_and_command_adapter(monkeypatch):
    import argparse
    from hermes_cli.subcommands.workspace import build_workspace_parser

    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    build_workspace_parser(subparsers, cmd_workspace=lambda args: 0)
    args = parser.parse_args(["workspace", "--port", "9123", "--no-browser", "--root", "/tmp/ws"])
    assert args.command == "workspace"
    assert args.port == 9123
    assert args.no_browser is True
    assert args.root == "/tmp/ws"

    captured = []
    monkeypatch.setattr(web_workspace, "run", lambda argv: captured.append(argv) or 0)
    assert web_workspace.cmd_workspace(args) == 0
    assert captured == [["--host", "127.0.0.1", "--port", "9123", "--root", "/tmp/ws", "--no-browser"]]


def test_inbox_system_and_terminal_are_bounded(server):
    base, _ = server
    assert request(base, "/api/inbox")[0] == 200
    assert request(base, "/api/system")[0] == 200
    assert request(base, "/api/sessions")[0] == 200
    status, _result, _ = request(base, "/api/terminal", method="POST", payload={"input": "pwd"}, csrf=False)
    assert status == 403  # CSRF still required
    assert request(base, "/api/files?path=/etc/passwd")[0] == 404
