#!/usr/bin/env python3
"""Engram auto-capture hooks for Claude Code."""

import hashlib
import json
import os
import sys
import time
import urllib.request

ENGRAM_API = os.getenv("ENGRAM_API_URL", "http://localhost:8585")

CAPTURE_TOOLS = {"Read", "Bash", "Edit", "Write", "MultiEdit", "Agent"}
SKIP_TOOLS = {"Glob", "Grep", "LS", "TodoRead", "TodoWrite", "WebSearch", "WebFetch"}


def store_memory(text, category="other", importance=0.5, metadata=None):
    """Fire-and-forget store. Never raises."""
    try:
        payload = json.dumps({
            "text": text,
            "category": category,
            "metadata": {"importance": importance, **(metadata or {})},
        }).encode()
        req = urllib.request.Request(
            f"{ENGRAM_API}/store",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=2)
    except Exception:
        pass


def content_hash(text):
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def load_dedup_cache(session_id):
    path = f"/tmp/engram-session-{session_id}.json"
    try:
        with open(path) as f:
            return set(json.load(f))
    except Exception:
        return set()


def save_dedup_cache(session_id, cache):
    path = f"/tmp/engram-session-{session_id}.json"
    try:
        with open(path, "w") as f:
            json.dump(list(cache), f)
    except Exception:
        pass


def handle_post_tool_use(data):
    tool_name = data.get("tool_name", "")
    if tool_name not in CAPTURE_TOOLS:
        return

    tool_input = data.get("tool_input", {})
    tool_response = str(data.get("tool_response", ""))
    session_id = data.get("session_id", "unknown")

    insight = None
    importance = 0.5

    if tool_name == "Read":
        file_path = tool_input.get("file_path", "")
        snippet = tool_response[:200].replace("\n", " ").strip()
        insight = f"Read {file_path}"
        if snippet:
            insight += f" — {snippet}"

    elif tool_name == "Bash":
        command = tool_input.get("command", "")
        if len(command) < 5 or command.strip().startswith(("ls", "cd ", "echo", "pwd")):
            return
        stdout = tool_response[:200].replace("\n", " ").strip()
        insight = f"Ran: {command}"
        if stdout:
            insight += f" → {stdout}"

    elif tool_name in ("Edit", "MultiEdit"):
        file_path = tool_input.get("file_path", "")
        old = tool_input.get("old_string", "")[:80]
        new = tool_input.get("new_string", "")[:80]
        insight = f"Edited {file_path}"
        if old and new:
            insight += f" — changed '{old}' to '{new}'"
        importance = 0.7

    elif tool_name == "Write":
        file_path = tool_input.get("file_path", "")
        content_preview = tool_input.get("content", "")[:100]
        insight = f"Created {file_path}"
        if content_preview:
            insight += f" — {content_preview}"
        importance = 0.7

    elif tool_name == "Agent":
        prompt = tool_input.get("prompt", "")[:100]
        result = tool_response[:300].replace("\n", " ").strip()
        insight = f"Agent: {prompt}"
        if result:
            insight += f" → {result}"
        importance = 0.6

    if not insight:
        return

    # Dedup
    cache = load_dedup_cache(session_id)
    h = content_hash(insight)
    if h in cache:
        return
    cache.add(h)
    save_dedup_cache(session_id, cache)

    store_memory(insight, "other", importance, {
        "session_id": session_id,
        "source": "auto-capture",
        "tool": tool_name,
    })


def handle_user_prompt(data):
    prompt = data.get("prompt", "")
    if len(prompt) < 20:
        return
    session_id = data.get("session_id", "unknown")

    # Dedup
    cache = load_dedup_cache(session_id)
    h = content_hash(prompt[:300])
    if h in cache:
        return
    cache.add(h)
    save_dedup_cache(session_id, cache)

    store_memory(prompt[:300], "fact", 0.6, {
        "session_id": session_id,
        "source": "auto-capture",
        "type": "user-prompt",
    })


def handle_session_start(data):
    """Inject last session summary as context."""
    try:
        payload = json.dumps({
            "query": "session summary",
            "limit": 1,
            "category": "fact",
        }).encode()
        req = urllib.request.Request(
            f"{ENGRAM_API}/search",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        resp = urllib.request.urlopen(req, timeout=5)
        result = json.loads(resp.read())
        results = result.get("results", [])
        if results:
            summary = results[0]
            created = summary.get("created_at", 0)
            # Only inject if less than 7 days old
            if time.time() - created < 7 * 86400:
                content = summary.get("content", "")
                print(f"[Previous session context]\n{content}")
    except Exception:
        pass


def handle_stop(data):
    """Parse session transcript and store a structured summary."""
    import re
    from collections import Counter

    session_id = data.get("session_id", "unknown")
    transcript_path = data.get("transcript_path", "")

    if not transcript_path or not os.path.exists(transcript_path):
        return

    files_modified = set()
    files_created = set()
    commands_run = []
    errors = []
    tool_counts = Counter()
    user_messages = []
    decisions = []
    line_count = 0

    decision_re = re.compile(
        r"\b(let's|we should|go with|use|switch to|pick|choose|decided|approved)\b",
        re.IGNORECASE,
    )
    error_keywords = {"error", "failed", "traceback", "exception", "denied"}

    try:
        with open(transcript_path, "r") as f:
            for line in f:
                line_count += 1
                try:
                    entry = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue

                msg_type = entry.get("type", "")

                if msg_type == "tool_use":
                    tool = entry.get("name", "")
                    tool_counts[tool] += 1
                    inp = entry.get("input", {})

                    if tool in ("Edit", "MultiEdit"):
                        fp = inp.get("file_path", "")
                        if fp:
                            files_modified.add(fp)
                    elif tool == "Write":
                        fp = inp.get("file_path", "")
                        if fp:
                            files_created.add(fp)
                    elif tool == "Bash":
                        cmd = inp.get("command", "")
                        if cmd and len(cmd) > 4:
                            commands_run.append(cmd[:120])

                elif msg_type == "human":
                    text = entry.get("content", "")
                    if isinstance(text, str) and len(text) > 10:
                        user_messages.append(text[:200])
                        if decision_re.search(text):
                            decisions.append(text[:150])

                elif msg_type == "tool_result":
                    content = str(entry.get("content", ""))
                    content_lower = content.lower()
                    if any(kw in content_lower for kw in error_keywords):
                        errors.append(content[:150])

    except (OSError, IOError):
        return

    # Don't store empty summaries
    if line_count < 5 and not files_modified and not files_created:
        return

    # Build summary
    parts = [f"Session summary (session {session_id[:8]}):"]

    if files_modified:
        parts.append(f"Files modified: {', '.join(sorted(files_modified)[:10])}")
    if files_created:
        parts.append(f"Files created: {', '.join(sorted(files_created)[:10])}")
    if commands_run:
        parts.append(f"Commands run: {'; '.join(commands_run[:5])}")
    if decisions:
        parts.append(f"Decisions: {'; '.join(decisions[:3])}")
    if errors:
        parts.append(f"Errors hit: {'; '.join(errors[:3])}")

    if tool_counts:
        activity = ", ".join(f"{count} {tool}" for tool, count in tool_counts.most_common(5))
        parts.append(f"Activity: {activity}")

    if user_messages:
        parts.append(f"User asked about: {'; '.join(user_messages[:3])}")

    summary = "\n".join(parts)

    store_memory(summary, "fact", 0.8, {
        "type": "session_summary",
        "session_id": session_id,
        "line_count": line_count,
        "source": "auto-capture",
    })

    # Clean up session dedup cache
    cache_path = f"/tmp/engram-session-{session_id}.json"
    try:
        if os.path.exists(cache_path):
            os.remove(cache_path)
    except OSError:
        pass


def main():
    if len(sys.argv) < 2:
        sys.exit(0)

    subcommand = sys.argv[1]
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
    except Exception:
        data = {}

    handlers = {
        "post-tool-use": handle_post_tool_use,
        "user-prompt": handle_user_prompt,
        "session-start": handle_session_start,
        "stop": handle_stop,
    }

    handler = handlers.get(subcommand)
    if handler:
        handler(data)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        sys.exit(0)
