# qs / ccs

Two separate terminal tools live in this repo:

- `qs` — quick markdown search over a `qmd` knowledge base
- `ccs` — multi-source coding-agent session browser (Claude Code, Codex, Pi, Opencode)

They are intentionally separate tools. `qs` stays a small shell wrapper around `qmd`; `ccs` is a separate Python CLI because the session-browser path handling, multi-format JSON/SQLite parsing, and preview logic are a poor fit for more shell quoting.

## qs

Semantic search your markdown knowledge base from the terminal.

`qmd` search -> `fzf` selection -> `glow` rendering in one command.

### Demo

```bash
qs zfs snapshot          # hybrid search (BM25 + vector + reranking)
qs -f zfs snapshot       # BM25 keyword search (fast, no LLM)
qs                       # browse all kb files with fzf
```

### How it works

```
┌──────────┐     ┌─────────┐     ┌──────────┐
│ qmd      │ ──► │ fzf     │ ──► │ glow     │
│ (search) │     │ (pick)  │     │ (render) │
└──────────┘     └─────────┘     └──────────┘
```

1. **Search** — `qmd` searches your indexed markdown files (hybrid: BM25 + vector similarity + LLM query expansion)
2. **Pick** — `fzf` shows results with `bat` preview, ranked by relevance score
3. **Render** — selected file opens in `glow` (terminal markdown renderer)

### Keybindings (in fzf)

| Key | Action |
|-----|--------|
| `Enter` | Render with glow |
| `Ctrl-Y` | Copy file path to clipboard |
| `Ctrl-E` | Open in `$EDITOR` |
| `Esc` | Quit |

### Requirements

- [qmd](https://github.com/tobilu/qmd) — markdown search engine (BM25 + vector)
- [fzf](https://github.com/junegunn/fzf) — fuzzy finder
- [glow](https://github.com/charmbracelet/glow) — terminal markdown renderer
- [bat](https://github.com/sharkdp/bat) — cat with syntax highlighting (for fzf preview)
- [fd](https://github.com/sharkdp/fd) — fast file finder (optional; `find` is used as a fallback in browse mode)

### Install

```bash
# Clone
git clone https://github.com/tmsjngx0/qs.git ~/.local/share/qs

# Add to PATH (or symlink)
ln -s ~/.local/share/qs/qs ~/.local/bin/qs

# Or add alias to .zshrc
echo 'qs() { ~/.local/share/qs/qs "$@"; }' >> ~/.zshrc
```

### Setup qmd (required)

`qs` searches whatever `qmd` has indexed. Set up your collections first:

```bash
# Add your markdown directories
qmd collection add notes ~/notes '**/*.md'
qmd collection add source ~/source '**/*.md'

# Index
qmd update

# Generate embeddings (needed for hybrid search)
qmd embed
```

### Configuration

| Env var | Default | Description |
|---------|---------|-------------|
| `QS_KB_DIR` | `~/kb` | Root directory for fzf browse mode |

## ccs

Browse coding-agent session logs from Claude Code, Codex, Pi, and Opencode in one picker. Type to filter, drill into messages, render with `bat`.

**Quick keys:** `Enter` open · `y` copy session · `Y` copy one msg · `?` in-app help · `Esc` back

### Requirements

Realistically you install **zero or one thing** (fzf, optional). Everything else is Python stdlib or auto-detected with graceful fallback.

| | Tool | Notes |
|---|---|---|
| Required | `python3` (≥3.9) | Stdlib only — no `pip install`. `sqlite3` is bundled. |
| Recommended | [`fzf`](https://github.com/junegunn/fzf) | Fuzzy filter + live preview + key bindings. `brew install fzf` · `scoop install fzf` · `apt install fzf`. **Without it, ccs runs in a stdlib fallback picker** (numbered list, `/text` substring filter, `q` quit). |
| Pager (optional, one of) | `bat` → `$PAGER` → `less` | First one found wins. Plain `less` is fine. |
| Clipboard (optional, one of) | `pbcopy` · `wl-copy` · `xclip` · `xsel` · `clip.exe` | macOS / Wayland / X11 / X11 / WSL+Windows. Your OS already ships one. |

### Platform support

| Platform | Status |
|---|---|
| macOS | ✅ |
| Linux (X11 / Wayland) | ✅ |
| WSL2 on Windows | ✅ |
| Native Windows | ⚠️ Partial — `clip.exe`/`fzf.exe` work, but Claude Code's project-directory encoding hasn't been verified. Use `--all` to bypass cwd filtering. |

### Demo

```bash
ccs                              # current cwd, all available sources
ccs migration plan               # pre-fill fzf query (substring filter on tool/cwd/title/time)
ccs --all                        # scan every cwd across all tools
ccs --source claude              # restrict to one tool (repeat to combine)
ccs --cwd /home/thoma/source/qs  # specific cwd, all tools
ccs --session ~/.claude/projects/.../foo.jsonl   # open a JSONL file directly
ccs --session opencode://ses_2786e7db…           # open an Opencode session by id
```

### Keybindings (in fzf)

Session picker:

| Key | Action |
|-----|--------|
| `Enter` | Open the message picker for the session |
| `?` | Show the keybinding help screen |
| `Esc` | Quit |

Message picker (after picking a session):

| Key | Action |
|-----|--------|
| `Enter` | Render the message in `bat` |
| `y` | Copy the **whole conversation** (all messages, joined) to clipboard |
| `Y` | Copy **only the highlighted message** to clipboard |
| `?` | Show the keybinding help screen |
| `Esc` | Back to session picker |

Clipboard fallback chain: `pbcopy` → `wl-copy` → `xclip` → `xsel` → `clip.exe`.

### How it works

```
┌────────────────────────────┐
│ adapters discover sessions │      claude / codex / pi / opencode
│  (auto-skip if missing)    │      → SessionMeta records
└─────────────┬──────────────┘
              ▼
┌────────────────────────────┐
│ fzf session picker         │      one row per session, hidden cols carry
│  (substring filter)        │      tool + locator for dispatch
└─────────────┬──────────────┘
              ▼
┌────────────────────────────┐
│ fzf message picker         │      message list w/ live preview
│  (per session)             │
└─────────────┬──────────────┘
              ▼
┌────────────────────────────┐
│ bat pager on selected msg  │
└────────────────────────────┘
```

Each adapter knows how to walk its native storage (JSONL files for Claude/Codex/Pi, read-only SQLite for Opencode). The picker uses `fzf`'s built-in interactive filter on a rich row (`tool · time · msgs · size · cwd · title`), so search is just typing.

### Sources

| Tool        | Storage                                          | Format      |
|-------------|--------------------------------------------------|-------------|
| Claude Code | `~/.claude/projects/<encoded-cwd>/*.jsonl`       | JSONL       |
| Codex       | `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`   | JSONL       |
| Pi          | `~/.pi/agent/sessions/<encoded-cwd>/*.jsonl`     | JSONL       |
| Opencode    | `~/.local/share/opencode/opencode.db`            | SQLite (RO) |

Adapters auto-skip when their storage doesn't exist on the machine. Pi `imported-claude-*` files are deduped against Claude.

### Configuration

| Env var                | Default                                                  |
|------------------------|----------------------------------------------------------|
| `CLAUDE_PROJECTS_DIR`  | `~/.claude/projects`                                     |
| `CODEX_HOME`           | `~/.codex`                                               |
| `PI_SESSIONS_DIR`      | `~/.pi/agent/sessions`                                   |
| `OPENCODE_DB`          | `~/.local/share/opencode/opencode.db`                    |

### Notes

Adapter classes are vendored from the [`recall`](https://github.com/tmsjngx0/agent-skills) skill so `ccs` has no runtime dependency on that skill's deploy location. Upstream changes can be re-vendored as needed.

## License

MIT
