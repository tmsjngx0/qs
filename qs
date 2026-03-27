#!/bin/bash
# qs — Quick Search: qmd → fzf → glow
# Usage: qs <query>        — Hybrid search (BM25 + vector + LLM expansion)
#        qs -f <query>     — BM25 only (fast, no LLM)
#        qs                — Browse all indexed files with fzf
set -euo pipefail

KB_DIR="${QS_KB_DIR:-$HOME/kb}"
MODE="query"
QUERY=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    -f|--fast) MODE="search"; shift ;;
    -h|--help)
      echo "qs — Quick Search: qmd → fzf → glow"
      echo ""
      echo "Usage:"
      echo "  qs <terms>       Hybrid search (BM25 + vector + reranking)"
      echo "  qs -f <terms>    BM25 keyword search (fast, no LLM)"
      echo "  qs               Browse all kb files"
      echo ""
      echo "Controls:"
      echo "  Enter            Render selected file with glow"
      echo "  Ctrl-Y           Copy file path to clipboard"
      echo "  Ctrl-E           Open in \$EDITOR"
      echo "  Esc              Quit"
      exit 0
      ;;
    *) QUERY="${QUERY:+$QUERY }$1"; shift ;;
  esac
done

# If no query, fallback to gf-style browsing over kb/
if [[ -z "$QUERY" ]]; then
  fd -e md . "$KB_DIR" \
    | fzf --preview 'bat --color=always --style=numbers {}' \
          --bind 'ctrl-y:execute-silent(echo -n {} | pbcopy)+abort' \
          --bind "ctrl-e:execute(\${EDITOR:-vim} {})" \
    | xargs -r glow
  exit 0
fi

# qmd output format:
#   qmd://collection/path/file.md:line #hash
#   Title: Document Title
#   Score:  92%
#
#   @@ snippet @@
#   content lines...
#
# Parse: extract URI line → resolve to filesystem path

# Run qmd, extract result blocks
raw=$(qmd "$MODE" "$QUERY" 2>/dev/null || true)

if [[ -z "$raw" ]]; then
  echo "No results for: $QUERY"
  [[ "$MODE" == "search" ]] && echo "Try: qs -q '$QUERY' (hybrid search)"
  exit 1
fi

# Parse results into fzf-friendly format: "score\ttitle\trelpath\tfullpath"
formatted=$(echo "$raw" | awk -v kb="$KB_DIR" '
  /^qmd:\/\// {
    # Extract URI (first field, strip :line and #hash)
    uri = $1
    sub(/:[0-9]+$/, "", uri)
    sub(/#.*/, "", uri)
    # qmd://collection/path → kb/path
    path = uri
    sub(/^qmd:\/\//, "", path)
    fullpath = kb "/" path
    # Relative path for display
    relpath = path
  }
  /^Title:/ {
    title = $0
    sub(/^Title:\s*/, "", title)
  }
  /^Score:/ {
    score = $0
    sub(/^Score:\s*/, "", score)
    # Print the entry
    if (fullpath != "" && title != "") {
      printf "%s\t%s\t%s\t%s\n", score, title, relpath, fullpath
    }
    # Reset for next result
    fullpath = ""
    title = ""
    score = ""
  }
')

if [[ -z "$formatted" ]]; then
  echo "No parseable results for: $QUERY"
  exit 1
fi

# fzf: show "score | title | path", preview with bat, output full path
selected=$(echo "$formatted" \
  | fzf --delimiter='\t' \
        --with-nth=1,2,3 \
        --preview 'bat --color=always --style=numbers {4}' \
        --bind 'ctrl-y:execute-silent(echo -n {4} | pbcopy)+abort' \
        --bind "ctrl-e:execute(\${EDITOR:-vim} {4})" \
        --preview-window='right:60%' \
        --header="Enter: glow | Ctrl-Y: copy path | Ctrl-E: edit" \
  | cut -f4)

if [[ -n "$selected" ]]; then
  glow "$selected"
fi
