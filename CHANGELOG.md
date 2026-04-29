# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- `ccs` now spans Claude Code, Codex, Pi, and Opencode — one picker, four sources
  - Adapter pattern (`ClaudeAdapter`, `CodexAdapter`, `PiAdapter`, `OpencodeAdapter`) vendored from the `recall` skill so lifecycles stay decoupled
  - Opencode read-only SQLite support via `opencode://<session_id>` locators
  - Free-text positional argument pre-fills fzf's interactive filter (e.g. `ccs migration plan`)
  - `--source`, `--all`, `--cwd`, `--session` flags; storage roots overridable via `CLAUDE_PROJECTS_DIR` / `CODEX_HOME` / `PI_SESSIONS_DIR` / `OPENCODE_DB`
  - Dedup of Pi `imported-claude-*` sessions against Claude

### Changed

- `ccs` two-tier UX (session → message) replaces the prior three-tier project drill-down; cwd is shown as a column instead of a separate stage
- Keep `qs` as a Bash CLI, but switch search rendering to `qmd` URIs instead of fragile path reconstruction
- Add clipboard fallbacks and dependency checks to `qs`
- Let `qs` browse mode fall back to `find` when `fd` is unavailable

## [0.1.0] - 2026-03-27

### Added

- Initial release
- Hybrid search (BM25 + vector + reranking) as default mode
- Fast BM25-only mode with `-f` flag
- Browse mode (no query) with `fd` + `fzf` over `~/kb/`
- `fzf` keybindings: Enter (glow), Ctrl-Y (copy path), Ctrl-E (open in editor)
- `bat` syntax-highlighted preview in fzf
- Configurable kb directory via `QS_KB_DIR` env var
