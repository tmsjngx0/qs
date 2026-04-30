#!/usr/bin/env python3
"""ccs — Claude Code / Codex / Pi / Opencode session browser.

Multi-source session browser built on fzf. Discovers sessions from all four
agent tools (auto-skip if storage missing), merges them into a single picker,
and lets you drill into messages with bat-rendered previews.

Search: pass any text after `ccs` to pre-fill fzf's interactive filter. The
session line includes tool, cwd, title, timestamp, and message count, so
substring matches across any of those fields just work.

Adapter classes are vendored from `~/.agents/skills/recall/scripts/recall-day.py`
to decouple lifecycles. Storage roots are env-overridable: CLAUDE_PROJECTS_DIR,
CODEX_HOME, PI_SESSIONS_DIR, OPENCODE_DB.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator


# ===========================================================================
# Storage roots
# ===========================================================================

CLAUDE_PROJECTS = Path(os.environ.get("CLAUDE_PROJECTS_DIR", str(Path.home() / ".claude" / "projects")))
CODEX_HOME = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
CODEX_SESSIONS = CODEX_HOME / "sessions"
PI_SESSIONS = Path(os.environ.get("PI_SESSIONS_DIR", str(Path.home() / ".pi" / "agent" / "sessions")))
OPENCODE_DB = Path(os.environ.get("OPENCODE_DB", str(Path.home() / ".local" / "share" / "opencode" / "opencode.db")))

CLAUDE_IGNORE = {"cache", "memory"}

OPENCODE_LOCATOR_PREFIX = "opencode://"


# ===========================================================================
# Cleanup patterns (vendored from recall-day.py)
# ===========================================================================

STRIP_PATTERNS = [
    re.compile(r'<system-reminder>.*?</system-reminder>', re.DOTALL),
    re.compile(r'<local-command-caveat>.*?</local-command-caveat>', re.DOTALL),
    re.compile(r'<local-command-stdout>.*?</local-command-stdout>', re.DOTALL),
    re.compile(
        r'<command-name>.*?</command-name>\s*<command-message>.*?</command-message>'
        r'\s*(?:<command-args>.*?</command-args>)?',
        re.DOTALL,
    ),
    re.compile(r'<task-notification>.*?</task-notification>', re.DOTALL),
    re.compile(r'<teammate-message[^>]*>.*?</teammate-message>', re.DOTALL),
    re.compile(r'<permissions instructions>.*?</permissions instructions>', re.DOTALL),
    re.compile(r'<environment_context>.*?</environment_context>', re.DOTALL),
    re.compile(r'<INSTRUCTIONS>.*?</INSTRUCTIONS>', re.DOTALL),
    re.compile(r'<user_instructions>.*?</user_instructions>', re.DOTALL),
    re.compile(r'<skill[^>]*>.*?</skill>', re.DOTALL),
]


def clean_content(text: str) -> str:
    if not isinstance(text, str):
        return ""
    for pat in STRIP_PATTERNS:
        text = pat.sub("", text)
    return text.strip()


def extract_text(content: object) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                t = block.get("type")
                if t in ("text", "input_text"):
                    parts.append(str(block.get("text", "")))
                elif t == "tool_use":
                    inp = json.dumps(block.get("input", {}), ensure_ascii=False)[:400]
                    parts.append(f"[tool_use {block.get('name', '')}] {inp}")
                elif t == "tool_result":
                    inner = block.get("content", "")
                    if isinstance(inner, list):
                        inner = extract_text(inner)
                    parts.append(f"[tool_result] {inner}")
                else:
                    parts.append(json.dumps(block, ensure_ascii=False)[:400])
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(p for p in parts if p)
    if isinstance(content, dict):
        if "content" in content:
            return extract_text(content["content"])
        return json.dumps(content, ensure_ascii=False)
    return str(content)


def derive_title(first_user_msg: str) -> str:
    if not first_user_msg:
        return "Untitled"
    first_line = first_user_msg.split("\n", 1)[0].strip()
    first_line = re.sub(r"^#+\s*", "", first_line)
    if len(first_line) > 80:
        first_line = first_line[:77] + "..."
    return first_line if len(first_line) >= 3 else "Untitled"


# ===========================================================================
# Records
# ===========================================================================


@dataclass
class SessionMeta:
    tool: str
    locator: str
    session_id: str
    session_id_full: str
    cwd: str
    title: str
    user_msg_count: int
    start_time: datetime | None
    file_size: int = 0


@dataclass
class MessageRecord:
    index: int
    role: str
    type: str
    timestamp: str
    summary: str
    body: str


# ===========================================================================
# Format helpers
# ===========================================================================


def truncate(text: str, limit: int) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def fmt_ts(value: object) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, datetime):
        return value.astimezone().strftime("%Y-%m-%d %H:%M")
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M")
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return str(value)


def fmt_size(size_bytes: int) -> str:
    if size_bytes <= 0:
        return "—"
    if size_bytes < 1024:
        return f"{size_bytes}B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.0f}K"
    return f"{size_bytes / (1024 * 1024):.1f}M"


def shorten_cwd(cwd: str) -> str:
    if not cwd:
        return "-"
    home = str(Path.home())
    if cwd.startswith(home):
        cwd = "~" + cwd[len(home):]
    if len(cwd) > 38:
        cwd = "…" + cwd[-37:]
    return cwd


# ===========================================================================
# Body rendering — converts a parsed entry to a markdown string for bat preview
# ===========================================================================

# Strings longer than this (or containing newlines) get extracted into a fenced
# code block instead of staying inline. This is what fixes the "huge JSON
# escape-sequence wall" problem on Codex `session_meta.base_instructions.text`
# and similar large-text payloads.
LONG_STRING_THRESHOLD = 120


def _pretty_value(value: object, depth: int = 0) -> str:
    """Render a JSON-ish value as markdown.

    Returns a fragment that is either:
      - inline (single token, no leading newline) — caller appends after a
        ': ' on the parent line; or
      - block (starts with '\n') — caller appends directly to the parent line.

    Multi-line / long strings always become fenced blocks; small scalars stay
    inline; nested dicts/lists become indented bullet lists.
    """
    pad = "  " * depth
    if value is None:
        return "_null_"
    if isinstance(value, bool):
        return f"`{str(value).lower()}`"
    if isinstance(value, (int, float)):
        return f"`{value}`"
    if isinstance(value, str):
        if not value:
            return '`""`'
        if "\n" in value or len(value) > LONG_STRING_THRESHOLD:
            return "\n\n```\n" + value + "\n```"
        return value
    if isinstance(value, list):
        if not value:
            return "`[]`"
        lines = []
        for item in value:
            rendered = _pretty_value(item, depth + 1)
            if rendered.startswith("\n"):
                lines.append(f"{pad}-{rendered}")
            else:
                lines.append(f"{pad}- {rendered}")
        return "\n" + "\n".join(lines)
    if isinstance(value, dict):
        if not value:
            return "`{}`"
        lines = []
        for k, v in value.items():
            rendered = _pretty_value(v, depth + 1)
            if rendered.startswith("\n"):
                lines.append(f"{pad}- **{k}**:{rendered}")
            else:
                lines.append(f"{pad}- **{k}**: {rendered}")
        return "\n" + "\n".join(lines)
    return f"`{value!r}`"


def _maybe_humanize_json(text: str) -> str:
    """If text looks like a one-line JSON object/array, parse + prettify.
    Otherwise return as-is. Used to unwrap raw JSONL lines that end up
    embedded as plain strings inside tool_result / user message content."""
    if not isinstance(text, str):
        return str(text)
    s = text.strip()
    if not s:
        return text
    if (s[0] == "{" and s[-1] == "}") or (s[0] == "[" and s[-1] == "]"):
        try:
            obj = json.loads(s)
        except (json.JSONDecodeError, ValueError):
            return text
        return _pretty_value(obj, 0).lstrip("\n")
    return text


def render_user(ts: str, cleaned: str) -> str:
    body = _maybe_humanize_json(cleaned)
    return f"# user — {ts or '-'}\n\n{body}\n"


def render_assistant_blocks(ts: str, content: object) -> str:
    """Render the assistant content list as readable markdown."""
    lines = [f"# assistant — {ts or '-'}", ""]
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                lines.append(str(block))
                continue
            t = block.get("type")
            if t == "text":
                lines.append(str(block.get("text", "")))
                lines.append("")
            elif t == "tool_use":
                name = block.get("name", "?")
                inp = block.get("input", {})
                lines.append(f"## tool_use: {name}")
                lines.append(_pretty_value(inp, 0).lstrip("\n"))
                lines.append("")
            elif t == "thinking":
                thinking = block.get("thinking", "") or block.get("text", "")
                if thinking:
                    lines.append(f"## thinking\n\n> " + thinking.replace("\n", "\n> "))
                    lines.append("")
            else:
                lines.append(_pretty_value(block, 0).lstrip("\n"))
    elif isinstance(content, str):
        lines.append(_maybe_humanize_json(content))
    return "\n".join(lines).rstrip() + "\n"


def render_tool_result(ts: str, content: object) -> str:
    text = extract_text(content) if not isinstance(content, str) else content
    body = _maybe_humanize_json(text)
    if body == text:
        # Plain text — keep it as a fenced raw block for safety
        return f"# tool_result — {ts or '-'}\n\n```\n{text}\n```\n"
    return f"# tool_result — {ts or '-'}\n\n{body}\n"


def render_generic(label: str, ts: str, raw: dict) -> str:
    body = _pretty_value(raw, 0).lstrip("\n")
    return f"# {label} — {ts or '-'}\n\n{body}\n"


# ===========================================================================
# Adapters
# ===========================================================================


class Adapter:
    name: str = ""

    def available(self) -> bool:
        raise NotImplementedError

    def discover(self, *, cwd_filter: str | None, all_projects: bool) -> Iterator[SessionMeta]:
        raise NotImplementedError

    def messages(self, locator: str) -> list[MessageRecord]:
        raise NotImplementedError


class ClaudeAdapter(Adapter):
    name = "claude"

    def available(self) -> bool:
        return CLAUDE_PROJECTS.exists()

    def _project_dirs(self, cwd_filter: str | None, all_projects: bool) -> list[Path]:
        if cwd_filter:
            encoded = cwd_filter.replace("/", "-")
            p = CLAUDE_PROJECTS / encoded
            return [p] if p.exists() else []
        if all_projects:
            return [d for d in CLAUDE_PROJECTS.iterdir() if d.is_dir()]
        cwd = os.getcwd()
        default = CLAUDE_PROJECTS / cwd.replace("/", "-")
        if default.exists():
            return [default]
        return [d for d in CLAUDE_PROJECTS.iterdir() if d.is_dir()]

    def discover(self, *, cwd_filter, all_projects):
        for proj_dir in self._project_dirs(cwd_filter, all_projects):
            cwd_for_records = proj_dir.name.replace("-", "/")
            for filepath in proj_dir.glob("*.jsonl"):
                rel = filepath.relative_to(proj_dir).parts
                if any(part in CLAUDE_IGNORE for part in rel):
                    continue
                meta = self._scan(filepath, cwd_for_records)
                if meta is not None:
                    yield meta

    def _scan(self, filepath: Path, cwd: str) -> SessionMeta | None:
        session_id = filepath.stem
        start_time: datetime | None = None
        first_user_msg: str | None = None
        user_msg_count = 0
        try:
            file_size = filepath.stat().st_size
            with open(filepath, encoding="utf-8") as f:
                for line in f:
                    if not line.strip():
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if obj.get("sessionId"):
                        session_id = obj["sessionId"]
                    if start_time is None:
                        ts = obj.get("timestamp")
                        if ts:
                            try:
                                start_time = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
                            except (ValueError, TypeError):
                                pass
                    if obj.get("type") == "user":
                        msg = obj.get("message") or {}
                        if isinstance(msg, dict) and msg.get("role") == "user":
                            cleaned = clean_content(extract_text(msg.get("content", "")))
                            if not cleaned or len(cleaned) < 3:
                                continue
                            user_msg_count += 1
                            if first_user_msg is None:
                                first_user_msg = cleaned
        except (OSError, UnicodeDecodeError):
            return None

        return SessionMeta(
            tool=self.name,
            locator=str(filepath),
            session_id=session_id[:8],
            session_id_full=session_id,
            cwd=cwd,
            title=derive_title(first_user_msg or ""),
            user_msg_count=user_msg_count,
            start_time=start_time,
            file_size=file_size,
        )

    def messages(self, locator: str) -> list[MessageRecord]:
        path = Path(locator)
        out: list[MessageRecord] = []
        idx = 0
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    if not line.strip():
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    rtype = obj.get("type", "-")
                    msg = obj.get("message") if isinstance(obj.get("message"), dict) else None
                    role = (msg or {}).get("role", "-")
                    ts = fmt_ts(obj.get("timestamp"))
                    if rtype == "user" and role == "user":
                        cleaned = clean_content(extract_text((msg or {}).get("content", "")))
                        if not cleaned:
                            tool_result = (msg or {}).get("content")
                            body = render_tool_result(ts, tool_result)
                            summary = "[tool_result]"
                            role_label = "tool"
                        else:
                            body = render_user(ts, cleaned)
                            summary = truncate(cleaned, 100)
                            role_label = "user"
                        idx += 1
                        out.append(MessageRecord(idx, role_label, rtype, ts, summary, body))
                    elif rtype == "assistant" and role == "assistant":
                        content = (msg or {}).get("content", [])
                        body = render_assistant_blocks(ts, content)
                        summary = truncate(clean_content(extract_text(content)), 100) or "[assistant]"
                        idx += 1
                        out.append(MessageRecord(idx, "assistant", rtype, ts, summary, body))
                    else:
                        idx += 1
                        body = render_generic(rtype or "entry", ts, obj)
                        summary = truncate(extract_text(obj), 100)
                        out.append(MessageRecord(idx, role, rtype, ts, summary, body))
        except OSError as exc:
            out.append(MessageRecord(0, "error", "error", "", str(exc), f"# error\n\n{exc}\n"))
        return out


class CodexAdapter(Adapter):
    name = "codex"

    def available(self) -> bool:
        return CODEX_SESSIONS.exists()

    def discover(self, *, cwd_filter, all_projects):
        if not self.available():
            return
        target_cwd = cwd_filter or (None if all_projects else os.getcwd())
        for filepath in CODEX_SESSIONS.rglob("rollout-*.jsonl"):
            meta = self._scan(filepath, target_cwd)
            if meta is not None:
                yield meta

    def _scan(self, filepath: Path, cwd_filter: str | None) -> SessionMeta | None:
        session_id = filepath.stem
        start_time: datetime | None = None
        cwd = ""
        first_user_msg: str | None = None
        user_msg_count = 0
        try:
            file_size = filepath.stat().st_size
            with open(filepath, encoding="utf-8") as f:
                for line in f:
                    if not line.strip():
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    rtype = obj.get("type")
                    payload = obj.get("payload") or {}
                    if rtype == "session_meta" and start_time is None:
                        sid = payload.get("id")
                        if sid:
                            session_id = sid
                        cwd = payload.get("cwd", "") or ""
                        ts = payload.get("timestamp") or obj.get("timestamp")
                        if ts:
                            try:
                                start_time = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
                            except (ValueError, TypeError):
                                pass
                        if cwd_filter and cwd and not cwd.startswith(cwd_filter):
                            return None
                        continue
                    if rtype == "response_item" and payload.get("type") == "message":
                        if payload.get("role") != "user":
                            continue
                        cleaned = clean_content(extract_text(payload.get("content", [])))
                        if not cleaned or len(cleaned) < 3:
                            continue
                        user_msg_count += 1
                        if first_user_msg is None:
                            first_user_msg = cleaned
        except (OSError, UnicodeDecodeError):
            return None

        if cwd_filter and cwd and not cwd.startswith(cwd_filter):
            return None

        return SessionMeta(
            tool=self.name,
            locator=str(filepath),
            session_id=session_id[:8],
            session_id_full=session_id,
            cwd=cwd,
            title=derive_title(first_user_msg or ""),
            user_msg_count=user_msg_count,
            start_time=start_time,
            file_size=file_size,
        )

    def messages(self, locator: str) -> list[MessageRecord]:
        path = Path(locator)
        out: list[MessageRecord] = []
        idx = 0
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    if not line.strip():
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    rtype = obj.get("type")
                    payload = obj.get("payload") or {}
                    ts = fmt_ts(obj.get("timestamp"))
                    if rtype == "session_meta":
                        idx += 1
                        body = render_generic("session_meta", ts, payload)
                        out.append(MessageRecord(idx, "system", "session_meta", ts,
                                                 truncate(json.dumps(payload, ensure_ascii=False), 100),
                                                 body))
                    elif rtype == "response_item" and payload.get("type") == "message":
                        role = payload.get("role", "-")
                        content = payload.get("content", [])
                        cleaned = clean_content(extract_text(content))
                        if role == "user":
                            body = render_user(ts, cleaned)
                        else:
                            body = render_assistant_blocks(ts, content)
                        idx += 1
                        out.append(MessageRecord(idx, role, "message", ts,
                                                 truncate(cleaned, 100) or f"[{role}]",
                                                 body))
                    elif rtype == "response_item" and payload.get("type") == "function_call":
                        name = payload.get("name", "?")
                        args = payload.get("arguments", "")
                        body = (
                            f"# function_call: {name} — {ts or '-'}\n\n"
                            f"```\n{args}\n```\n"
                        )
                        idx += 1
                        out.append(MessageRecord(idx, "tool", "function_call", ts,
                                                 truncate(f"{name} {args}", 100), body))
                    elif rtype == "response_item" and payload.get("type") == "function_call_output":
                        body = render_generic("function_call_output", ts, payload)
                        idx += 1
                        out.append(MessageRecord(idx, "tool", "function_call_output", ts,
                                                 truncate(extract_text(payload), 100), body))
        except OSError as exc:
            out.append(MessageRecord(0, "error", "error", "", str(exc), f"# error\n\n{exc}\n"))
        return out


class PiAdapter(Adapter):
    name = "pi"

    def available(self) -> bool:
        return PI_SESSIONS.exists()

    @staticmethod
    def _encode_cwd(cwd: str) -> str:
        # Pi encodes /a/b/c as --a-b-c-- (each slash becomes a dash, then the
        # whole thing is wrapped in another pair of dashes). Equivalent to
        # appending a trailing slash before the slash→dash replacement and
        # then surrounding with single dashes:
        #   /home/thoma/source/qs → /home/thoma/source/qs/
        #   → -home-thoma-source-qs-
        #   → --home-thoma-source-qs--
        normalized = cwd.rstrip("/") + "/"
        return "-" + normalized.replace("/", "-") + "-"

    def _project_dirs(self, cwd_filter: str | None, all_projects: bool) -> list[Path]:
        if cwd_filter:
            p = PI_SESSIONS / self._encode_cwd(cwd_filter)
            return [p] if p.exists() else []
        if all_projects:
            return [d for d in PI_SESSIONS.iterdir() if d.is_dir()]
        default = PI_SESSIONS / self._encode_cwd(os.getcwd())
        if default.exists():
            return [default]
        return [d for d in PI_SESSIONS.iterdir() if d.is_dir()]

    def discover(self, *, cwd_filter, all_projects):
        for proj_dir in self._project_dirs(cwd_filter, all_projects):
            # Decode --home-thoma-source-qs-- back to /home/thoma/source/qs
            cwd_for_records = "/" + proj_dir.name.strip("-").replace("-", "/")
            for filepath in proj_dir.glob("*.jsonl"):
                # Skip imported-claude dupes (recall-day's dedup behaviour)
                if filepath.name.startswith("imported-claude-"):
                    continue
                meta = self._scan(filepath, cwd_for_records)
                if meta is not None:
                    yield meta

    def _scan(self, filepath: Path, cwd_default: str) -> SessionMeta | None:
        session_id = filepath.stem
        start_time: datetime | None = None
        cwd = ""
        first_user_msg: str | None = None
        user_msg_count = 0
        try:
            file_size = filepath.stat().st_size
            with open(filepath, encoding="utf-8") as f:
                for line in f:
                    if not line.strip():
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    rtype = obj.get("type")
                    if rtype == "session" and start_time is None:
                        sid = obj.get("id")
                        if sid:
                            session_id = sid
                        cwd = obj.get("cwd", "") or ""
                        ts = obj.get("timestamp")
                        if ts:
                            try:
                                start_time = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
                            except (ValueError, TypeError):
                                pass
                        continue
                    if rtype == "message":
                        msg = obj.get("message") or {}
                        if msg.get("role") != "user":
                            continue
                        cleaned = clean_content(extract_text(msg.get("content", "")))
                        if not cleaned or len(cleaned) < 3:
                            continue
                        user_msg_count += 1
                        if first_user_msg is None:
                            first_user_msg = cleaned
                        if start_time is None:
                            ts = obj.get("timestamp")
                            if ts:
                                try:
                                    start_time = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
                                except (ValueError, TypeError):
                                    pass
        except (OSError, UnicodeDecodeError):
            return None

        return SessionMeta(
            tool=self.name,
            locator=str(filepath),
            session_id=session_id[:8],
            session_id_full=session_id,
            cwd=cwd or cwd_default,
            title=derive_title(first_user_msg or ""),
            user_msg_count=user_msg_count,
            start_time=start_time,
            file_size=file_size,
        )

    def messages(self, locator: str) -> list[MessageRecord]:
        path = Path(locator)
        out: list[MessageRecord] = []
        idx = 0
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    if not line.strip():
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    rtype = obj.get("type")
                    ts = fmt_ts(obj.get("timestamp"))
                    if rtype == "session":
                        idx += 1
                        body = render_generic("session", ts, obj)
                        out.append(MessageRecord(idx, "system", "session", ts,
                                                 truncate(json.dumps(obj, ensure_ascii=False), 100),
                                                 body))
                    elif rtype == "message":
                        msg = obj.get("message") or {}
                        role = msg.get("role", "-")
                        content = msg.get("content", "")
                        cleaned = clean_content(extract_text(content))
                        if role == "user":
                            body = render_user(ts, cleaned)
                        else:
                            body = render_assistant_blocks(ts, content)
                        idx += 1
                        out.append(MessageRecord(idx, role, "message", ts,
                                                 truncate(cleaned, 100) or f"[{role}]",
                                                 body))
                    else:
                        idx += 1
                        body = render_generic(rtype or "entry", ts, obj)
                        out.append(MessageRecord(idx, "-", rtype or "-", ts,
                                                 truncate(extract_text(obj), 100), body))
        except OSError as exc:
            out.append(MessageRecord(0, "error", "error", "", str(exc), f"# error\n\n{exc}\n"))
        return out


class OpencodeAdapter(Adapter):
    name = "opencode"

    def available(self) -> bool:
        return OPENCODE_DB.exists()

    def _connect(self) -> sqlite3.Connection:
        uri = f"file:{OPENCODE_DB}?mode=ro"
        return sqlite3.connect(uri, uri=True)

    @staticmethod
    def _make_locator(session_id: str) -> str:
        return f"{OPENCODE_LOCATOR_PREFIX}{session_id}"

    @staticmethod
    def _parse_locator(locator: str) -> str | None:
        if not locator.startswith(OPENCODE_LOCATOR_PREFIX):
            return None
        return locator[len(OPENCODE_LOCATOR_PREFIX):]

    def discover(self, *, cwd_filter, all_projects):
        if not self.available():
            return
        target_cwd = cwd_filter or (None if all_projects else os.getcwd())
        try:
            with self._connect() as conn:
                conn.row_factory = sqlite3.Row
                sql = (
                    "SELECT s.id, s.directory, s.title, s.time_created, "
                    "(SELECT COUNT(*) FROM message m "
                    " WHERE m.session_id = s.id "
                    "  AND json_extract(m.data, '$.role') = 'user') AS user_msgs "
                    "FROM session s "
                )
                params: list[object] = []
                if target_cwd:
                    sql += "WHERE s.directory = ? "
                    params.append(target_cwd)
                sql += "ORDER BY s.time_created DESC"
                for row in conn.execute(sql, params):
                    ts = datetime.fromtimestamp(row["time_created"] / 1000, tz=timezone.utc) if row["time_created"] else None
                    title = (row["title"] or "Untitled").strip()
                    if len(title) > 80:
                        title = title[:77] + "…"
                    sid = row["id"]
                    short_id = sid[:12] if sid.startswith("ses_") else sid[:8]
                    yield SessionMeta(
                        tool=self.name,
                        locator=self._make_locator(sid),
                        session_id=short_id,
                        session_id_full=sid,
                        cwd=row["directory"] or "",
                        title=title,
                        user_msg_count=row["user_msgs"] or 0,
                        start_time=ts,
                        file_size=0,
                    )
        except sqlite3.DatabaseError as exc:
            print(f"opencode: sqlite error: {exc}", file=sys.stderr)

    def messages(self, locator: str) -> list[MessageRecord]:
        sid = self._parse_locator(locator)
        if sid is None:
            return []
        out: list[MessageRecord] = []
        try:
            with self._connect() as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    """
                    SELECT m.id, m.time_created, m.data,
                           (SELECT GROUP_CONCAT(p.data, char(30)) FROM part p
                              WHERE p.message_id = m.id ORDER BY p.time_created) AS parts
                    FROM message m WHERE m.session_id = ?
                    ORDER BY m.time_created ASC, m.id ASC
                    """,
                    (sid,),
                )
                idx = 0
                for row in rows:
                    try:
                        data = json.loads(row["data"]) if row["data"] else {}
                    except json.JSONDecodeError:
                        data = {}
                    role = data.get("role", "-")
                    ts = fmt_ts(row["time_created"] / 1000) if row["time_created"] else ""
                    texts: list[str] = []
                    structured_parts: list[dict] = []
                    if row["parts"]:
                        for raw_part in row["parts"].split(chr(30)):
                            try:
                                part = json.loads(raw_part)
                            except json.JSONDecodeError:
                                continue
                            structured_parts.append(part)
                            if part.get("type") == "text":
                                texts.append(part.get("text", ""))
                    combined = clean_content("\n".join(t for t in texts if t))
                    if role == "user":
                        body = render_user(ts, combined)
                    elif role == "assistant":
                        # Reconstruct assistant blocks from parts
                        blocks: list[dict] = []
                        for part in structured_parts:
                            t = part.get("type")
                            if t == "text":
                                blocks.append({"type": "text", "text": part.get("text", "")})
                            elif t == "tool":
                                blocks.append({
                                    "type": "tool_use",
                                    "name": part.get("tool", "?"),
                                    "input": part.get("state", {}).get("input", {}) if isinstance(part.get("state"), dict) else {},
                                })
                        body = render_assistant_blocks(ts, blocks if blocks else combined)
                    else:
                        body = render_generic(role or "entry", ts, data)
                    idx += 1
                    out.append(MessageRecord(idx, role, "message", ts,
                                             truncate(combined, 100) or f"[{role}]",
                                             body))
        except sqlite3.DatabaseError as exc:
            out.append(MessageRecord(0, "error", "error", "",
                                     f"sqlite error: {exc}",
                                     f"# error\n\nsqlite error: {exc}\n"))
        return out


ADAPTERS: dict[str, type[Adapter]] = {
    "claude": ClaudeAdapter,
    "codex": CodexAdapter,
    "pi": PiAdapter,
    "opencode": OpencodeAdapter,
}


def adapter_for_tool(tool: str) -> Adapter:
    cls = ADAPTERS.get(tool)
    if cls is None:
        raise SystemExit(f"unknown tool: {tool}")
    return cls()


# ===========================================================================
# fzf integration
# ===========================================================================


def shutil_which(command: str) -> str | None:
    # Delegate to stdlib so PATHEXT (.exe / .bat / .cmd) resolution works on
    # native Windows. The hand-rolled version that lived here previously only
    # tried bare `command`, which silently failed for fzf.exe / bat.exe.
    return shutil.which(command)


# Clipboard fallback chain mirrors qs: pbcopy (mac) → wl-copy (Wayland) →
# xclip / xsel (X11) → clip.exe (WSL). First one found wins.
_CLIPBOARD_CMDS: tuple[list[str], ...] = (
    ["pbcopy"],
    ["wl-copy"],
    ["xclip", "-selection", "clipboard"],
    ["xsel", "--clipboard", "--input"],
    ["clip.exe"],
)


def clipboard_copy(text: str) -> bool:
    for cmd in _CLIPBOARD_CMDS:
        if shutil_which(cmd[0]):
            try:
                subprocess.run(cmd, input=text, text=True, check=True)
                return True
            except subprocess.CalledProcessError as exc:
                print(
                    f"clipboard copy via {cmd[0]} failed: rc={exc.returncode}. "
                    f"Tried command: {' '.join(cmd)}",
                    file=sys.stderr,
                )
                return False
    print(
        "No clipboard tool found. Tried: "
        + ", ".join(c[0] for c in _CLIPBOARD_CMDS)
        + ". Install one of these or set up WSL's clip.exe in PATH.",
        file=sys.stderr,
    )
    return False


def run_fzf(lines: list[str], *, header: str, preview: str, with_nth: str,
            initial_query: str | None = None, prompt: str = "> ",
            bindings: list[str] | None = None) -> str:
    """Returns "" on ESC/Ctrl-C/empty selection so callers can implement
    multi-level navigation (ESC = back one level, not exit)."""
    cmd = [
        "fzf",
        "--ansi",
        "--no-mouse",
        "--delimiter", "\t",
        "--with-nth", with_nth,
        "--prompt", prompt,
        "--header", header,
        "--preview", preview,
        "--preview-window", "right:65%:wrap",
    ]
    for binding in bindings or []:
        cmd += ["--bind", binding]
    if initial_query:
        cmd += ["--query", initial_query]
    result = subprocess.run(
        cmd,
        input="\n".join(lines),
        text=True,
        stdout=subprocess.PIPE,
    )
    if result.returncode in (1, 130):
        return ""
    if result.returncode != 0:
        raise SystemExit(result.returncode)
    return result.stdout.strip()


def _self_invoke(*args: str) -> str:
    script = shlex.quote(str(Path(__file__).resolve()))
    return script + " " + " ".join(shlex.quote(a) for a in args)


# ===========================================================================
# Discovery + browse loop
# ===========================================================================


def discover_all(sources: list[str], *, cwd_filter: str | None,
                 all_projects: bool) -> tuple[list[SessionMeta], list[str]]:
    sessions: list[SessionMeta] = []
    skipped: list[str] = []
    for name in sources:
        adapter = adapter_for_tool(name)
        if not adapter.available():
            skipped.append(name)
            continue
        try:
            for meta in adapter.discover(cwd_filter=cwd_filter, all_projects=all_projects):
                sessions.append(meta)
        except Exception as exc:  # noqa: BLE001 — surface adapter failures, keep going
            print(f"⚠️  {name} adapter failed: {exc}", file=sys.stderr)
    sessions.sort(key=lambda s: (s.start_time or datetime.min.replace(tzinfo=timezone.utc)), reverse=True)
    return sessions, skipped


def session_line(meta: SessionMeta) -> str:
    """Tab-delimited row. Hidden cols 1..3 used for dispatch; visible cols start at 4."""
    ts = fmt_ts(meta.start_time)
    return "\t".join([
        meta.tool,                        # 1 hidden
        meta.locator,                     # 2 hidden
        meta.session_id_full,             # 3 hidden
        f"{meta.tool:<8}",                # 4 visible
        f"{ts:<16}",                      # 5
        f"{meta.user_msg_count:>4}msg",   # 6
        f"{fmt_size(meta.file_size):>5}", # 7
        f"{shorten_cwd(meta.cwd):<40}",   # 8
        meta.title,                       # 9
    ])


def browse_sessions(sources: list[str], *, cwd_filter: str | None,
                    all_projects: bool, query: str | None) -> int:
    sessions, skipped = discover_all(sources, cwd_filter=cwd_filter, all_projects=all_projects)
    if not sessions:
        scope = ", ".join(sources)
        where = cwd_filter or ("ALL projects" if all_projects else os.getcwd())
        print(f"No sessions found ({scope}) in {where}.", file=sys.stderr)
        if skipped:
            print(f"  unavailable sources: {', '.join(skipped)}", file=sys.stderr)
        return 1

    lines = [session_line(s) for s in sessions]
    preview_cmd = _self_invoke("--preview-session", "{1}", "{2}")
    help_cmd = _self_invoke("--help-keys", "sessions")
    # Multi-line --header: status on top, dedicated shortcut row below so the
    # cheatsheet always fits on its own line regardless of terminal width.
    status = f"{len(sessions)} sessions [{', '.join(s for s in sources if s not in skipped)}]"
    if skipped:
        status += f"  (unavailable: {', '.join(skipped)})"
    shortcuts = "Enter: open  •  ?: help  •  Esc: quit  •  type to filter"
    header = f"{status}\n{shortcuts}"
    bindings = [f"?:execute({help_cmd})"]

    while True:
        selected = run_fzf(
            lines,
            header=header,
            preview=preview_cmd,
            with_nth="4..",
            initial_query=query,
            prompt="ccs> ",
            bindings=bindings,
        )
        if not selected:
            return 0
        # Only apply the initial query on the first round so returning from
        # message view doesn't keep re-pre-filling the filter.
        query = None
        parts = selected.split("\t")
        if len(parts) < 3:
            continue
        tool, locator = parts[0], parts[1]
        browse_messages(tool, locator)


def browse_messages(tool: str, locator: str) -> int:
    adapter = adapter_for_tool(tool)
    records = adapter.messages(locator)
    if not records:
        print(f"No messages found in {tool}:{locator}", file=sys.stderr)
        return 1

    lines = []
    for rec in records:
        summary = truncate(rec.summary or rec.body.replace("\n", " "), 100)
        lines.append("\t".join([
            str(rec.index),
            f"{rec.index:04d}",
            f"{rec.timestamp or '-':<16}",
            f"{rec.type[:14]:<14}",
            f"{rec.role[:10]:<10}",
            summary,
        ]))
    preview_cmd = _self_invoke("--preview-message", tool, locator, "{1}")
    copy_session_cmd = _self_invoke("--copy-session", tool, locator)
    # {1} = message index (visible in row column 1, hidden via --with-nth=2..)
    copy_message_cmd = _self_invoke("--copy-message", tool, locator, "{1}")
    help_cmd = _self_invoke("--help-keys", "messages")
    # Multi-line --header: status on top, shortcut row below — fzf prints each
    # newline-separated line as its own header row, keeping the cheatsheet
    # legible even when terminal width is tight.
    status = f"{tool} • {len(records)} messages"
    shortcuts = "Enter: open  •  y: copy all  •  Y: copy one  •  ?: help  •  Esc: back"
    header = f"{status}\n{shortcuts}"
    # execute-silent runs the copy without redrawing the screen; change-header
    # gives non-modal feedback so the user knows it succeeded but stays in the
    # picker with their selection intact.
    bindings = [
        f"y:execute-silent({copy_session_cmd})+change-header(✓ Copied conversation to clipboard)",
        f"Y:execute-silent({copy_message_cmd})+change-header(✓ Copied message to clipboard)",
        f"?:execute({help_cmd})",
    ]

    while True:
        selected = run_fzf(
            lines,
            header=header,
            preview=preview_cmd,
            with_nth="2..",
            prompt=f"{tool}> ",
            bindings=bindings,
        )
        if not selected:
            return 0
        try:
            idx = int(selected.split("\t", 1)[0])
        except ValueError:
            continue
        show_message(tool, locator, idx)


def copy_session(tool: str, locator: str) -> int:
    """Render every message in a session and pipe the joined markdown to the
    system clipboard. Used by the 'y' binding in the message picker."""
    adapter = adapter_for_tool(tool)
    records = adapter.messages(locator)
    if not records:
        print(f"No messages to copy in {tool}:{locator}", file=sys.stderr)
        return 1
    body = "\n\n---\n\n".join(rec.body for rec in records)
    if clipboard_copy(body):
        size = len(body)
        print(
            f"✓ Copied {len(records)} messages ({size:,} chars) "
            f"from {tool} session to clipboard",
            file=sys.stderr,
        )
        return 0
    return 1


_HELP_TEXT = {
    "sessions": """\
ccs — session picker
====================

Navigation
----------
  Enter      Open the selected session (drill into messages)
  Esc        Quit
  Ctrl-C     Quit

Other
-----
  ?          Show this help
  Type       Substring filter across tool, time, msgs, size, cwd, title

Standard fzf shortcuts
----------------------
  Ctrl-N / Ctrl-J / Down     Next item
  Ctrl-P / Ctrl-K / Up       Previous item
  Ctrl-D / Ctrl-U            Page down / up
  Ctrl-A / Ctrl-E            Start / end of query
  Ctrl-W                     Delete word
""",
    "messages": """\
ccs — message picker (inside a session)
========================================

Navigation
----------
  Enter      Open selected message in bat pager
  Esc        Back to session picker
  Ctrl-C     Quit

Clipboard
---------
  y          Copy the WHOLE conversation (all messages, joined)
  Y          Copy ONLY the currently highlighted message

Other
-----
  ?          Show this help
  Type       Substring filter across index, timestamp, type, role, summary

Standard fzf shortcuts
----------------------
  Ctrl-N / Ctrl-J / Down     Next item
  Ctrl-P / Ctrl-K / Up       Previous item
  Ctrl-D / Ctrl-U            Page down / up
""",
}


def show_help_keys(scope: str) -> int:
    text = _HELP_TEXT.get(scope)
    if text is None:
        print(
            f"unknown help scope: '{scope}'. Expected one of: {list(_HELP_TEXT)}",
            file=sys.stderr,
        )
        return 1
    pager = os.environ.get("PAGER") or ("less" if shutil_which("less") else None)
    if pager:
        try:
            subprocess.run([pager, "-R"], input=text, text=True, check=False)
            return 0
        except FileNotFoundError:
            pass
    sys.stdout.write(text)
    return 0


def copy_message(tool: str, locator: str, index: int) -> int:
    """Copy a single message's rendered markdown to clipboard. Used by the
    'Y' binding in the message picker."""
    adapter = adapter_for_tool(tool)
    records = adapter.messages(locator)
    if index < 1 or index > len(records):
        print(
            f"index out of range: got {index}, valid 1..{len(records)} "
            f"for {tool}:{locator}",
            file=sys.stderr,
        )
        return 1
    rec = records[index - 1]
    if clipboard_copy(rec.body):
        print(
            f"✓ Copied message #{rec.index} ({rec.role}, {len(rec.body):,} chars) "
            f"to clipboard",
            file=sys.stderr,
        )
        return 0
    return 1


def show_message(tool: str, locator: str, index: int) -> int:
    adapter = adapter_for_tool(tool)
    records = adapter.messages(locator)
    if index < 1 or index > len(records):
        print(f"index out of range: {index}", file=sys.stderr)
        return 1
    rec = records[index - 1]
    tmp = tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".md", delete=False)
    with tmp:
        tmp.write(rec.body)
    pager = "bat" if shutil_which("bat") else None
    if pager:
        subprocess.run([pager, "--paging=always", "--style=plain", "--language=markdown", tmp.name], check=False)
    else:
        subprocess.run([os.environ.get("PAGER", "less"), tmp.name], check=False)
    return 0


# ===========================================================================
# Preview entry points (hidden flags, invoked by fzf)
# ===========================================================================


def preview_session(tool: str, locator: str) -> int:
    adapter = adapter_for_tool(tool)
    records = adapter.messages(locator)
    lines = [
        f"# session — {tool}",
        "",
        f"- locator: {locator}",
        f"- messages: {len(records)}",
        "",
        "## Recent messages",
        "",
    ]
    for rec in records[-15:]:
        lines.append(f"- `{rec.index:04d}` {rec.timestamp or '-'} **{rec.role}** — {truncate(rec.summary, 90)}")
    sys.stdout.write("\n".join(lines) + "\n")
    return 0


def preview_message(tool: str, locator: str, index: int) -> int:
    adapter = adapter_for_tool(tool)
    records = adapter.messages(locator)
    if index < 1 or index > len(records):
        return 1
    sys.stdout.write(records[index - 1].body)
    return 0


# ===========================================================================
# CLI
# ===========================================================================


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="ccs",
        description="Multi-source session browser (Claude / Codex / Pi / Opencode).",
    )
    parser.add_argument("query", nargs="*", help="Initial fzf filter query (free text).")
    parser.add_argument("--source", action="append", choices=list(ADAPTERS.keys()),
                        help="Restrict to one source. Repeat to combine. Default: all available.")
    parser.add_argument("--here", action="store_true", help="Filter to current cwd only (default).")
    parser.add_argument("--all", "--all-projects", dest="all_projects", action="store_true",
                        help="Scan all projects/cwds.")
    parser.add_argument("--cwd", help="Filter to a specific absolute cwd.")
    parser.add_argument("--session", help="Open a JSONL path or opencode://ID directly.")
    parser.add_argument("--preview-session", nargs=2, metavar=("TOOL", "LOCATOR"),
                        help=argparse.SUPPRESS)
    parser.add_argument("--preview-message", nargs=3, metavar=("TOOL", "LOCATOR", "INDEX"),
                        help=argparse.SUPPRESS)
    parser.add_argument("--copy-session", nargs=2, metavar=("TOOL", "LOCATOR"),
                        help=argparse.SUPPRESS)
    parser.add_argument("--copy-message", nargs=3, metavar=("TOOL", "LOCATOR", "INDEX"),
                        help=argparse.SUPPRESS)
    parser.add_argument("--help-keys", metavar="SCOPE",
                        choices=["sessions", "messages"],
                        help=argparse.SUPPRESS)
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if args.preview_session:
        tool, locator = args.preview_session
        return preview_session(tool, locator)
    if args.preview_message:
        tool, locator, index = args.preview_message
        try:
            idx = int(index)
        except ValueError:
            return 1
        return preview_message(tool, locator, idx)
    if args.copy_session:
        tool, locator = args.copy_session
        return copy_session(tool, locator)
    if args.copy_message:
        tool, locator, index = args.copy_message
        try:
            idx = int(index)
        except ValueError:
            print(f"--copy-message INDEX must be an int, got '{index}'", file=sys.stderr)
            return 1
        return copy_message(tool, locator, idx)
    if args.help_keys:
        return show_help_keys(args.help_keys)

    if not shutil_which("fzf"):
        print("ccs requires fzf in PATH.", file=sys.stderr)
        return 1

    if args.session:
        # Direct mode: detect by locator shape
        if args.session.startswith(OPENCODE_LOCATOR_PREFIX):
            return browse_messages("opencode", args.session)
        path = Path(args.session).expanduser()
        if not path.exists():
            print(f"session path not found: {args.session}", file=sys.stderr)
            return 1
        tool = detect_tool_from_path(path)
        return browse_messages(tool, str(path))

    sources = args.source or list(ADAPTERS.keys())
    cwd_filter = args.cwd
    if not cwd_filter and not args.all_projects:
        cwd_filter = os.getcwd()
    query = " ".join(args.query) if args.query else None
    return browse_sessions(sources, cwd_filter=cwd_filter,
                            all_projects=args.all_projects, query=query)


def detect_tool_from_path(path: Path) -> str:
    """Best-effort tool detection from file location. Defaults to claude."""
    p = str(path.resolve())
    if str(CLAUDE_PROJECTS) in p:
        return "claude"
    if str(CODEX_SESSIONS) in p or path.name.startswith("rollout-"):
        return "codex"
    if str(PI_SESSIONS) in p:
        return "pi"
    return "claude"


if __name__ == "__main__":
    raise SystemExit(main())
