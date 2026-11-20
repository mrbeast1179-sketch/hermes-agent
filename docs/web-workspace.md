# Hermes local browser workspace

`hermes workspace` starts a loopback-only browser workspace with durable
notes, a safe Markdown editor, an inbox, recent session metadata, a bounded
read-only terminal, and system information.

```bash
hermes workspace                      # opens http://127.0.0.1:8787
hermes workspace --no-browser         # print the URL instead of opening it
hermes workspace --port 9000          # choose a port
hermes workspace --root ~/my-workspace # choose the data directory
```

Data lives in `~/.hermes/workspace/workspace.sqlite3` by default (override
with `--root`). The workspace is a separate surface from `hermes webapp`:
`webapp` is the gateway-hosted Desktop, while `workspace` is a small
local-first editor that starts in under a second and needs no gateway.

## Security model

- The server binds to loopback only; a non-loopback host is rejected.
- Every API request requires the bearer token generated for the process.
- Mutations additionally require a same-origin `Origin` header and an
  `X-Workspace-CSRF` header carrying the same token.
- The first page load exchanges `?token=...` for a `HttpOnly; SameSite=Strict`
  cookie so reloads keep working; the token is then removed from the URL and
  is never embedded in the HTML.
- Responses set `nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy:
  no-referrer`, and a restrictive Content-Security-Policy.
- Markdown is rendered by a small safe subset: all source text is HTML
  escaped, and `javascript:`, `data:`, `vbscript:`, and `file:` links are
  replaced with an inert `#` anchor.
- There is no arbitrary file-read endpoint and no arbitrary command
  endpoint. The terminal accepts only `pwd`, `whoami`, `uname -a`, `date`,
  `ls -la`, and `git status --short --branch`, with exact argument vectors.

## Notes API

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/notes?q=&tag=&sort=` | List/search notes (`sort`: `updated`, `created`, `title`) |
| `GET` | `/api/tags` | Tag names with live counts |
| `GET` | `/api/export` | Download a full workspace snapshot (JSON) |
| `POST` | `/api/import` | Import notes from an export payload (new ids are assigned) |
| `GET` | `/api/notes/{id}` | Read one note with rendered HTML |
| `POST` | `/api/notes` | Create a note |
| `PUT` | `/api/notes/{id}` | Update a note (optimistic `version` required) |
| `DELETE` | `/api/notes/{id}` | Move a note to trash (optimistic `version` required) |
| `GET` | `/api/trash` | List trashed notes |
| `POST` | `/api/notes/{id}/restore` | Restore a trashed note |
| `DELETE` | `/api/notes/{id}/purge` | Permanently delete a trashed note |
| `POST` | `/api/notes/{id}/pin` | Pin (`pinned: true`) or unpin a note |
| `POST` | `/api/notes/{id}/duplicate` | Copy a note (content and tags) into a new note |
| `GET` | `/api/notes/{id}/versions` | List every stored version of a note |
| `POST` | `/api/notes/{id}/versions/{version}/restore` | Restore a stored version (as a new version) |
| `POST` | `/api/tags/rename` | Rename a tag across every live note (`old`, `new`) |
| `DELETE` | `/api/tags/{tag}` | Remove a tag from every live note |
| `POST` | `/api/markdown/preview` | Render HTML without saving |

Updates use optimistic concurrency: a stale `version` returns HTTP 409 so a
second tab cannot silently overwrite the first tab's edits.

Pinned notes always lead the list, keep their relative order, and survive
restarts. The editor shows a Pin/Unpin button, and the list marks pinned
notes with a pin.

The note list shows how many notes match the current filter and a one-line
snippet of each note, so notes are distinguishable without opening them.

## Duplicating and organising notes

Duplicate copies the selected note's content and tags into a new note
titled "<title> (copy)" and opens it in the editor.

The Tags button next to the filter renames or removes the selected tag
across every live note. Renaming keeps a note's other tags and never
duplicates a tag; removing strips the tag everywhere. Both report how many
notes changed.

## Importing Markdown files

The Import button accepts a workspace JSON export or a single Markdown or
text file. A `.md`, `.markdown`, or `.txt` file becomes a new note titled
after the file name, with the file contents as the note body, and opens in
the editor.

## Linking and session shortcuts

Selecting a note puts it in the URL (`#note=<id>`), so a note can be
bookmarked or shared and reopening the link restores that note.

The Sessions panel lists recent Hermes sessions with a copyable resume
command (`hermes --resume <id>`), refreshing every 30 seconds while the
panel is open. The System panel reports the workspace data directory
alongside the platform and Hermes home.

## Version history

Every saved version of a note is stored, so an edit can be rolled back.
The History button lists the versions newest first; restoring one applies
its title, content, and tags as a **new** version, so the history itself is
never rewritten and the restore itself can be undone.

## Keyboard shortcuts

| Shortcut | Action |
| --- | --- |
| `⌘S` / `Ctrl+S` | Save the current note |
| `⌘N` / `Ctrl+N` | Start a new note |
| `⌘K` / `Ctrl+K` | Focus the search box |

## Headless backup and restore

```bash
hermes workspace --export backup.json     # write a JSON backup
hermes workspace --export -               # write it to stdout
hermes workspace --import backup.json     # import notes from a backup
```

Both run without starting the server and accept `--root` to pick the
workspace directory. Imports always assign fresh ids, so restoring a
backup never overwrites existing notes. Trashed notes are not part of a
backup; the export command says so when any exist.

## Self-check

```bash
hermes workspace --check
```

Verifies the database opens, the schema has every table, counts the stored
notes/inbox/chat rows, confirms the Markdown renderer escapes unsafe
markup, and confirms a loopback port can be bound. It exits non-zero when
a check fails, so it is usable as a smoke test in scripts.

## Inbox

The Inbox panel captures thoughts with an input and Add button (Enter also
works). Each item can be marked Done or Archived; the list is stored in the
workspace database and included in export. `GET /api/inbox?status=open`
filters by status.

## Finding and moving notes

The notes list has a text search, a tag filter populated from live tag
counts, and a sort selector (recently updated, newest first, title A–Z).
The filter survives re-renders, so a tag stays selected while you edit.

Export writes a `hermes-workspace-v1` JSON snapshot of every note and
inbox item; Import reads that snapshot back, assigning fresh ids so an
import can never overwrite an existing note. Import rejects malformed
payloads with HTTP 400 instead of silently accepting them.

## Deletion and drafts

Deleting a note moves it to trash instead of destroying it. Trashed notes
keep their content and tags, disappear from the normal list, and cannot be
edited until they are restored. The Trash panel restores a note with one
click or purges it permanently.

The editor also autosaves an unsaved draft to `localStorage` for the note
you are editing. If you close the tab or reload before saving, the draft is
restored and the status bar says "Unsaved draft restored"; saving clears
the draft.

## Chat

The chat panel uses Hermes' own provider configuration, so it works with
whatever `hermes model` selected (including OAuth providers) and the
workspace never reads or stores an API key.

`POST /api/chat` calls `agent.auxiliary_client.call_llm` in-process. Spawning
`hermes chat` for this would start a second installation, which Hermes'
shared-profile guard refuses; the in-process router avoids that entirely.

Assistant replies render as safe Markdown (headings, bold, code, lists) in
the chat log; your own messages stay plain text. Rendering is done on the
server by the same escaping renderer the note preview uses, so a reply
containing a script tag shows the text rather than executing it.

Replies stream: the panel renders text as it arrives instead of waiting for
the full answer. While a reply is in flight the Send button is disabled and
a "Hermes is thinking" indicator shows elapsed seconds; on failure the
draft text is restored instead of being lost. The streamed reply is stored
in the conversation exactly like a non-streamed one.

The stream endpoint emits `{"delta": "..."}` events and a final
`{"done": true, "reply": "..."}` event; a provider failure is reported as
an `{"error": "..."}` event and is never stored as an assistant reply.

The conversation is persisted in the same SQLite database, so it survives
restarts and reloads, and it is included in the workspace export. Each turn
sends the last 20 messages to the provider. "Ask about this note" attaches
the current note as context for the next message; Clear history empties the
conversation.

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/chat` | Read the stored conversation |
| `POST` | `/api/chat` | Send a message (`message`, optional `context`) |
| `POST` | `/api/chat/stream` | Same, streamed as Server-Sent Events (`delta` events, then `done`) |
| `DELETE` | `/api/chat` | Clear the conversation |

Optional environment overrides:

| Variable | Effect |
| --- | --- |
| `HERMES_WORKSPACE_CHAT_BACKEND` | `hermes` (default, in-process router), `cli` (spawn `hermes chat -q`), `openai` (OpenAI-compatible HTTP), `auto` (try each in that order) |
| `HERMES_WORKSPACE_MODEL` | Override the model used for chat |
| `OPENAI_API_KEY` / `OPENAI_BASE_URL` | Credentials for the `openai` backend only |

Verified against the live configured provider: a one-line prompt returned
`workspace chat ok` with `backend=hermes-provider`.

## Tests

```bash
pytest tests/test_web_workspace.py
```

The suite covers note CRUD and versioning, search, persistence across
restarts, bearer/CSRF/same-origin enforcement, security headers, cookie
reload authentication, Markdown escaping, the client JavaScript syntax, and
the read-only terminal allowlist.
