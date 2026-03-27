# qs — Quick Search

Semantic search your markdown knowledge base from the terminal.

`qmd` search → `fzf` selection → `glow` rendering — in one command.

## Demo

```bash
qs zfs snapshot          # hybrid search (BM25 + vector + reranking)
qs -f zfs snapshot       # BM25 keyword search (fast, no LLM)
qs                       # browse all kb files with fzf
```

## How it works

```
┌──────────┐     ┌─────────┐     ┌──────────┐
│ qmd      │ ──► │ fzf     │ ──► │ glow     │
│ (search) │     │ (pick)  │     │ (render) │
└──────────┘     └─────────┘     └──────────┘
```

1. **Search** — `qmd` searches your indexed markdown files (hybrid: BM25 + vector similarity + LLM query expansion)
2. **Pick** — `fzf` shows results with `bat` preview, ranked by relevance score
3. **Render** — selected file opens in `glow` (terminal markdown renderer)

## Keybindings (in fzf)

| Key | Action |
|-----|--------|
| `Enter` | Render with glow |
| `Ctrl-Y` | Copy file path to clipboard |
| `Ctrl-E` | Open in `$EDITOR` |
| `Esc` | Quit |

## Requirements

- [qmd](https://github.com/tobilu/qmd) — markdown search engine (BM25 + vector)
- [fzf](https://github.com/junegunn/fzf) — fuzzy finder
- [glow](https://github.com/charmbracelet/glow) — terminal markdown renderer
- [bat](https://github.com/sharkdp/bat) — cat with syntax highlighting (for fzf preview)
- [fd](https://github.com/sharkdp/fd) — fast file finder (for browse mode)

## Install

```bash
# Clone
git clone https://github.com/tmsjngx0/qs.git ~/.local/share/qs

# Add to PATH (or symlink)
ln -s ~/.local/share/qs/qs ~/.local/bin/qs

# Or add alias to .zshrc
echo 'qs() { ~/.local/share/qs/qs "$@"; }' >> ~/.zshrc
```

## Setup qmd (required)

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

## Configuration

| Env var | Default | Description |
|---------|---------|-------------|
| `QS_KB_DIR` | `~/kb` | Root directory for fzf browse mode |

## License

MIT
