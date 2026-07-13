---
name: verify
description: How to run and end-to-end verify ghostlight changes on this machine — tests via uv, live CLI driving with scratch env vars, and how to restore real tab dots afterwards.
---

# Verifying ghostlight

## Tests (dev only; runtime is stdlib-only)

    uv run --with pytest python -m pytest tests/ -q

## Driving the real CLI safely

Point the CLI at scratch state so the real install (`~/.claude/settings.json`,
`~/.local/state/ghostlight/`) is never touched:

    export GHOSTLIGHT_SETTINGS=/tmp/scratch/settings.json
    export GHOSTLIGHT_STATE_DIR=/tmp/scratch/state

- `./ghostlight doctor` (no overrides) is read-only against the real install.
- `hook <event>` reads JSON on stdin: `echo '{"session_id":"x","cwd":"'$PWD'"}' | ./ghostlight hook session-start`
- Identity capture works from a Claude Code Bash tool: the hook walks up to
  the `claude` process's tty, so a fake session resolves to *this* tab.

## Gotchas

- Any `update` pass or `uninstall` reconciles/strips dots on **all** real
  Ghostty tabs, even with scratch state — other sessions' dots get stripped.
  Restore afterwards with a plain `./ghostlight update` (real env); real
  status files re-add the correct dots within a second.
- Read tab titles without side effects:

      osascript -e 'tell application "Ghostty"' -e 'set out to ""' -e 'repeat with w in windows' -e 'repeat with tb in tabs of w' -e 'set out to out & (name of tb) & linefeed' -e 'end repeat' -e 'end repeat' -e 'return out' -e 'end tell'

- The runtime needs only macOS system Python 3 (`/usr/bin/python3` 3.9 works);
  don't add pip/uv dependencies to the script itself.
