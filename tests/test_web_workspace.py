from __future__ import annotations

import json
import sys
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


def test_chat_uses_hermes_cli_with_configured_provider(monkeypatch):
    from types import SimpleNamespace

    calls = {}

    def fake_run(command, **kwargs):
        calls["command"] = command
        calls["env"] = kwargs.get("env", {})
        return SimpleNamespace(returncode=0, stdout="Provider reply" + chr(10), stderr="")

    monkeypatch.setattr(web_workspace.subprocess, "run", fake_run)
    monkeypatch.setenv("HERMES_WORKSPACE_CHAT_BACKEND", "cli")
    result = web_workspace._chat_reply("hello")
    assert result == {"reply": "Provider reply", "backend": "hermes-cli"}
    assert calls["command"][:5] == [sys.executable, "-m", "hermes_cli.main", "chat", "-q"]
    assert calls["command"][5] == "hello"
    assert calls["env"]["HERMES_SESSION_SOURCE"] == "web-workspace"
    assert package_root_in_env(calls["env"]["PYTHONPATH"])


def test_chat_uses_in_process_hermes_provider(monkeypatch):
    from types import ModuleType, SimpleNamespace

    module = ModuleType("agent.auxiliary_client")
    captured = {}

    def fake_call_llm(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="Provider reply"))]
        )

    module.call_llm = fake_call_llm
    package = ModuleType("agent")
    package.__path__ = []
    monkeypatch.setitem(sys.modules, "agent", package)
    monkeypatch.setitem(sys.modules, "agent.auxiliary_client", module)
    monkeypatch.setenv("HERMES_WORKSPACE_CHAT_BACKEND", "hermes")
    monkeypatch.delenv("HERMES_WORKSPACE_MODEL", raising=False)
    result = web_workspace._chat_reply("hello")
    assert result == {"reply": "Provider reply", "backend": "hermes-provider"}
    assert captured["messages"] == [{"role": "user", "content": "hello"}]
    assert captured["task"] == "workspace_chat"
    assert captured["model"] is None


def test_chat_provider_empty_response_is_an_error(monkeypatch):
    from types import ModuleType, SimpleNamespace

    module = ModuleType("agent.auxiliary_client")
    module.call_llm = lambda **kwargs: SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=None))]
    )
    package = ModuleType("agent")
    package.__path__ = []
    monkeypatch.setitem(sys.modules, "agent", package)
    monkeypatch.setitem(sys.modules, "agent.auxiliary_client", module)
    monkeypatch.setenv("HERMES_WORKSPACE_CHAT_BACKEND", "hermes")
    with pytest.raises(ValueError, match="no content"):
        web_workspace._chat_reply("hello")


def package_root_in_env(value):
    import pathlib

    return str(pathlib.Path(web_workspace.__file__).resolve().parent.parent) in value


def test_chat_failure_reports_provider_error(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(
        web_workspace.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=2, stdout="", stderr="provider not configured"),
    )
    monkeypatch.setenv("HERMES_WORKSPACE_CHAT_BACKEND", "cli")
    with pytest.raises(ValueError, match="provider not configured"):
        web_workspace._chat_reply("hello")


def test_openai_fallback_requires_key(monkeypatch):
    monkeypatch.setenv("HERMES_WORKSPACE_CHAT_BACKEND", "openai")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(ValueError):
        web_workspace._chat_reply("hello")


def test_openai_fallback_parses_provider_response(monkeypatch):
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps({"choices": [{"message": {"content": "HTTP reply"}}]}).encode()

    monkeypatch.setenv("HERMES_WORKSPACE_CHAT_BACKEND", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("HERMES_WORKSPACE_MODEL", "test-model")
    captured = {}

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["auth"] = request.headers.get("Authorization")
        return FakeResponse()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    result = web_workspace._chat_reply("hello")
    assert result["reply"] == "HTTP reply"
    assert captured["url"].endswith("/chat/completions")
    assert captured["auth"] == "Bearer test-key"


def _fake_provider_module(monkeypatch, reply="Reply"):
    from types import ModuleType, SimpleNamespace

    module = ModuleType("agent.auxiliary_client")
    captured = {"calls": []}

    def fake_call_llm(**kwargs):
        captured["calls"].append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=reply))]
        )

    module.call_llm = fake_call_llm
    package = ModuleType("agent")
    package.__path__ = []
    monkeypatch.setitem(sys.modules, "agent", package)
    monkeypatch.setitem(sys.modules, "agent.auxiliary_client", module)
    monkeypatch.setenv("HERMES_WORKSPACE_CHAT_BACKEND", "hermes")
    return captured


def test_chat_history_persists_across_turns(server, monkeypatch):
    base, _ = server
    captured = _fake_provider_module(monkeypatch, reply="First reply")

    status, first, _ = request(base, "/api/chat", method="POST", payload={"message": "first question"})
    assert status == 200
    assert first["reply"] == "First reply"
    assert first["backend"] == "hermes-provider"

    status, _second, _ = request(base, "/api/chat", method="POST", payload={"message": "second question"})
    assert status == 200
    # The provider sees the prior turn plus the new question.
    second_messages = captured["calls"][-1]["messages"]
    assert [m["role"] for m in second_messages] == ["user", "assistant", "user"]
    assert second_messages[0]["content"] == "first question"
    assert second_messages[1]["content"] == "First reply"
    assert second_messages[2]["content"] == "second question"

    _, history, _ = request(base, "/api/chat")
    assert [m["role"] for m in history["messages"]] == ["user", "assistant", "user", "assistant"]
    assert history["messages"][-1]["content"] == "First reply"

    # History is included in the workspace snapshot.
    _, exported, _ = request(base, "/api/export")
    assert len(exported["chat"]) == 4

    status, cleared, _ = request(base, "/api/chat", method="DELETE")
    assert status == 200
    assert cleared["cleared"] == 4
    _, history, _ = request(base, "/api/chat")
    assert history["messages"] == []


def test_chat_context_is_shared_with_provider(server, monkeypatch):
    base, _ = server
    captured = _fake_provider_module(monkeypatch)
    status, _result, _ = request(base, "/api/chat", method="POST", payload={
        "message": "summarize this", "context": "# My Note\nimportant body",
    })
    assert status == 200
    messages = captured["calls"][-1]["messages"]
    assert any("important body" in m["content"] for m in messages)
    assert messages[-1]["content"] == "summarize this"
    _, history, _ = request(base, "/api/chat")
    assert any(m["backend"] == "context" for m in history["messages"])


def test_pinned_notes_sort_first_and_persist(server, tmp_path: Path):
    base, _ = server
    _, first, _ = request(base, "/api/notes", method="POST", payload={"title": "First created"})
    _, second, _ = request(base, "/api/notes", method="POST", payload={"title": "Second created"})

    status, pinned, _ = request(base, f"/api/notes/{second['note']['id']}/pin", method="POST", payload={"pinned": True})
    assert status == 200
    assert pinned["note"]["pinned"] is True

    _, listed, _ = request(base, "/api/notes")
    titles = [n["title"] for n in listed["notes"]]
    # The pinned note leads; the other two share a timestamp, so only the
    # leading position is deterministic.
    assert titles[0] == "Second created"
    assert sorted(titles) == ["First created", "Second created"]

    # Pin state survives a restart.
    store = web_workspace.WorkspaceStore(tmp_path / "workspace")
    try:
        reopened = store.get_note(second["note"]["id"])
        assert reopened["pinned"] is True
    finally:
        store.close()

    status, unpinned, _ = request(base, f"/api/notes/{second['note']['id']}/pin", method="POST", payload={"pinned": False})
    assert status == 200
    assert unpinned["note"]["pinned"] is False
    assert request(base, "/api/notes/not-a-note/pin", method="POST", payload={"pinned": True})[0] == 404


def test_workspace_self_check_reports_ok(tmp_path: Path, capsys):
    result = web_workspace.check_workspace(tmp_path / "workspace")
    assert result["ok"] is True
    names = {check["name"] for check in result["checks"]}
    assert {"database", "schema", "data", "renderer", "loopback"} <= names

    class Args:
        root = str(tmp_path / "workspace")
        check = True
        port = 8787
        host = "127.0.0.1"
        token = None
        no_browser = False

    assert web_workspace.cmd_workspace(Args()) == 0
    output = capsys.readouterr().out
    assert "Result: ok" in output
    assert "[ok] schema" in output


def test_workspace_self_check_flags_unusable_root(tmp_path: Path):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("file in the way")
    result = web_workspace.check_workspace(blocker)
    assert result["ok"] is False
    assert result["checks"][0]["name"] == "database"


def test_chat_stream_emits_deltas_and_persists_reply(server, monkeypatch):
    base, _ = server
    from types import ModuleType, SimpleNamespace

    module = ModuleType("agent.auxiliary_client")

    def fake_call_llm(**kwargs):
        assert kwargs.get("stream") is True
        def chunks():
            for word in ["Streamed ", "reply ", "here"]:
                yield SimpleNamespace(
                    choices=[SimpleNamespace(delta=SimpleNamespace(content=word))]
                )
        return chunks()

    module.call_llm = fake_call_llm
    package = ModuleType("agent")
    package.__path__ = []
    monkeypatch.setitem(sys.modules, "agent", package)
    monkeypatch.setitem(sys.modules, "agent.auxiliary_client", module)
    monkeypatch.setenv("HERMES_WORKSPACE_CHAT_BACKEND", "hermes")

    req = urllib.request.Request(
        base + "/api/chat/stream",
        data=json.dumps({"message": "stream please"}).encode(),
        headers={
            "Authorization": "Bearer test-token-1234567890",
            "Origin": base,
            "X-Workspace-CSRF": "test-token-1234567890",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as response:
        assert response.headers["Content-Type"].startswith("text/event-stream")
        body = response.read().decode()
    events = [
        json.loads(line[len("data: "):])
        for line in body.splitlines()
        if line.startswith("data: ")
    ]
    assert [event["delta"] for event in events if "delta" in event] == [
        "Streamed ",
        "reply ",
        "here",
    ]
    assert events[-1]["done"] is True
    assert events[-1]["reply"] == "Streamed reply here"

    _, history, _ = request(base, "/api/chat")
    assert [m["role"] for m in history["messages"]] == ["user", "assistant"]
    assert history["messages"][-1]["content"] == "Streamed reply here"


def test_chat_stream_reports_provider_errors(server, monkeypatch):
    base, _ = server
    from types import ModuleType

    module = ModuleType("agent.auxiliary_client")

    def fake_call_llm(**kwargs):
        raise RuntimeError("provider exploded")

    module.call_llm = fake_call_llm
    package = ModuleType("agent")
    package.__path__ = []
    monkeypatch.setitem(sys.modules, "agent", package)
    monkeypatch.setitem(sys.modules, "agent.auxiliary_client", module)
    monkeypatch.setenv("HERMES_WORKSPACE_CHAT_BACKEND", "hermes")

    req = urllib.request.Request(
        base + "/api/chat/stream",
        data=json.dumps({"message": "stream please"}).encode(),
        headers={
            "Authorization": "Bearer test-token-1234567890",
            "Origin": base,
            "X-Workspace-CSRF": "test-token-1234567890",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as response:
        body = response.read().decode()
    assert "provider exploded" in body
    # A failed turn must not be stored as an assistant reply.
    _, history, _ = request(base, "/api/chat")
    assert [m["role"] for m in history["messages"]] == ["user"]


def test_note_version_history_records_and_restores(server):
    base, _ = server
    _, created, _ = request(base, "/api/notes", method="POST", payload={
        "title": "First title", "content": "first body", "tags": ["one"],
    })
    note_id = created["note"]["id"]

    _, updated, _ = request(base, f"/api/notes/{note_id}", method="PUT", payload={
        "title": "Second title", "content": "second body", "tags": ["one", "two"], "version": 1,
    })
    assert updated["note"]["version"] == 2

    status, history, _ = request(base, f"/api/notes/{note_id}/versions")
    assert status == 200
    versions = history["versions"]
    assert [v["version"] for v in versions] == [2, 1]
    assert versions[1]["title"] == "First title"
    assert versions[1]["content"] == "first body"
    assert versions[1]["tags"] == ["one"]

    # Restoring writes a NEW version rather than rewinding history.
    status, restored, _ = request(base, f"/api/notes/{note_id}/versions/1/restore", method="POST")
    assert status == 200
    assert restored["note"]["version"] == 3
    assert restored["note"]["title"] == "First title"
    assert restored["note"]["content"] == "first body"
    assert restored["note"]["tags"] == ["one"]

    _, history, _ = request(base, f"/api/notes/{note_id}/versions")
    assert [v["version"] for v in history["versions"]] == [3, 2, 1]
    assert request(base, f"/api/notes/{note_id}/versions/99/restore", method="POST")[0] == 404
    assert request(base, "/api/notes/not-a-note/versions")[0] == 404


def test_duplicate_note_copies_content_and_tags(server):
    base, _ = server
    _, created, _ = request(base, "/api/notes", method="POST", payload={
        "title": "Original", "content": "# body", "tags": ["keep", "copy"],
    })
    note_id = created["note"]["id"]
    status, copy, _ = request(base, f"/api/notes/{note_id}/duplicate", method="POST")
    assert status == 201
    assert copy["note"]["id"] != note_id
    assert copy["note"]["title"] == "Original (copy)"
    assert copy["note"]["content"] == "# body"
    assert copy["note"]["tags"] == ["keep", "copy"]

    _, listed, _ = request(base, "/api/notes")
    assert len(listed["notes"]) == 2
    assert request(base, "/api/notes/not-a-note/duplicate", method="POST")[0] == 404


def test_rename_and_remove_tag_across_notes(server):
    base, _ = server
    request(base, "/api/notes", method="POST", payload={"title": "One", "tags": ["old", "other"]})
    request(base, "/api/notes", method="POST", payload={"title": "Two", "tags": ["old"]})

    status, renamed, _ = request(base, "/api/tags/rename", method="POST", payload={"old": "old", "new": "new"})
    assert status == 200
    assert renamed["changed"] == 2
    _, tags, _ = request(base, "/api/tags")
    names = {tag["tag"] for tag in tags["tags"]}
    assert "old" not in names
    assert "new" in names

    status, removed, _ = request(base, "/api/tags/new", method="DELETE")
    assert status == 200
    assert removed["changed"] == 2
    _, tags, _ = request(base, "/api/tags")
    assert "new" not in {tag["tag"] for tag in tags["tags"]}

    assert request(base, "/api/tags/rename", method="POST", payload={"old": "", "new": "x"})[0] == 400
    assert request(base, "/api/tags/rename", method="POST", payload={"old": "x", "new": ""})[0] == 400


def test_inbox_add_and_status_actions(server):
    base, _ = server
    status, created, _ = request(base, "/api/inbox", method="POST", payload={"text": "Capture this"})
    assert status == 201
    assert created["item"]["text"] == "Capture this"
    assert created["item"]["status"] == "open"

    status, listed, _ = request(base, "/api/inbox")
    assert [i["text"] for i in listed["items"]] == ["Capture this"]

    status, done, _ = request(base, "/api/inbox/" + created["item"]["id"], method="PATCH", payload={"status": "done"})
    assert status == 200
    assert done["item"]["status"] == "done"
    assert request(base, "/api/inbox?status=open")[0] == 200
    assert [i["text"] for i in request(base, "/api/inbox?status=open")[1]["items"]] == []

    status, arch, _ = request(base, "/api/inbox/" + created["item"]["id"], method="PATCH", payload={"status": "archived"})
    assert status == 200
    assert arch["item"]["status"] == "archived"


def test_tag_summary_and_sorting(server):
    base, _ = server
    request(base, "/api/notes", method="POST", payload={"title": "Charlie", "tags": ["alpha", "shared"]})
    request(base, "/api/notes", method="POST", payload={"title": "alpha", "tags": ["shared"]})
    request(base, "/api/notes", method="POST", payload={"title": "Bravo", "tags": ["beta"]})

    _, tags, _ = request(base, "/api/tags")
    assert tags["tags"] == [
        {"tag": "shared", "count": 2},
        {"tag": "alpha", "count": 1},
        {"tag": "beta", "count": 1},
    ]

    _, by_title, _ = request(base, "/api/notes?sort=title")
    assert [n["title"] for n in by_title["notes"]] == ["alpha", "Bravo", "Charlie"]

    _, filtered, _ = request(base, "/api/notes?tag=shared")
    assert sorted(n["title"] for n in filtered["notes"]) == ["Charlie", "alpha"]


def test_export_and_import_round_trip(server):
    base, _ = server
    request(base, "/api/notes", method="POST", payload={"title": "Export me", "content": "# body", "tags": ["x"]})
    status, exported, _ = request(base, "/api/export")
    assert status == 200
    assert exported["format"] == "hermes-workspace-v1"
    assert [n["title"] for n in exported["notes"]] == ["Export me"]
    assert exported["notes"][0]["content"] == "# body"
    assert exported["notes"][0]["tags"] == ["x"]

    # Import assigns fresh ids and preserves title/content/tags.
    status, result, _ = request(base, "/api/import", method="POST", payload={
        "notes": [{"title": "Export me", "content": "# body", "tags": ["x"]}],
    })
    assert status == 200
    assert result["imported"] == 1
    _, listed, _ = request(base, "/api/notes")
    assert len(listed["notes"]) == 2
    assert {n["title"] for n in listed["notes"]} == {"Export me"}
    assert len({n["id"] for n in listed["notes"]}) == 2

    # Malformed payloads are rejected, not silently accepted.
    assert request(base, "/api/import", method="POST", payload={"notes": "nope"})[0] == 400


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
