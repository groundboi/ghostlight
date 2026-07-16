# ghostlight

> [!NOTE]
> This is completely vibe-coded. It may not be pretty or maintainable, but it solves a real problem for me.

Colored status dots on Ghostty tab titles for Claude Code and Codex sessions.

| Dot | Meaning |
|-----|---------|
| 🟠 | waiting on you (permission dialog, question prompt, etc.) |
| 🔵 | compacting context |
| 🟢 | working |
| ⚪ | idle / done |
| *(none)* | no active session in this tab |

Multiple sessions in a tab show the "worst" (most attention-needing) state.
Only a leading dot and space are ever added to your tab title; the tab text is
otherwise left alone.

## Requirements

- macOS, Ghostty ≥ 1.3.1, Python 3, and Claude Code and/or Codex

## Install

Ghostlight only uses stdlib Python modules, so it's simple to install:

    ./ghostlight install
    ./ghostlight doctor   # verify everything, including the one-time Automation prompt

The installer adds hooks to both `~/.claude/settings.json` and
`~/.codex/hooks.json`, preserving hooks already configured in either file.
New sessions will pick up the hooks; running sessions need to be restarted.
Codex also requires a one-time review: open `/hooks` in Codex and trust the
new Ghostlight hooks.

If desired, you can symlink `ghostlight` to get it on your `PATH`, like
`ln -s /path/to/ghostlight ~/.local/bin/ghostlight`.

Pretty straight-forward to use: run `ghostlight -h` to get started.

## Non-goals & limitations

- Will not support all operating systems, terminals, or agent harnesses
- Will only change the _tab_ title, not individual split/pane titles
- Will not work in tmux, non-interactive sessions, SSH, etc.
- Automatic tab title updates by programs in the tab will be halted, though
  `ghostlight` can handle manual tab title updates and will continue to work afterwards.

## How it works

Claude Code and Codex hooks call `ghostlight hook <event>`, which record
session state in `~/.local/state/ghostlight/status/` and spawn a short-lived
`ghostlight update` that retitles changed tabs via AppleScript
(`set_tab_title`). No daemon or launchd processes needed. Which tab a
session lives in is detected once at session start by writing a nonce to
the terminal's title and asking Ghostty which terminal has it.

Codex does not currently provide a `SessionEnd` hook. After a Codex session's
tab closes, its state file is cleaned up opportunistically the next time any
Ghostlight hook triggers an update.

## Debugging

    tail -f ~/.local/state/ghostlight/ghostlight.log
