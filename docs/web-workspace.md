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
| `GET` | `/api/notes?q=&tag=` | List/search notes |
| `GET` | `/api/notes/{id}` | Read one note with rendered HTML |
| `POST` | `/api/notes` | Create a note |
| `PUT` | `/api/notes/{id}` | Update a note (optimistic `version` required) |
| `DELETE` | `/api/notes/{id}` | Move a note to trash (optimistic `version` required) |
| `GET` | `/api/trash` | List trashed notes |
| `POST` | `/api/notes/{id}/restore` | Restore a trashed note |
| `DELETE` | `/api/notes/{id}/purge` | Permanently delete a trashed note |
| `POST` | `/api/markdown/preview` | Render HTML without saving |

Updates use optimistic concurrency: a stale `version` returns HTTP 409 so a
second tab cannot silently overwrite the first tab's edits.

## Deletion and drafts

Deleting a note moves it to trash instead of destroying it. Trashed notes
keep their content and tags, disappear from the normal list, and cannot be
edited until they are restored. The Trash panel restores a note with one
click or purges it permanently.

The editor also autosaves an unsaved draft to `localStorage` for the note
you are editing. If you close the tab or reload before saving, the draft is
restored and the status bar says "Unsaved draft restored"; saving clears
the draft.

## Tests

```bash
pytest tests/test_web_workspace.py
```

The suite covers note CRUD and versioning, search, persistence across
restarts, bearer/CSRF/same-origin enforcement, security headers, cookie
reload authentication, Markdown escaping, the client JavaScript syntax, and
the read-only terminal allowlist.
