# ghostlight

> [!NOTE]
> This is completely vibe-coded. It may not be pretty or maintainable, but it solves a real problem for me.

Colored status dots on Ghostty tab titles for Claude Code sessions.

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

- macOS, Ghostty ≥ 1.3.1, Python 3, Claude Code

## Install

Ghostlight only uses stdlib Python modules, so it's simple to install:

    ./ghostlight install
    ./ghostlight doctor   # verify everything, including the one-time Automation prompt

New Claude Code sessions will pick up the hooks; running sessions will need
to be restarted.

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

Claude Code hooks call `ghostlight hook <event>`, which records session
state in `~/.local/state/ghostlight/status/` and spawns a short-lived
`ghostlight update` that retitles changed tabs via AppleScript
(`set_tab_title`). No daemon or launchd processes needed. Which tab a
session lives in is detected once at session start by writing a nonce to
the terminal's title and asking Ghostty which terminal has it.

## Debugging

    tail -f ~/.local/state/ghostlight/ghostlight.log
