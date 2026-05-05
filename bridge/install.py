"""Installers that wire the bridge into outside systems.

Claude Code's ``SessionStart`` hook — patches
``~/.claude/settings.json`` so every new agent session runs
``engram bridge pull`` and gets the pulled memories as prompt
context.

Design goals:

- **Idempotent.** Running an installer twice leaves the target in the
  same shape as running it once.
- **Safe.** We take a timestamped backup of any file we modify before
  writing new content.
- **Minimal.** We only touch our own markers. Existing hook logic and
  existing settings entries are preserved.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional


CLAUDE_SETTINGS_PATH = Path.home() / ".claude" / "settings.json"
HOOK_MARKER = "engram-bridge-pull"
HOOK_COMMAND = "engram bridge pull"

# All engram hooks (auto-capture system)
ENGRAM_HOOKS = {
    "SessionStart": {
        "id": "engram-session-start",
        "subcommand": "session-start",
        "timeout": 10000,
    },
    "PostToolUse": {
        "id": "engram-post-tool-use",
        "subcommand": "post-tool-use",
        "timeout": 5000,
    },
    "UserPromptSubmit": {
        "id": "engram-user-prompt",
        "subcommand": "user-prompt",
        "timeout": 5000,
    },
    "Stop": {
        "id": "engram-stop",
        "subcommand": "stop",
        "timeout": 30000,
    },
}


@dataclass
class InstallResult:
    settings_path: Path
    backup_path: Optional[Path]
    changed: bool
    action: str  # "added", "already-installed", "created-settings"
    message: str


def _load_settings(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _backup(path: Path) -> Optional[Path]:
    if not path.exists():
        return None
    stamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    backup = path.with_suffix(path.suffix + ".bak-" + stamp)
    shutil.copy2(path, backup)
    return backup


def _repo_root() -> Path:
    """Find the engram-memory-community repo root."""
    here = Path(__file__).resolve().parent
    # bridge/install.py → repo root is parent
    return here.parent


def _hook_entry() -> Dict[str, Any]:
    """Shape of the hook entry we register.

    Claude Code's settings.json supports a ``hooks`` section where each
    event (``SessionStart``, ``PostToolUse``, ...) maps to a list of
    matchers. Each matcher is ``{matcher, hooks: [{type, command}]}``.
    We register a single command hook that runs on every session start
    regardless of matcher.
    """
    return {
        "matcher": "*",
        "hooks": [
            {
                "type": "command",
                "command": HOOK_COMMAND,
                "id": HOOK_MARKER,
            }
        ],
    }


def _already_installed(session_start: List[Any]) -> bool:
    for item in session_start:
        if not isinstance(item, dict):
            continue
        hooks = item.get("hooks") or []
        if not isinstance(hooks, list):
            continue
        for hook in hooks:
            if not isinstance(hook, dict):
                continue
            if hook.get("id") == HOOK_MARKER:
                return True
            cmd = hook.get("command")
            if isinstance(cmd, str) and cmd.strip() == HOOK_COMMAND:
                return True
    return False


def install_all_hooks(settings_path: Path = CLAUDE_SETTINGS_PATH) -> List[InstallResult]:
    """Register all engram hooks (auto-capture, session summary, etc.)."""
    results = []
    settings = _load_settings(settings_path)
    backup_path = _backup(settings_path)

    hooks = settings.setdefault("hooks", {})
    repo_root = _repo_root()
    hook_script = repo_root / "scripts" / "hooks" / "engram_hook.py"

    changed = False
    for event_type, config in ENGRAM_HOOKS.items():
        event_hooks = hooks.setdefault(event_type, [])

        # Check if already installed (by id)
        already = any(
            h.get("id") == config["id"]
            for entry in event_hooks
            for h in entry.get("hooks", [])
        )
        if already:
            results.append(InstallResult(
                settings_path=settings_path,
                backup_path=backup_path,
                changed=False,
                action="already-installed",
                message=f"{event_type} hook '{config['id']}' already installed",
            ))
            continue

        entry = {
            "matcher": "*",
            "hooks": [{
                "type": "command",
                "command": f"python3 {hook_script} {config['subcommand']}",
                "timeout": config["timeout"],
                "id": config["id"],
            }],
        }
        event_hooks.append(entry)
        changed = True
        results.append(InstallResult(
            settings_path=settings_path,
            backup_path=backup_path,
            changed=True,
            action="added",
            message=f"Added {event_type} hook '{config['id']}'",
        ))

    if changed:
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        with settings_path.open("w", encoding="utf-8") as fh:
            json.dump(settings, fh, indent=2)

    return results


def install_claude_code_hook(
    settings_path: Optional[Path] = None,
) -> InstallResult:
    """Patch ``~/.claude/settings.json`` to register our SessionStart hook.

    Safe to call repeatedly. Returns an ``InstallResult`` describing what
    happened.
    """
    target = settings_path or CLAUDE_SETTINGS_PATH
    target.parent.mkdir(parents=True, exist_ok=True)

    existed = target.exists()
    settings = _load_settings(target)

    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        hooks = {}
    session_start = hooks.get("SessionStart")
    if not isinstance(session_start, list):
        session_start = []

    if _already_installed(session_start):
        return InstallResult(
            settings_path=target,
            backup_path=None,
            changed=False,
            action="already-installed",
            message="SessionStart hook already registered.",
        )

    backup = _backup(target) if existed else None

    session_start.append(_hook_entry())
    hooks["SessionStart"] = session_start
    settings["hooks"] = hooks

    tmp = target.with_suffix(target.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(settings, fh, indent=2)
        fh.write("\n")
    tmp.replace(target)

    action = "added" if existed else "created-settings"
    message = "Registered SessionStart hook in {}".format(target)
    if backup:
        message += " (backup: {})".format(backup.name)
    return InstallResult(
        settings_path=target,
        backup_path=backup,
        changed=True,
        action=action,
        message=message,
    )


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--all-hooks", action="store_true", help="Install all engram hooks")
    args = parser.parse_args()

    if args.all_hooks:
        results = install_all_hooks()
        for r in results:
            print(f"  {r.action}: {r.message}")
    else:
        result = install_claude_code_hook()
        print(f"  {result.action}: {result.message}")
