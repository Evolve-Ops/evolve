# Native Google Docs / Sheets MCP Tools — Spec

**Status:** draft (2026-06-01)
**Calibrated against:**

1. The structured error returned by `drive_read_file` (PR [#1969](https://github.com/palace-games/evolve/pull/1969)) when pointed at a Google-native file — operators see a working Drive read for plain text and a polite refusal for `application/vnd.google-apps.document` / `.spreadsheet` / `.presentation` types. The error already names the gap: missing scopes + missing tools.
2. The chooser-modal Workspace card copy at [packages/admin/evolve_admin/web/index.html:6600](packages/admin/evolve_admin/web/index.html:6600) — "full read / write / send via a service account" implicitly promises Google-native reads that the v1 surface can't yet deliver.
3. The pod's recurring "Sam shared a Doc, can the bot summarize it?" use case — the most common Workspace-bot ask after Gmail/Calendar, and the most common gap operators hit on day two.

**Companion docs:**

- [docs/spec-google-integration-paths-2026-05-30.md](spec-google-integration-paths-2026-05-30.md) — umbrella spec (this one extends §6 scope catalog + §7 wizard flow)
- [docs/spec-google-drive-scope-2026-06-01.md](spec-google-drive-scope-2026-06-01.md) — sibling spec from earlier today; covers the `drive.file` ↔ `drive` upgrade for shared-folder writes. Distinct surface from native Docs/Sheets but the wizard scope-picker work overlaps.
- [docs/runbook-path-c-google-integration.md](runbook-path-c-google-integration.md) — operator runbook; this spec adds §1.5.a "Adding scopes later" and bumps the §1.5 conservative scope set.
- [packages/admin/evolve_admin/mcp_bridge/google_tools.py](packages/admin/evolve_admin/mcp_bridge/google_tools.py) — host file for the new tool handlers.
- [packages/admin/evolve_admin/web/wizard_google_routes.py:88](packages/admin/evolve_admin/web/wizard_google_routes.py:88) — `SCOPE_CATALOG` host.
- [packages/admin/evolve_admin/google_preflight.py:91](packages/admin/evolve_admin/google_preflight.py:91) — `PREFLIGHT_PROBES` host.

Stock names: **`lex`** (bot id), **`example-corp.com`** (Workspace), **`Sam`** (primary user).

---

## 0. Purpose

PR [#1969](https://github.com/palace-games/evolve/pull/1969) closed the "bot can send but can't read" gap for plain Drive files, Gmail, and Calendar. It deliberately left a structured error for Google-native MIME types — the file ids list cleanly via `drive_list_files`, the metadata fetch works, but the content fetch returns:

> Google-native files (`application/vnd.google-apps.document`) cannot be read via this tool yet. Native Docs/Sheets/Slides reads need the `documents`/`spreadsheets`/`presentations` scopes plus their own MCP tools. Use the web link for now.

This spec defines those tools.

Calibrating use cases (all observed asks against deployed bots, ordered by frequency):

1. **"Lex, summarize the agenda Sam shared with you."** — Docs read, plain text fidelity is fine
2. **"Lex, log this expense to the household sheet."** — Sheets append, single row
3. **"Lex, what tabs are in this workbook?"** — Sheets discovery, then targeted read
4. **"Lex, swap the `<draft>` placeholders in this doc with the real values."** — Docs find/replace
5. **"Lex, clear last quarter's data so I can paste the new export."** — Sheets clear

No calibrating use case for Slides has shown up in two months of observation. Slides is deferred (§7).

---

## 1. Design decisions (resolved)

Six decisions; each picks an option, names what's tradeable, and references the open-question section below for anything still in flux.

### 1.1 Read-only via export vs native API

**Decision: both, sequenced.** Phase D.1 ships export-as-text (one tool, no new scopes); phases D.3–D.4 add native API tools (new scopes, write-capable).

| Path | Scope needed | What it gives | What it costs |
|---|---|---|---|
| `files.export` (Drive API) | `drive.file` (already default) | Plain text or CSV of any Google-native file | Lossy: Sheets export = first sheet only as CSV; Docs export loses structure |
| Native Docs/Sheets API | `documents.readonly` + `spreadsheets.readonly` (new) | Structured reads, range-scoped Sheets queries, writes | New scope catalog entries; new wizard copy; per-operator opt-in for writes |

Shipping export first is a deliberate ~50-line PR. It closes the operator-visible error path from #1969 within one merge cycle, with zero scope expansion. It does not preempt or compete with the native tools — the two coexist with documented choose-which guidance (§10 OQ-3).

### 1.2 Sheets surface

**Decision: five tools, all A1-notation-based.**

```
sheets_list_sheets(spreadsheet_id) → {sheets: [{title, sheet_id, row_count, col_count}]}
sheets_get_values(spreadsheet_id, range)          → {range, values: [[...]]}
sheets_update_values(spreadsheet_id, range, values, value_input_option?)
                                                  → {updated_range, updated_cells, updated_rows, updated_columns}
sheets_append_row(spreadsheet_id, sheet_name, values, value_input_option?)
                                                  → {updated_range, updates}
sheets_clear_range(spreadsheet_id, range)         → {cleared_range}
```

A1 notation (`"Sheet1!A1:C10"`, `"Sheet1"`) is ugly but it's the universal Sheets API surface — column-index alternatives assume the bot already knows the sheet structure, which it usually doesn't. `sheets_list_sheets` is the discovery primitive that makes A1 usable: the bot calls it first, sees `[{title: "Expenses", ...}, {title: "Budget", ...}]`, then constructs the right A1 ref.

`sheets_append_row` is a 1D convenience over `append`. Common case: log a row. The bot doesn't have to construct a 2D array for a 1-row write.

`value_input_option` defaults to `USER_ENTERED` (Google interprets formulas + dates + currency) with caller override for `RAW`. See OQ-2 in §10 — the default matches every "log this for me" use case observed, but it's not free of surprises.

Sheet create / format / chart APIs deferred; no calibrating use case (§7).

### 1.3 Docs surface

**Decision: three text-only tools.**

```
docs_read(document_id, max_chars=200_000) → {title, content, truncated}
docs_append_text(document_id, text)       → {revision_id}
docs_replace_text(document_id, find, replace, match_case=False)
                                          → {occurrences_changed, revision_id}
```

`docs_read` walks the Docs API's structured tree but serializes to plain text — headings prefixed with blank lines, list items as `- ` bullets, tables flattened to tab-separated rows. Best-effort fidelity, no structural metadata returned to the bot. If a bot needs the structured tree, that's a v1.1 ask once a use case shows up.

`docs_replace_text` uses `replaceAllText` (Google's primitive). Default replaces every occurrence (matches LLM intent every time it's been observed); first-only mode deferred. Returns `occurrences_changed` so the bot can confirm "no matches" vs "23 matches replaced."

Structured surfaces (insert-at-heading, image insert, formatting, comments) deferred. The calibrating LLM-driven use cases are all text-shaped.

### 1.4 Slides

**Decision: skip in v1.** No calibrating use case. Do not add `presentations` to `SCOPE_CATALOG`, do not advertise Slides in chooser copy, do not write tool stubs.

**Revisit criterion:** a deployed bot's operator asks for it AND there is a specific, testable read or write the bot needs (not "in case someday"). When that lands, the addition is a single PR mirroring this spec's Docs surface: `slides_read`, possibly `slides_replace_text`. Documented here so the omission isn't relitigated every six weeks.

### 1.5 Scope catalog additions

**Decision: four entries.** No `presentations` (per 1.4).

```python
{"id": "https://www.googleapis.com/auth/documents",
 "label": "Docs (read + write)",
 "description": "Read and edit Google Docs the bot has access to.",
 "audience": "internal+external", "default_set": False, "high_privilege": False},

{"id": "https://www.googleapis.com/auth/documents.readonly",
 "label": "Docs (read-only)",
 "description": "Read Google Docs the bot has access to.",
 "audience": "internal+external", "default_set": True, "high_privilege": False},

{"id": "https://www.googleapis.com/auth/spreadsheets",
 "label": "Sheets (read + write)",
 "description": "Read and edit Google Sheets the bot has access to.",
 "audience": "internal+external", "default_set": False, "high_privilege": False},

{"id": "https://www.googleapis.com/auth/spreadsheets.readonly",
 "label": "Sheets (read-only)",
 "description": "Read Google Sheets the bot has access to.",
 "audience": "internal+external", "default_set": True, "high_privilege": False},
```

`high_privilege=False` for all four: these scopes are bounded by the same drive.file access set that already gates plain-text Drive reads. Reading a shared Doc is not categorically more dangerous than reading a shared `.txt`. Writes are off-by-default (not high-privilege) because the wizard's scope picker is the right friction point, not a confirmation modal.

### 1.6 Default-set membership

**Decision (recommendation, gated by pod-admin confirmation — OQ-1): `documents.readonly` + `spreadsheets.readonly` in the default-set; write scopes off by default.**

Argument:

- `drive.file` is already in the default-set. It caps the *file set* the bot can access — to bot-created files plus files explicitly shared with the bot's subject. Adding `documents.readonly` + `spreadsheets.readonly` does **not** broaden the set of files; it just lets the bot read the content of Google-native files in that same set.
- Today's chooser-modal Workspace card promises "list + read + write Drive files." For Google-native types, that promise is broken — `drive_list_files` finds them, `drive_read_file` refuses. Closing this gap with default scopes restores the promise.
- Principle of least privilege still satisfied: a bot can list a Doc shared with it (drive.file), but reading content requires the readonly scope as a second gate. Writes require a third gate.

Counter-argument worth airing: Docs and Sheets are often categorically more sensitive than plain text (business docs, financial sheets). An operator who installs a "personal assistant" bot may not expect "read every business spreadsheet shared with you" by default.

Mitigation if pod-admin pushes back: ship readonly scopes off-by-default; let the wizard surface them as the highest-priority opt-in. Cost is one extra wizard click per Workspace bot. Decide before D.2 lands.

---

## 2. Phase D.1 — `drive_export_as_text` (ships first, no spec gate)

Single new MCP tool. Reuses `drive.file` scope (already in default-set). No `SCOPE_CATALOG` changes, no wizard copy, no chooser-modal copy. The minimum-viable diff that closes the structured-error path from PR #1969.

```python
async def tool_drive_export_as_text(arguments: dict, registry: BotRegistry) -> dict:
    """Export a Google-native Drive file to plain text or CSV.

    Arguments:
        bot: optional bot id
        id: Drive file id, required
        format: optional override of the default per-type format:
                "text/plain", "text/csv", "text/html", "application/rtf"
        max_bytes: int, default 1 MiB; hard cap 10 MiB
    """
```

Per-MIME-type defaults:

| Source MIME | Default export MIME | Lossiness |
|---|---|---|
| `application/vnd.google-apps.document` | `text/plain` | Loses headings, links, tables, embedded images |
| `application/vnd.google-apps.spreadsheet` | `text/csv` | **First sheet only**; loses formulas (CSV gets computed values) |
| `application/vnd.google-apps.presentation` | `text/plain` | Loses everything except speaker-notes-ish text |
| anything else | passthrough error | "not a Google-native type; use drive_read_file" |

Return shape:

```python
{"ok": True, "bot": bot_id, "id": ..., "name": ..., "source_mime_type": ...,
 "export_mime_type": "text/plain", "content": "...", "truncated": False,
 "web_view_link": ...}
```

**Why ship this before the spec is fully reviewed:** ~50 lines, no scope expansion, no operator-visible decisions. Replaces the dead-end error from #1969 with a working path. Buys design time for D.3 / D.4 without leaving operators stuck.

Limitations the tool's docstring + chooser-modal copy must surface:

- Sheets export to CSV exports the **first sheet only**. Multi-sheet reads need `sheets_get_values` (D.3) or repeated calls per `gid` (workable but ugly).
- Docs export to text drops structure. Structured reads need `docs_read` (D.4).
- Slides export is functional but rarely useful (mostly title + body text per slide concatenated).

Tests: mirror the `drive_read_file` test set — each MIME type's default + override path, missing-id, max_bytes truncation, "not a Google-native type" rejection. New entry in `mcp_bridge/server.py:_TOOLS` with `inputSchema`.

---

## 3. Phase D.2 — Scope catalog + preflight

Lands AFTER D.1 and BEFORE D.3/D.4 (the native tools assume the scopes are catalogued).

### 3.1 `SCOPE_CATALOG` entries

Four new dict entries per §1.5, inserted in `packages/admin/evolve_admin/web/wizard_google_routes.py:88` after the Drive block, before `contacts.readonly`. Order in the picker UI: read-only first, write second (matches the existing Calendar/Drive ordering).

### 3.2 Preflight probes — none added

The four new scopes are expected to ride alongside `drive.*` in any real deployment (a bot with `documents.readonly` but no Drive scope cannot discover documents to read). The existing `drive.about.get` probe at [google_preflight.py:131](packages/admin/evolve_admin/google_preflight.py:131) already covers these flows.

If an operator somehow grants ONLY `documents.readonly` (no Drive scope), `pick_preflight_probe` returns `None` and the wizard surfaces its existing skip message. Documented edge case, not handled.

The Docs/Sheets APIs lack cheap liveness probes — no `documents.list()`, no `spreadsheets.list()`. Drive's `files.list(q="mimeType='...'")` is the canonical discovery path. Trying to engineer a Docs-API-only probe (e.g. `documents.get(documentId=<known-id>)`) would require a per-Workspace seed document, which is more friction than the bug it would catch.

### 3.3 Runbook updates

Two changes to [docs/runbook-path-c-google-integration.md](docs/runbook-path-c-google-integration.md):

1. **§1.5 conservative scope set bump.** Add `documents.readonly` + `spreadsheets.readonly` to the comma-separated default DwD authorization list. Existing operators who follow the runbook get the new default; in-place operators who don't update the DwD authorization see a clean 403 from `sheets_get_values` and fall through to the existing scope-mismatch remediation hint.
2. **New §1.5.a "Adding scopes later."** Step-by-step: Workspace Admin → API Controls → Domain-wide Delegation → click the existing client ID → Edit → append scope → Save → wait ~30s for propagation. Referenced from the wizard's scope picker as the "how do I authorize this?" target. Renumber existing §6 (References) to §7.

---

## 4. Phase D.3 — Sheets MCP tools

Five tool handlers added to `mcp_bridge/google_tools.py`, registered in `TOOL_HANDLERS` with appropriate `is_write_op`, declared in `mcp_bridge/server.py:_TOOLS` with `inputSchema`.

### 4.1 Signatures + Google API mapping

| MCP tool | Scope required | Underlying Google call | `is_write_op` |
|---|---|---|---|
| `sheets_list_sheets(spreadsheet_id)` | `spreadsheets.readonly` | `spreadsheets().get(spreadsheetId, fields="properties.title,sheets.properties")` (no grid data) | false |
| `sheets_get_values(spreadsheet_id, range)` | `spreadsheets.readonly` | `spreadsheets().values().get(spreadsheetId, range)` | false |
| `sheets_update_values(spreadsheet_id, range, values, value_input_option?)` | `spreadsheets` | `spreadsheets().values().update(spreadsheetId, range, valueInputOption=..., body={"values": values})` | **true** |
| `sheets_append_row(spreadsheet_id, sheet_name, values, value_input_option?)` | `spreadsheets` | `spreadsheets().values().append(spreadsheetId, range=sheet_name, valueInputOption=..., insertDataOption="INSERT_ROWS", body={"values": [values]})` | **true** |
| `sheets_clear_range(spreadsheet_id, range)` | `spreadsheets` | `spreadsheets().values().clear(spreadsheetId, range, body={})` | **true** |

`value_input_option` defaults to `"USER_ENTERED"`. Caller passes `"RAW"` for verbatim storage (skips formula/date/currency interpretation). See OQ-2.

### 4.2 Error mapping (all Sheets tools)

| Google response | MCP return |
|---|---|
| 200 OK | `{"ok": True, ...}` with tool-specific payload |
| 404 Not Found | `{"ok": False, "error": "spreadsheet <id> not visible to this bot. Make sure Sam shared it with lex@example-corp.com (Editor for write tools, Viewer for read)."}` |
| 403 PERMISSION_DENIED (scope) | `{"ok": False, "error": "this bot doesn't have the spreadsheets scope. Add it in the wizard → Workspace → Scope picker, then re-run `evolve-admin deploy lex`."}` |
| 400 INVALID_ARGUMENT (range) | `{"ok": False, "error": "range \"<range>\" is not valid A1 notation. Examples: \"Sheet1\", \"Sheet1!A1:C10\", \"'My Sheet'!A:A\"."}` |

The "missing scope" error string interpolates the bot's specific id + subject when available so the operator can copy-paste the remediation. Pattern matches the [hint catalog in google_preflight.py:171](packages/admin/evolve_admin/google_preflight.py:171).

### 4.3 Caveats documented in the per-tool docstring

- `sheets_get_values` on an empty cell range returns `{"values": []}`, not `[[]]` or `[[""]]`. Caller checks `len(values)`.
- `sheets_append_row` returns the `updated_range` Google assigned, which may differ from `sheet_name` (Google picks the next empty row).
- `sheets_clear_range` clears values AND formulas but preserves cell formatting. Documented because the alternative (full `batchUpdate` with delete-dimension) is heavier.
- `sheets_update_values` with `values=[[]]` (zero rows) is a no-op; caller is expected to not call it in that case.

---

## 5. Phase D.4 — Docs MCP tools

Three tool handlers in `mcp_bridge/google_tools.py`.

### 5.1 Signatures + Google API mapping

| MCP tool | Scope required | Underlying Google call | `is_write_op` |
|---|---|---|---|
| `docs_read(document_id, max_chars=200_000)` | `documents.readonly` | `documents().get(documentId)` → walk `body.content` | false |
| `docs_append_text(document_id, text)` | `documents` | `documents().batchUpdate(documentId, body={"requests": [{"insertText": {"endOfSegmentLocation": {}, "text": text + "\n"}}]})` | **true** |
| `docs_replace_text(document_id, find, replace, match_case=False)` | `documents` | `documents().batchUpdate(documentId, body={"requests": [{"replaceAllText": {"containsText": {"text": find, "matchCase": match_case}, "replaceText": replace}}]})` | **true** |

### 5.2 Text serialization in `docs_read`

The Docs API returns a structured tree (`body.content` is a list of `StructuralElement`). `docs_read` walks it and emits plain text with light structural cues:

- `paragraph` with `paragraphStyle.namedStyleType` in `{HEADING_1..6}` → prefixed with blank line + Markdown hashes (`# `, `## `, etc.)
- `paragraph` with `bullet` → prefixed with `- `
- `paragraph` plain → newline-separated
- `table` → rows tab-separated, lines blank-separated, prefixed/suffixed by blank line
- `tableOfContents`, `sectionBreak`, `pageBreak` → skipped
- `inlineObjectElement` (images) → emitted as `[image]` placeholder

`max_chars` truncates at the rendered text level (not API level). Returns `truncated=True` with the cut location.

Tradeoff: this is a lightweight reimplementation of "Doc → text" that an exporter (D.1 `drive_export_as_text` with `format="text/plain"`) would do server-side. `docs_read` is preferred when:

- The bot has `documents.readonly` but not `drive.file` (rare but legal)
- The bot wants per-section access (future structured surface; D.4 doesn't expose it yet but the implementation is positioned to)
- The export endpoint's plain-text rendering differs from Docs's native walk (Google occasionally tweaks the exporter)

Both tools coexist; the chooser-modal copy points operators at `drive_export_as_text` as the cheap path and `docs_read` as the rich path. See OQ-3.

### 5.3 `docs_replace_text` semantics

`replaceAllText` is Google's primitive — replaces every occurrence in one call, atomic, returns `occurrencesChanged` in the response. Caller intent is almost always "replace all" (LLMs match what they see; if they want first-only, they should make the find string unique).

First-only mode is **not** added in v1. Adding it requires the caller to either pre-walk the document tree or use the structured `replaceRange` request — both add 2× the complexity for an unverified need. Punt to v1.1 if a use case shows up.

Return shape:

```python
{"ok": True, "bot": ..., "document_id": ..., "occurrences_changed": 3,
 "revision_id": "<google-internal>"}
```

When `occurrences_changed == 0`, the bot should treat it as a soft no-op (the find string didn't match), not an error. The MCP layer returns `ok=True` to preserve that.

### 5.4 Error mapping

Same shape as Sheets (§4.2) — 404 for share-with-bot remediation, 403 for scope, 400 for malformed args.

---

## 6. Phase D.5 — Wizard + chooser modal + runbook copy

Copy-only PR; lands last. Reflects what actually shipped in D.1–D.4.

### 6.1 Chooser modal Workspace card ([index.html:6600](packages/admin/evolve_admin/web/index.html:6600))

Update the capability paragraph to mention Google-native types explicitly:

> Paid tenant ($6+/user/month). Full read / write / send via a service account + domain-wide delegation. Read Gmail; create + list Calendar events; list + read + write Drive files including **Google Docs and Sheets**. The most reliable Google integration — no token-refresh cycles, no consent-screen drift.

Write access to Docs/Sheets requires the operator to toggle the write scopes in the wizard's scope picker (per §1.6); the card stays honest by saying "read + write Drive files including Google Docs and Sheets" without claiming the writes are on by default.

### 6.2 Path-C wizard subtitle

Mirror the same capability summary in the wizard Screen 2 subtitle text (the line that lists what the default scope set unlocks). Add a one-line pointer to the scope picker:

> Need Docs/Sheets write, Gmail modify, contacts, or full Drive read? Toggle them in the scope picker below.

### 6.3 Personal-account chooser card

Personal account ([index.html:6614](packages/admin/evolve_admin/web/index.html:6614)) still says "Read-only today" — leave unchanged. Personal-Gmail path (path A) is its own multi-PR effort per umbrella spec §1; Docs/Sheets there ride on top of whatever auth path A ships with.

### 6.4 Runbook §1.5 (conservative scope set)

Bump the comma-separated default to:

```
https://www.googleapis.com/auth/gmail.send,
https://www.googleapis.com/auth/gmail.readonly,
https://www.googleapis.com/auth/calendar,
https://www.googleapis.com/auth/drive.file,
https://www.googleapis.com/auth/documents.readonly,
https://www.googleapis.com/auth/spreadsheets.readonly
```

(Real string is comma-separated no whitespace per Google Admin's parser; the formatted block above is for readability.)

### 6.5 Runbook §1.5.a (new)

"Adding scopes later" subsection per §3.3 above. Single screen path. Referenced from the wizard scope picker's "how do I authorize this?" hover.

---

## 7. Deferred to v1.1 (explicitly named, not silently dropped)

These are documented here so the next sprint's planning has them at hand:

1. **Slides** (`presentations` / `presentations.readonly`, `slides_read`, optional `slides_replace_text`) — revisit criterion: a deployed bot's operator asks AND a specific read/write is named.
2. **`sheets_create(title, sheet_names)` / `docs_create(title, content)`** — bots creating files. `drive_write_file` covers non-native files; native create is a different API. Use case: "Lex, make me a sheet to track X" — observed but rare in v1.
3. **Sheets formatting** (cell colors, conditional formatting, charts) — never asked.
4. **Docs structured surface** (insert at heading, image insert, comments, suggestion mode) — `docs_replace_text` covers the LLM's primary mutation pattern.
5. **`calendar_list_calendars` / `drive_list_shared_drives`** — cheap follow-up; both ride on existing scopes. File as a separate ~30-line PR (no spec needed) once the D.1–D.5 sprint is done. Tracked in §11.
6. **First-only `docs_replace_text` mode** — defer until a real use case names it.

---

## 8. Test plan

Mirror the patterns in [tests/test_mcp_google_tools.py](packages/admin/tests/test_mcp_google_tools.py):

### 8.1 Per-tool tests

Each new tool gets:

- Happy-path test with the Google API mocked (`MagicMock` returning canned response shapes)
- Missing-required-argument test (e.g. `sheets_get_values()` without `range`)
- 403 PERMISSION_DENIED → structured `ok=False` with scope-picker pointer
- 404 Not Found → structured `ok=False` with share-with-bot remediation
- 400 INVALID_ARGUMENT (Sheets only) → structured `ok=False` with A1 syntax pointer

### 8.2 MCP `_TOOLS` regression guard

The existing `test_mcp_server_lists_all_google_tools` (added in PR #1969) asserts every `TOOL_HANDLERS` entry has a matching `_TOOLS` entry with `inputSchema`. Stays green.

Count: D.1 adds 1, D.3 adds 5, D.4 adds 3 → 9 net new tools. After D.5, the Google surface is 17 tools (3 original write + 5 PR-#1969 read + 9 new = 17).

### 8.3 Specific scenarios worth named tests

- **`drive_export_as_text` with explicit format override.** Operator wants HTML, not plain text.
- **`sheets_get_values` empty range.** Returns `{values: []}`, not crashes on `KeyError`.
- **`sheets_append_row` updated_range divergence.** Google picks the next empty row; the test asserts the response carries it back.
- **`sheets_update_values` with `value_input_option="RAW"`.** Formula string stored verbatim, not evaluated.
- **`sheets_clear_range` preserves formatting.** Test asserts only `values().clear()` was called, not `batchUpdate` with `deleteRange`.
- **`docs_read` table serialization.** Single-cell table emits tab-separated correctly.
- **`docs_replace_text` with zero matches.** `occurrences_changed=0`, `ok=True` (soft no-op).
- **`is_write_op` flags.** Tabular test asserts the right tools are write-tagged so MCP's write-tool gating works.

### 8.4 The `JS parses clean` regression guard

`test_index_html_js_parses` (PR #1912) stays green through D.5's copy edits. Worth running locally before commit since the chooser-modal block is dense.

---

## 9. PR plan

Five PRs. D.1 is independent and ships now; D.2 gates D.3/D.4; D.5 is the copy wrap.

| PR | Scope | Approx LOC | Depends on |
|---|---|---|---|
| **D.1** | `drive_export_as_text` tool + tests + `_TOOLS` entry. Chooser-card copy unchanged (export is invisible to operators — they just see Google-native reads start working). | ~50 prod + ~80 test | nothing |
| **D.2** | `SCOPE_CATALOG` four new entries; runbook §1.5 bump + new §1.5.a; tests for the catalog endpoint listing new entries. Default-set inclusion of `*.readonly` scopes (OQ-1 gated). | ~80 prod + ~40 test + runbook | D.1 ideally merged so error path is closed during review |
| **D.3** | Five Sheets tools + tests + `_TOOLS` entries. | ~250 prod + ~180 test | D.2 |
| **D.4** | Three Docs tools + tests + `_TOOLS` entries. | ~200 prod + ~120 test | D.2 (parallel to D.3) |
| **D.5** | Chooser-modal Workspace card copy bump; path-C wizard subtitle bump; runbook §1.5 final pass. | ~30 prod + 0 test (JS-parse regression suffices) | D.3 + D.4 (so the copy claims match shipped tools) |

D.3 and D.4 can land in either order or in parallel. D.5 needs both.

After-merge ops (paste into each PR description):

```bash
ssh admin-user@mini "sudo /bin/launchctl kickstart -k system/ai.evolve.evolve.admin-ui"
ssh admin-user@mini "sudo /bin/launchctl kickstart -k system/ai.evolve.evolve.mcp-bridge"
```

For D.2: operators with active Workspace DwD setups need to manually add the new scopes to their Workspace Admin DwD authorization. The runbook §1.5.a flow covers it; the wizard scope picker also links to it. Existing bots whose scopes don't include the new defaults are NOT auto-upgraded (operator-visible config edit).

---

## 10. Open questions

1. **Default-set inclusion of `*.readonly` Docs/Sheets scopes.** §1.6 recommends yes; alternative is off-by-default with the scope picker as the opt-in. Pod-admin confirms before D.2 ships. **Recommendation: yes (in default-set).**

2. **`value_input_option` default for Sheets writes.** §1.2 picks `USER_ENTERED` (Google interprets formulas, dates, currency). Alternative: `RAW` (stores literally). USER_ENTERED matches every "log this for me" use case observed but means a bot writing `"=A1+B1"` creates a formula instead of a literal string. Caller can override per-call. **Recommendation: USER_ENTERED default, caller override.**

3. **`drive_export_as_text` vs `sheets_get_values` / `docs_read` overlap.** Both can read a Doc / Sheet. Bots will be confused which to call. **Recommendation: tool docstrings name the rule — "export = first-sheet CSV / lossy text, cheap (`drive.file` only); native = ranged / structured, costlier scope." Chooser-modal copy doesn't need to surface this; it's a code-path-level detail bots resolve via docstrings.**

4. **`docs_replace_text` write scope.** The `documents` scope is required for `batchUpdate`. If a bot has only `documents.readonly`, the call 403s. **Recommendation: 403 handler returns the same scope-picker pointer as Sheets writes (§4.2). Bot-author sees the structured error and fixes their config.**

5. **What about Office-format files (`.docx`, `.xlsx`)?** `drive_read_file` (PR #1969) already handles them as binary base64. `.docx` and `.xlsx` are common-enough Drive content that "read this `.xlsx`" is a real ask. **Recommendation: out of scope here. A separate `drive_extract_text` tool (calls `files.export` with target MIME `text/plain` regardless of source) could cover both Google-native and Office formats, but it expands the API surface beyond the gap this spec addresses. File as a follow-up if a use case names it.**

6. **Drive Picker UX vs native Sheets/Docs share-flow guidance.** §3.2's "share with `lex@example-corp.com`" remediation assumes the operator knows how to share a file in Drive. The wizard could surface a copy-paste-able email address for the bot's subject. **Recommendation: chooser-modal copy already says "share with the bot's mailbox" implicitly; explicit one-click-copy is a v1.1 polish.**

---

## 11. Out of scope

- **Slides** (per §1.4 + §7) — explicit decision, criteria to revisit documented.
- **`calendar_list_calendars` / `drive_list_shared_drives`** — cheap, no new scopes, no spec needed. Track as a fast-follow PR after D.5. Maybe ~40 LOC + tests; not worth fighting for spec inclusion.
- **Path-A / Path-B coverage of the new scopes.** The catalog entries work regardless of auth path; the actual tools route through `google_auth.load_credentials` which today raises `NotImplementedError` for paths A/B (per [mcp_bridge/google_tools.py:3](packages/admin/evolve_admin/mcp_bridge/google_tools.py:3) module docstring). When path-A lands as its own spec, the Docs/Sheets tools work automatically.
- **Sheets create / Docs create.** Native file creation needs a different API call than `drive_write_file`. Use case observed but rare; defer until v1.1.
- **Multi-document batch tools.** "Update 50 sheets at once." Compose at the bot layer; the MCP surface stays one-call-per-target.
- **Office formats (.docx, .xlsx) extraction.** Covered by `drive_read_file` as binary; structured text extraction is OQ-5.
- **Comments / suggestion-mode** for Docs. Not asked.
- **Drive activity / revisions / version history.** Not asked.

---

## 12. References

- External: [Google Docs API — batchUpdate / replaceAllText](https://developers.google.com/docs/api/reference/rest/v1/documents/batchUpdate)
- External: [Google Sheets API — spreadsheets.values](https://developers.google.com/sheets/api/reference/rest/v4/spreadsheets.values)
- External: [Google Drive API — files.export](https://developers.google.com/drive/api/reference/rest/v3/files/export)
- External: [Google OAuth 2.0 scopes](https://developers.google.com/identity/protocols/oauth2/scopes) — full Docs/Sheets/Slides scope list
- Internal: [docs/spec-google-integration-paths-2026-05-30.md](spec-google-integration-paths-2026-05-30.md) §6 (scope catalog) + §7 (wizard) — this spec extends both
- Internal: [docs/spec-google-drive-scope-2026-06-01.md](spec-google-drive-scope-2026-06-01.md) — sibling spec on `drive.file` ↔ `drive` upgrade (the wizard scope picker is a shared touch point)
- Internal: [docs/runbook-path-c-google-integration.md](runbook-path-c-google-integration.md) §1.5 + §1.5.a (new)
- Internal: PR [#1969](https://github.com/palace-games/evolve/pull/1969) — the read/list tools that surfaced the gap this spec closes
