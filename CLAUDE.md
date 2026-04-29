# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repo intent

Two distinct terminal tools share this repo on purpose. They do not import from each other and should not be merged.

- **`qs`** — Bash. Markdown search UI over a `qmd` knowledge base. Single-file shell wrapper.
- **`ccs.py`** — Python (stdlib only). Multi-source coding-agent session browser (Claude Code, Codex, Pi, Opencode).

If a change starts blurring the boundary (e.g. adding session-browser logic to `qs`, or markdown KB search to `ccs`), stop and reconsider — the README's intro explicitly calls out the separation.

## Running

No build, no test suite, no lint config. Both tools are scripts run directly.

```bash
./qs                           # browse markdown
./qs <terms>                   # hybrid search (BM25 + vector)
./qs -f <terms>                # BM25 only

./ccs.py                       # current cwd, all available sources
./ccs.py <query>               # pre-fill fzf interactive filter
./ccs.py --all                 # scan every cwd
./ccs.py --source claude       # restrict to one tool (repeat to combine)
./ccs.py --session <path>      # open JSONL or opencode://<id> directly
```

External dependencies:
- `qs`: `qmd`, `fzf`, `glow`, `bat`, optional `fd`. Clipboard via `pbcopy`/`wl-copy`/`xclip`/`xsel`.
- `ccs.py`: `fzf`, optional `bat` (falls back to `$PAGER`/`less`). Stdlib `sqlite3` for Opencode.

## ccs.py architecture

Reading `ccs.py` end-to-end is reasonable, but two patterns are non-obvious from any single function:

### Adapter pattern (vendored)

`Adapter` subclasses (`ClaudeAdapter`, `CodexAdapter`, `PiAdapter`, `OpencodeAdapter`) each own their tool's storage format. Discovery yields `SessionMeta` records; `messages(locator)` yields `MessageRecord` records. The `ADAPTERS` registry maps tool name → class, and `adapter_for_tool()` instantiates on demand.

The adapter classes are **vendored from `~/.agents/skills/recall/scripts/recall-day.py`**, not imported. Lifecycle decoupling is intentional — that file is managed by an external skills deployer. When upstream gains capability worth pulling in, re-vendor by hand; do not introduce an import path or `sys.path` hack to that location.

### Locator as a string

`SessionMeta.locator` is a plain `str` — file path for JSONL adapters, `opencode://<session_id>` for Opencode. Every code path that consumes a locator (preview, message extraction, direct-open via `--session`) treats it as opaque and lets the adapter parse it. Do not generalize to `Path` or a `(scheme, value)` dataclass — that breaks Opencode and adds boilerplate.

### fzf hidden-column dispatch

`session_line()` emits 9 tab-delimited columns. fzf is launched with `--with-nth 4..` (display from col 4) but the returned selection still contains all 9. Cols 1–3 carry `tool` / `locator` / `session_id_full` for dispatch; the visible cols are formatted for readability and substring filtering. `_self_invoke()` is how `--preview-session` and `--preview-message` re-enter the same script — fzf preview runs as a fresh process with no module state.

If you change the column layout, update both `session_line()` *and* the `--with-nth` value, and verify `{1}`/`{2}` placeholders in the preview command still point at the right field.

### Search is fzf, not an index

The README and PLAN.md are explicit: substring-on-rich-row via fzf is the search experience. Do not add a BM25 layer, do not pre-scan message bodies into a search index, do not add an `--search` flag that does content-grep across all sessions. If filtering on title/cwd/tool/timestamp is insufficient for a real use case, surface the gap before building infrastructure.

### Two-tier UX

Session picker → message picker → bat pager. The previous (pre-2026-04-28) version had a three-tier project-first drill-down; that was removed because Claude/Codex/Pi/Opencode each define "project" differently. cwd is now a column on the session row, not a separate stage. Keep it that way.

## qs architecture

Single-file Bash, ~225 lines. The non-obvious bit:

- **Self-invocation for fzf bindings.** `qs` calls itself with hidden subcommands (`__preview-file`, `__preview-uri`, `__copy-path`, `__copy-result`, `__edit-path`, `__edit-result`) inside fzf's `--bind` and `--preview` strings. This avoids the shell-quoting quagmire that broke the earlier ccs Bash prototype (see `PLAN.md` history). Do not "simplify" by inlining shell into the binding strings.
- **`qmd` URIs vs paths.** Search results carry both. Copy/edit prefer the resolved file path; preview/render uses `qmd get` for URIs. `collection_root()` caches `qmd collection show` lookups to avoid repeating the call per result.

## Verifying changes to `ccs.py`

There is no test suite. After non-trivial edits, run this in-process probe — it catches adapter regressions without needing fzf:

```bash
python3 -c "
import ccs
for name, cls in ccs.ADAPTERS.items():
    a = cls()
    if not a.available():
        print(f'{name}: skipped'); continue
    sessions = list(a.discover(cwd_filter=None, all_projects=True))
    print(f'{name}: {len(sessions)} sessions')
    if sessions:
        s = next((x for x in sessions if x.user_msg_count >= 2), sessions[0])
        msgs = a.messages(s.locator)
        print(f'  first session {s.session_id} → {len(msgs)} messages')
"
```

For preview round-trip (still without fzf):

```bash
python3 ccs.py --preview-session claude <path/to/some.jsonl> | head
python3 ccs.py --preview-message claude <path> <index>
```

The actual fzf TUI cannot be exercised non-interactively. Final sign-off requires a real terminal.

## Storage roots (env-overridable)

`ccs.py` reads from `~/.claude/projects`, `~/.codex/sessions`, `~/.pi/agent/sessions`, `~/.local/share/opencode/opencode.db`. Override via `CLAUDE_PROJECTS_DIR`, `CODEX_HOME`, `PI_SESSIONS_DIR`, `OPENCODE_DB`. Adapters auto-skip when their storage is missing — don't add availability flags or config files.

## Known v1 limitations (do not "fix" without discussion)

- Preview re-parses the full JSONL on every fzf highlight. Acceptable; an LRU cache or message-index sidecar is a real refactor, not a quick patch.
- Codex uses cwd prefix-match; Claude/Pi/Opencode are exact-match. Documented asymmetry inherited from `recall-day.py`.
- System entries (`attachment`, `permission-mode`, `file-history-snapshot`) appear in the message list. Users filter via fzf substring (`user`/`assistant`). A `--user-only` flag would be a feature, not a bug fix.
