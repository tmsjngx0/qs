# QS / CCS Plan

> **Status (2026-04-28):** Iteration 1 landed. `ccs.py` now covers Claude Code, Codex, Pi, and Opencode through a vendored adapter pattern (sourced from the `recall` skill). Free-text positional arg pre-fills fzf's interactive filter — that is the search experience the original brief asked for. `qs` stays a Bash wrapper around `qmd` and is unchanged.
>
> **Repo-shape decision:** Option A — keep `qs` as a single-file Bash CLI, ship `ccs` as a separate Python script in the same repo. No `bin/` reshuffle.
>
> Sections below are the original planning doc kept for history.

## Context

This repo currently contains `qs`, a Bash utility for `qmd -> fzf -> glow` over a markdown knowledge base. It should stay conceptually separate from `ccs`, which is a Claude Code session browser for JSONL session data under `~/.claude/projects`.

The earlier `ccs` Bash prototype hit quoting issues around `fzf` preview placeholders and shell-escaped paths. That is a strong signal to rewrite `ccs` in Python rather than continuing to add shell-workarounds.

## Immediate Goals

1. Re-evaluate `qs` as its own tool.
2. Design a separate `ccs` tool in this repo or a sibling repo/directory without conflating the two.
3. Resume implementation from `/home/thoma/source/qs` so the work happens in the intended location.

## Recommended Next Steps

1. Review the current `qs` contract.
   - Confirm whether `qs` should remain a single-file Bash CLI.
   - Check portability issues like `pbcopy` and assumptions about `qmd` URI layout.
   - Decide whether `qs` needs small fixes only or a more deliberate rewrite.

2. Define `ccs` scope before coding.
   - Input root: `~/.claude/projects`
   - Primary flows:
     - browse project/session directories
     - browse session `.jsonl` files
     - inspect message-by-message content
   - UX constraints:
     - safe path handling with spaces and shell escaping
     - robust previews in `fzf`
     - readable rendering for user/assistant/tool messages

3. Implement `ccs` in Python, not Bash.
   - Use Python for path handling, JSONL parsing, temp files, and subprocess calls.
   - Keep `fzf` for navigation and `bat` for rendering unless there is a reason to inline rendering.
   - Avoid passing raw `fzf` placeholders through nested shell quoting when a direct Python subprocess call can own that logic.

4. Decide repo structure before edits.
   - Option A: keep `qs` as-is and add a separate executable such as `ccs`.
   - Option B: create `bin/` or `scripts/` and move both tools into a clearer layout.
   - Option C: split `ccs` into its own repo if the repo should remain search-focused.

## Known Issues In Current `qs`

- Clipboard binding uses `pbcopy`, which is macOS-specific.
- File/path handling is still shell-driven and should be reviewed for quoting safety.
- Parsing `qmd` output depends on a specific text format and may be fragile.
- Browse mode and search mode assume a few tools exist but do not validate dependencies up front.

## Proposed Session Start Prompt

When resuming in `/home/thoma/source/qs`, start with:

`Review this repo and help me decide whether to keep qs as a small bash tool, then design and implement ccs as a separate Python CLI without conflating the two.`

## Deliverables For Next Session

1. A short repo-shape decision for `qs` vs `ccs`.
2. A concrete `ccs` implementation plan.
3. If the structure is clear, a first Python `ccs` executable with basic browse and preview flows.
