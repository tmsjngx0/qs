# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.1.0] - 2026-03-27

### Added

- Initial release
- Hybrid search (BM25 + vector + reranking) as default mode
- Fast BM25-only mode with `-f` flag
- Browse mode (no query) with `fd` + `fzf` over `~/kb/`
- `fzf` keybindings: Enter (glow), Ctrl-Y (copy path), Ctrl-E (open in editor)
- `bat` syntax-highlighted preview in fzf
- Configurable kb directory via `QS_KB_DIR` env var
