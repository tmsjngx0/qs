#!/usr/bin/env bash
# qs — Quick Search: qmd -> fzf -> glow
# Usage: qs <query>        — Hybrid search (BM25 + vector + LLM expansion)
#        qs -f <query>     — BM25 only (fast, no LLM)
#        qs                — Browse markdown files with fzf
set -euo pipefail

KB_DIR="${QS_KB_DIR:-$HOME/kb}"
MODE="query"
QUERY=""

declare -A COLLECTION_ROOTS=()

usage() {
  cat <<'EOF'
qs — Quick Search: qmd -> fzf -> glow

Usage:
  qs <terms>       Hybrid search (BM25 + vector + reranking)
  qs -f <terms>    BM25 keyword search (fast, no LLM)
  qs               Browse markdown files

Controls:
  Enter            Render selected file with glow
  Ctrl-Y           Copy file path to clipboard
  Ctrl-E           Open in $EDITOR
  Esc              Quit
EOF
}

have() {
  command -v "$1" >/dev/null 2>&1
}

require_cmd() {
  local cmd
  for cmd in "$@"; do
    if ! have "$cmd"; then
      echo "Missing required command: $cmd" >&2
      exit 1
    fi
  done
}

clipboard_copy() {
  if have pbcopy; then
    pbcopy
  elif have wl-copy; then
    wl-copy
  elif have xclip; then
    xclip -selection clipboard
  elif have xsel; then
    xsel --clipboard --input
  else
    echo "No clipboard tool found (pbcopy, wl-copy, xclip, xsel)." >&2
    return 1
  fi
}

preview_markdown() {
  local target="$1"
  if [[ "$target" == qmd://* ]]; then
    qmd get "$target" -l 200
  else
    bat --color=always --style=numbers "$target"
  fi
}

collection_root() {
  local collection="$1"

  if [[ -n "${COLLECTION_ROOTS[$collection]:-}" ]]; then
    printf '%s\n' "${COLLECTION_ROOTS[$collection]}"
    return 0
  fi

  local root
  root=$(qmd collection show "$collection" 2>/dev/null | awk -F': *' '/^[[:space:]]+Path:/ { print $2; exit }')
  if [[ -n "$root" ]]; then
    COLLECTION_ROOTS["$collection"]="$root"
    printf '%s\n' "$root"
  fi
}

uri_to_path() {
  local uri="$1"
  local remainder collection relpath root

  remainder="${uri#qmd://}"
  collection="${remainder%%/*}"
  relpath="${remainder#*/}"
  root="$(collection_root "$collection")"

  if [[ -n "$root" && -n "$relpath" && "$relpath" != "$remainder" ]]; then
    printf '%s/%s\n' "$root" "$relpath"
  fi
}

browse_files() {
  require_cmd fzf glow bat

  local finder selected
  if have fd; then
    finder=(fd -e md . "$KB_DIR")
  else
    finder=(find "$KB_DIR" -type f -name '*.md')
  fi

  selected="$("${finder[@]}" \
    | fzf --preview "$0 __preview-file {}" \
          --bind "ctrl-y:execute-silent($0 __copy-path {})+abort" \
          --bind "ctrl-e:execute($0 __edit-path {})" \
          --header="Enter: glow | Ctrl-Y: copy path | Ctrl-E: edit")"

  if [[ -n "$selected" ]]; then
    glow "$selected"
  fi
}

search_files() {
  require_cmd qmd fzf glow

  local raw formatted selected uri path title score
  raw="$(qmd "$MODE" --files "$QUERY" 2>/dev/null || true)"

  if [[ -z "$raw" ]]; then
    echo "No results for: $QUERY" >&2
    exit 1
  fi

  formatted=""
  while IFS=, read -r _ score uri _; do
    [[ -z "$uri" ]] && continue
    title="${uri#qmd://}"
    path="$(uri_to_path "$uri")"
    formatted+="${score}\t${title}\t${uri}\t${path}\n"
  done <<< "$raw"

  if [[ -z "$formatted" ]]; then
    echo "No parseable results for: $QUERY" >&2
    exit 1
  fi

  selected="$(printf '%b' "$formatted" \
    | fzf --delimiter='\t' \
          --with-nth=1,2 \
          --preview "$0 __preview-uri {3}" \
          --bind "ctrl-y:execute-silent($0 __copy-result {4} {3})+abort" \
          --bind "ctrl-e:execute($0 __edit-result {4} {3})" \
          --preview-window='right:60%' \
          --header="Enter: glow | Ctrl-Y: copy path | Ctrl-E: edit" \
    | cut -f3)"

  if [[ -n "$selected" ]]; then
    qmd get "$selected" | glow -
  fi
}

copy_result() {
  local path="${1:-}"
  local uri="${2:-}"
  if [[ -n "$path" ]]; then
    printf '%s' "$path" | clipboard_copy
  elif [[ -n "$uri" ]]; then
    printf '%s' "$uri" | clipboard_copy
  fi
}

edit_result() {
  local path="${1:-}"
  if [[ -z "$path" ]]; then
    echo "Unable to resolve a local file path for this result." >&2
    exit 1
  fi
  "${EDITOR:-vim}" "$path"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -f|--fast)
      MODE="search"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    __preview-file)
      preview_markdown "$2"
      exit 0
      ;;
    __preview-uri)
      preview_markdown "$2"
      exit 0
      ;;
    __copy-path)
      printf '%s' "$2" | clipboard_copy
      exit 0
      ;;
    __copy-result)
      copy_result "${2:-}" "${3:-}"
      exit 0
      ;;
    __edit-path)
      "${EDITOR:-vim}" "$2"
      exit 0
      ;;
    __edit-result)
      edit_result "${2:-}"
      exit 0
      ;;
    *)
      QUERY="${QUERY:+$QUERY }$1"
      shift
      ;;
  esac
done

if [[ -z "$QUERY" ]]; then
  browse_files
else
  search_files
fi
