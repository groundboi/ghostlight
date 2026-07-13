import fcntl
import io
import json
import os
import time

import ghostlight


class TestScaffold:
    def test_constants(self):
        assert ghostlight.DOTS == {
            "waiting": "🟠", "compacting": "🔵", "working": "🟢", "idle": "⚪",
        }
        assert ghostlight.PRIORITY == ["waiting", "compacting", "working", "idle"]
        assert ghostlight.TTL_SECONDS == 12 * 3600
        assert ghostlight.STATE_TTLS == {"idle": None, "waiting": 7 * 24 * 3600}

    def test_state_dir_env_override(self, state_env):
        assert ghostlight.state_dir() == state_env
        assert ghostlight.status_dir() == state_env / "status"

    def test_log_writes_and_never_raises(self, state_env):
        ghostlight.log("hello")
        assert "hello" in ghostlight.log_path().read_text()

    def test_log_rotates_instead_of_deleting(self, state_env):
        ghostlight.state_dir().mkdir(parents=True, exist_ok=True)
        p = ghostlight.log_path()
        p.write_text("old history\n" + "x" * 1_100_000)
        ghostlight.log("fresh line")
        old = p.with_name(p.name + ".old")
        assert old.read_text().startswith("old history")
        assert "fresh line" in p.read_text()

    def test_log_rotation_overwrites_previous_old(self, state_env):
        ghostlight.state_dir().mkdir(parents=True, exist_ok=True)
        p = ghostlight.log_path()
        old = p.with_name(p.name + ".old")
        old.write_text("ancient")
        p.write_text("recent\n" + "x" * 1_100_000)
        ghostlight.log("fresh")
        assert old.read_text().startswith("recent")


class TestTitleLogic:
    def test_strip_plain_name_unchanged(self):
        assert ghostlight.strip_known_dot("demo-migration") == "demo-migration"

    def test_strip_removes_leading_dot_and_space(self):
        assert ghostlight.strip_known_dot("🟢 demo") == "demo"
        assert ghostlight.strip_known_dot("🟠 demo") == "demo"
        assert ghostlight.strip_known_dot("🔵 demo") == "demo"
        assert ghostlight.strip_known_dot("⚪ demo") == "demo"

    def test_strip_only_one_dot(self):
        assert ghostlight.strip_known_dot("🟢 🟠 x") == "🟠 x"

    def test_strip_bare_dot(self):
        assert ghostlight.strip_known_dot("🟢") == ""

    def test_strip_ignores_dot_without_space(self):
        assert ghostlight.strip_known_dot("🟢x") == "🟢x"

    def test_make_title_adds_dot(self):
        assert ghostlight.make_title("working", "demo") == "🟢 demo"

    def test_make_title_replaces_dot(self):
        assert ghostlight.make_title("waiting", "🟢 demo") == "🟠 demo"

    def test_make_title_none_strips(self):
        assert ghostlight.make_title(None, "🟢 demo") == "demo"
        assert ghostlight.make_title(None, "demo") == "demo"

    def test_worst_state_priority(self):
        assert ghostlight.worst_state(["idle", "working"]) == "working"
        assert ghostlight.worst_state(["working", "waiting", "idle"]) == "waiting"
        assert ghostlight.worst_state(["compacting", "working"]) == "compacting"
        assert ghostlight.worst_state(["idle"]) == "idle"

    def test_worst_state_empty_or_unknown(self):
        assert ghostlight.worst_state([]) is None
        assert ghostlight.worst_state(["bogus"]) is None


def _entry(session="sess-1", tab="tab-abc", state="working"):
    return {
        "session_id": session, "tab_id": tab, "terminal_id": "TERM-UUID",
        "cwd": "/tmp/proj", "state": state, "updated_at": ghostlight.now_iso(),
    }


class TestStatusStore:
    def test_write_and_read_roundtrip(self, state_env):
        p = ghostlight.write_status(_entry())
        assert p.name == "tab-abc__sess-1.json"
        entries = ghostlight.read_status_files()
        assert len(entries) == 1
        assert entries[0]["state"] == "working"
        assert entries[0]["_path"] == p

    def test_no_tmp_files_left(self, state_env):
        ghostlight.write_status(_entry())
        assert list(ghostlight.status_dir().glob("*.tmp")) == []

    def test_corrupt_file_deleted_and_skipped(self, state_env):
        ghostlight.write_status(_entry())
        bad = ghostlight.status_dir() / "tab-x__sess-x.json"
        bad.write_text("{not json")
        entries = ghostlight.read_status_files()
        assert len(entries) == 1
        assert not bad.exists()

    def test_find_session_files(self, state_env):
        ghostlight.write_status(_entry(session="a", tab="t1"))
        ghostlight.write_status(_entry(session="b", tab="t1"))
        found = ghostlight.find_session_files("a")
        assert [p.name for p in found] == ["t1__a.json"]

    def test_update_session_state_changes(self, state_env):
        ghostlight.write_status(_entry(session="a"))
        assert ghostlight.update_session_state("a", "idle") is True
        assert ghostlight.read_status_files()[0]["state"] == "idle"

    def test_update_session_state_noop_when_same(self, state_env):
        ghostlight.write_status(_entry(session="a", state="idle"))
        assert ghostlight.update_session_state("a", "idle") is False

    def test_update_session_state_missing_session(self, state_env):
        assert ghostlight.update_session_state("ghost", "idle") is False

    def test_delete_session(self, state_env):
        ghostlight.write_status(_entry(session="a"))
        ghostlight.delete_session("a")
        assert ghostlight.read_status_files() == []

    def test_sweep_ttl(self, state_env):
        fresh = ghostlight.write_status(_entry(session="new"))
        old = ghostlight.write_status(_entry(session="old", tab="tab-old"))
        past = time.time() - ghostlight.TTL_SECONDS - 60
        os.utime(old, (past, past))
        ghostlight.sweep_ttl()
        assert fresh.exists() and not old.exists()

    def test_sweep_ttl_spares_idle_however_old(self, state_env):
        # idle means "no events" by definition — silence is not death
        p = ghostlight.write_status(_entry(session="idler", state="idle"))
        past = time.time() - 30 * 24 * 3600
        os.utime(p, (past, past))
        ghostlight.sweep_ttl()
        assert p.exists()

    def test_sweep_ttl_waiting_survives_default_ttl(self, state_env):
        # an unanswered permission prompt can legitimately sit overnight+
        p = ghostlight.write_status(_entry(session="w", state="waiting"))
        past = time.time() - ghostlight.TTL_SECONDS - 60
        os.utime(p, (past, past))
        ghostlight.sweep_ttl()
        assert p.exists()

    def test_sweep_ttl_waiting_removed_after_long_ttl(self, state_env):
        p = ghostlight.write_status(_entry(session="w", state="waiting"))
        past = time.time() - ghostlight.STATE_TTLS["waiting"] - 60
        os.utime(p, (past, past))
        ghostlight.sweep_ttl()
        assert not p.exists()

    def test_update_same_state_still_refreshes_mtime(self, state_env):
        # long single-turn runs stay in "working"; each event must reset
        # the silence clock or the TTL would sweep a live session mid-run
        p = ghostlight.write_status(_entry(session="a", state="working"))
        past = time.time() - 6 * 3600
        os.utime(p, (past, past))
        assert ghostlight.update_session_state("a", "working") is False
        assert p.stat().st_mtime > past + 3600


class TestUpdateLoop:
    def test_solo_run_calls_pass_once_and_clears_dirty(self, state_env):
        calls = []
        assert ghostlight.cmd_update(pass_fn=lambda: calls.append(1)) == 0
        assert calls == [1]
        assert not ghostlight.dirty_path().exists()

    def test_contention_exits_leaving_dirty(self, state_env):
        state_env.mkdir(parents=True, exist_ok=True)
        holder = open(ghostlight.lock_path(), "w")
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            calls = []
            assert ghostlight.cmd_update(pass_fn=lambda: calls.append(1)) == 0
            assert calls == []
            assert ghostlight.dirty_path().exists()
        finally:
            fcntl.flock(holder, fcntl.LOCK_UN)
            holder.close()

    def test_dirty_during_pass_triggers_second_pass(self, state_env):
        calls = []

        def pass_fn():
            calls.append(1)
            if len(calls) == 1:
                ghostlight.dirty_path().touch()

        assert ghostlight.cmd_update(pass_fn=pass_fn) == 0
        assert len(calls) == 2
        assert not ghostlight.dirty_path().exists()

    def test_pass_exception_is_swallowed_and_logged(self, state_env):
        def boom():
            raise RuntimeError("pass failed")

        assert ghostlight.cmd_update(pass_fn=boom) == 0
        assert "pass failed" in ghostlight.log_path().read_text()


class TestOsascript:
    def test_run_osascript_roundtrip_real(self):
        out = ghostlight.run_osascript(
            'on run argv\nreturn item 1 of argv\nend run', "hello")
        assert out == "hello"

    def test_run_osascript_error_returns_none(self, state_env):
        assert ghostlight.run_osascript("this is ( not applescript") is None

    def test_ghostty_running_parses_ps(self, monkeypatch):
        class R:
            returncode = 0
            stdout = "/sbin/launchd\n/Applications/Ghostty.app/Contents/MacOS/ghostty\n"
        monkeypatch.setattr(ghostlight.subprocess, "run", lambda *a, **k: R())
        assert ghostlight.ghostty_running() is True
        R.stdout = "/sbin/launchd\n/usr/bin/top\n"
        assert ghostlight.ghostty_running() is False

    def test_enumerate_tabs_parses_records(self, monkeypatch):
        monkeypatch.setattr(ghostlight, "ghostty_running", lambda: True)
        US, RS = ghostlight.US, ghostlight.RS
        raw = (f"tab-1{US}TERM-A{US}🟢 demo{RS}"
               f"tab-2{US}TERM-B{US}plain name{RS}")
        monkeypatch.setattr(ghostlight, "run_osascript", lambda *a: raw)
        tabs = ghostlight.enumerate_tabs()
        assert tabs == [
            {"tab_id": "tab-1", "terminal_id": "TERM-A", "name": "🟢 demo"},
            {"tab_id": "tab-2", "terminal_id": "TERM-B", "name": "plain name"},
        ]

    def test_enumerate_tabs_not_running(self, monkeypatch):
        monkeypatch.setattr(ghostlight, "ghostty_running", lambda: False)
        assert ghostlight.enumerate_tabs() is None

    def test_enumerate_tabs_script_failure(self, monkeypatch):
        monkeypatch.setattr(ghostlight, "ghostty_running", lambda: True)
        monkeypatch.setattr(ghostlight, "run_osascript", lambda *a: None)
        assert ghostlight.enumerate_tabs() is None

    def test_enumerate_tabs_salvages_control_char_title(
            self, state_env, monkeypatch):
        monkeypatch.setattr(ghostlight, "ghostty_running", lambda: True)
        US, RS = ghostlight.US, ghostlight.RS
        raw = f"tab-1{US}TERM-A{US}weird{US}title{RS}"
        monkeypatch.setattr(ghostlight, "run_osascript", lambda *a: raw)
        tabs = ghostlight.enumerate_tabs()
        assert tabs == [
            {"tab_id": "tab-1", "terminal_id": "TERM-A",
             "name": f"weird{US}title"},
        ]

    def test_enumerate_tabs_drops_and_logs_malformed_record(
            self, state_env, monkeypatch):
        monkeypatch.setattr(ghostlight, "ghostty_running", lambda: True)
        US, RS = ghostlight.US, ghostlight.RS
        raw = f"tab-1{US}TERM-A{RS}"
        monkeypatch.setattr(ghostlight, "run_osascript", lambda *a: raw)
        tabs = ghostlight.enumerate_tabs()
        assert tabs == []
        assert "malformed" in ghostlight.log_path().read_text()

    def test_set_tab_title_success_and_failure(self, monkeypatch):
        monkeypatch.setattr(ghostlight, "ghostty_running", lambda: True)
        seen = {}

        def fake(script, *args):
            seen["args"] = args
            return ""
        monkeypatch.setattr(ghostlight, "run_osascript", fake)
        assert ghostlight.set_tab_title("TERM-A", "🟢 demo") is True
        assert seen["args"] == ("TERM-A", "🟢 demo")
        monkeypatch.setattr(ghostlight, "run_osascript", lambda *a: None)
        assert ghostlight.set_tab_title("TERM-A", "x") is False

    def test_set_tab_title_not_running_skips_osascript(self, monkeypatch):
        monkeypatch.setattr(ghostlight, "ghostty_running", lambda: False)
        calls = []

        def fake(script, *args):
            calls.append(args)
            return ""
        monkeypatch.setattr(ghostlight, "run_osascript", fake)
        assert ghostlight.set_tab_title("TERM-A", "🟢 demo") is False
        assert calls == []


class TestUpdatePass:
    def _setup(self, monkeypatch, tabs, writes):
        monkeypatch.setattr(ghostlight, "enumerate_tabs", lambda: tabs)
        monkeypatch.setattr(
            ghostlight, "set_tab_title",
            lambda tid, title: writes.append((tid, title)) or True)

    def test_applies_dot_to_tab_with_session(self, state_env, monkeypatch):
        ghostlight.write_status(_entry(session="a", tab="tab-1", state="working"))
        writes = []
        self._setup(monkeypatch,
                    [{"tab_id": "tab-1", "terminal_id": "T1", "name": "demo"}],
                    writes)
        ghostlight.run_update_pass()
        assert writes == [("T1", "🟢 demo")]

    def test_worst_state_wins_across_sessions(self, state_env, monkeypatch):
        ghostlight.write_status(_entry(session="a", tab="tab-1", state="idle"))
        ghostlight.write_status(_entry(session="b", tab="tab-1", state="waiting"))
        writes = []
        self._setup(monkeypatch,
                    [{"tab_id": "tab-1", "terminal_id": "T1", "name": "demo"}],
                    writes)
        ghostlight.run_update_pass()
        assert writes == [("T1", "🟠 demo")]

    def test_no_write_when_title_already_correct(self, state_env, monkeypatch):
        ghostlight.write_status(_entry(session="a", tab="tab-1", state="working"))
        writes = []
        self._setup(monkeypatch,
                    [{"tab_id": "tab-1", "terminal_id": "T1", "name": "🟢 demo"}],
                    writes)
        ghostlight.run_update_pass()
        assert writes == []

    def test_strips_dot_when_no_sessions(self, state_env, monkeypatch):
        writes = []
        self._setup(monkeypatch,
                    [{"tab_id": "tab-1", "terminal_id": "T1", "name": "🟠 demo"},
                     {"tab_id": "tab-2", "terminal_id": "T2", "name": "plain"}],
                    writes)
        ghostlight.run_update_pass()
        assert writes == [("T1", "demo")]

    def test_deletes_stale_entries_for_missing_tabs(self, state_env, monkeypatch):
        p = ghostlight.write_status(_entry(session="a", tab="tab-gone"))
        writes = []
        self._setup(monkeypatch,
                    [{"tab_id": "tab-1", "terminal_id": "T1", "name": "x"}],
                    writes)
        ghostlight.run_update_pass()
        assert not p.exists()

    def test_ghostty_not_running_keeps_files(self, state_env, monkeypatch):
        p = ghostlight.write_status(_entry(session="a", tab="tab-1"))
        monkeypatch.setattr(ghostlight, "enumerate_tabs", lambda: None)
        ghostlight.run_update_pass()
        assert p.exists()

    def test_skips_retitle_during_nonce_identity_capture(
            self, state_env, monkeypatch):
        p = ghostlight.write_status(_entry(session="a", tab="tab-1"))
        writes = []
        self._setup(monkeypatch,
                    [{"tab_id": "tab-1", "terminal_id": "T1",
                      "name": "ghostlight-deadbeef-123"}],
                    writes)
        ghostlight.run_update_pass()
        assert writes == []
        assert p.exists()

    def test_ghostlight_prefixed_user_tab_still_gets_dot(
            self, state_env, monkeypatch):
        # only the exact nonce shape is skipped, not any ghostlight-* name
        ghostlight.write_status(_entry(session="a", tab="tab-1"))
        writes = []
        self._setup(monkeypatch,
                    [{"tab_id": "tab-1", "terminal_id": "T1",
                      "name": "ghostlight-dev"}],
                    writes)
        ghostlight.run_update_pass()
        assert writes == [("T1", "🟢 ghostlight-dev")]

    def test_skips_retitle_during_transient_empty_title(
            self, state_env, monkeypatch):
        p = ghostlight.write_status(_entry(session="a", tab="tab-1"))
        writes = []
        self._setup(monkeypatch,
                    [{"tab_id": "tab-1", "terminal_id": "T1", "name": ""}],
                    writes)
        ghostlight.run_update_pass()
        assert writes == []
        assert p.exists()

    def test_skips_retitle_when_dot_with_empty_base(
            self, state_env, monkeypatch):
        p = ghostlight.write_status(_entry(session="a", tab="tab-1"))
        writes = []
        self._setup(monkeypatch,
                    [{"tab_id": "tab-1", "terminal_id": "T1", "name": "🟢 "}],
                    writes)
        ghostlight.run_update_pass()
        assert writes == []
        assert p.exists()


class TestIdentityCapture:
    def test_find_tty_walks_to_ancestor(self, monkeypatch):
        # pid 100 (self) has no tty; parent 50 is claude on ttys007
        table = {"100": ("50", "??"), "50": ("40", "ttys007")}

        def fake_run(cmd, **kw):
            pid = cmd[-1]
            ppid, tty = table[pid]

            class R:
                stdout = f"{ppid} {tty}\n"
            return R()
        monkeypatch.setattr(ghostlight.subprocess, "run", fake_run)
        monkeypatch.setattr(ghostlight.os, "getpid", lambda: 100)
        assert ghostlight.find_tty() == "/dev/ttys007"

    def test_find_tty_gives_up_at_init(self, monkeypatch):
        table = {"100": ("1", "??"), "1": ("0", "??")}

        def fake_run(cmd, **kw):
            ppid, tty = table[cmd[-1]]

            class R:
                stdout = f"{ppid} {tty}\n"
            return R()
        monkeypatch.setattr(ghostlight.subprocess, "run", fake_run)
        monkeypatch.setattr(ghostlight.os, "getpid", lambda: 100)
        assert ghostlight.find_tty() is None

    def test_write_title_escape_via_pty(self):
        master, slave = os.openpty()
        try:
            ghostlight.write_title_escape(os.ttyname(slave), "NONCE-1")
            data = os.read(master, 100)
            assert data == b"\x1b]2;NONCE-1\x07"
        finally:
            os.close(master)
            os.close(slave)

    def _no_sleep(self, monkeypatch):
        monkeypatch.setattr(ghostlight.time, "sleep", lambda s: None)

    def test_resolve_via_nonce(self, state_env, monkeypatch):
        self._no_sleep(monkeypatch)
        monkeypatch.setattr(ghostlight, "ghostty_running", lambda: True)
        monkeypatch.setattr(ghostlight, "find_tty", lambda: "/dev/ttys007")
        written = []
        monkeypatch.setattr(ghostlight, "write_title_escape",
                            lambda tty, text: written.append(text))
        US = ghostlight.US

        def fake_osa(script, *args):
            assert script is ghostlight.FIND_BY_NAME_SCRIPT
            return f"TERM-A{US}tab-1"
        monkeypatch.setattr(ghostlight, "run_osascript", fake_osa)
        assert ghostlight.resolve_identity("/p", "sess-12345678") == ("TERM-A", "tab-1")
        assert written[0].startswith("ghostlight-sess-123")
        assert written[-1] == ""  # title cleared afterwards

    def test_resolve_falls_back_to_cwd(self, state_env, monkeypatch):
        self._no_sleep(monkeypatch)
        monkeypatch.setattr(ghostlight, "ghostty_running", lambda: True)
        monkeypatch.setattr(ghostlight, "find_tty", lambda: None)
        US = ghostlight.US

        def fake_osa(script, *args):
            if script is ghostlight.FIND_BY_CWD_SCRIPT and args == ("/p",):
                return f"TERM-B{US}tab-2"
            return ""
        monkeypatch.setattr(ghostlight, "run_osascript", fake_osa)
        assert ghostlight.resolve_identity("/p", "s") == ("TERM-B", "tab-2")

    def test_resolve_falls_back_to_front_window(self, state_env, monkeypatch):
        self._no_sleep(monkeypatch)
        monkeypatch.setattr(ghostlight, "ghostty_running", lambda: True)
        monkeypatch.setattr(ghostlight, "find_tty", lambda: None)
        US = ghostlight.US

        def fake_osa(script, *args):
            if script is ghostlight.FRONT_WINDOW_SCRIPT:
                return f"TERM-C{US}tab-3"
            return ""
        monkeypatch.setattr(ghostlight, "run_osascript", fake_osa)
        assert ghostlight.resolve_identity("/p", "s") == ("TERM-C", "tab-3")

    def test_resolve_all_fail(self, state_env, monkeypatch):
        self._no_sleep(monkeypatch)
        monkeypatch.setattr(ghostlight, "ghostty_running", lambda: True)
        monkeypatch.setattr(ghostlight, "find_tty", lambda: None)
        monkeypatch.setattr(ghostlight, "run_osascript", lambda *a: "")
        assert ghostlight.resolve_identity("/p", "s") is None

    def test_resolve_ghostty_not_running(self, state_env, monkeypatch):
        monkeypatch.setattr(ghostlight, "ghostty_running", lambda: False)
        monkeypatch.setattr(ghostlight, "find_tty",
                            lambda: (_ for _ in ()).throw(AssertionError("should not be called")))
        assert ghostlight.resolve_identity("/p", "s") is None

    def test_nonce_write_failure_still_clears_title(self, state_env, monkeypatch):
        self._no_sleep(monkeypatch)
        monkeypatch.setattr(ghostlight, "ghostty_running", lambda: True)
        monkeypatch.setattr(ghostlight, "find_tty", lambda: "/dev/ttys007")
        monkeypatch.setattr(ghostlight, "run_osascript", lambda *a: "")
        cleared = []

        def fake_write(tty, text):
            if text == "":
                cleared.append(text)
                return
            raise OSError("tty gone")
        monkeypatch.setattr(ghostlight, "write_title_escape", fake_write)
        assert ghostlight.resolve_identity("/p", "s") is None
        assert cleared == [""]  # clear was attempted despite nonce write failure


def _feed(monkeypatch, payload):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))


class TestHookCommand:
    def _spy_spawn(self, monkeypatch):
        spawns = []
        monkeypatch.setattr(ghostlight, "spawn_update", lambda: spawns.append(1))
        return spawns

    def test_session_start_creates_entry_and_spawns(self, state_env, monkeypatch):
        spawns = self._spy_spawn(monkeypatch)
        monkeypatch.setattr(ghostlight, "resolve_identity",
                            lambda cwd, sid: ("TERM-A", "tab-1"))
        _feed(monkeypatch, {"session_id": "s1", "cwd": "/p", "source": "startup"})
        assert ghostlight.cmd_hook("session-start") == 0
        e = ghostlight.read_status_files()[0]
        assert (e["tab_id"], e["terminal_id"], e["state"], e["cwd"]) == \
            ("tab-1", "TERM-A", "idle", "/p")
        assert spawns == [1]

    def test_session_start_moves_tab_on_reresolve(self, state_env, monkeypatch):
        self._spy_spawn(monkeypatch)
        ghostlight.write_status(_entry(session="s1", tab="tab-old"))
        monkeypatch.setattr(ghostlight, "resolve_identity",
                            lambda cwd, sid: ("TERM-N", "tab-new"))
        _feed(monkeypatch, {"session_id": "s1", "cwd": "/p"})
        ghostlight.cmd_hook("session-start")
        names = [p.name for p in ghostlight.find_session_files("s1")]
        assert names == ["tab-new__s1.json"]

    def test_session_start_resolution_failure_refreshes_existing(
            self, state_env, monkeypatch):
        self._spy_spawn(monkeypatch)
        ghostlight.write_status(_entry(session="s1", tab="tab-old", state="working"))
        monkeypatch.setattr(ghostlight, "resolve_identity", lambda cwd, sid: None)
        _feed(monkeypatch, {"session_id": "s1", "cwd": "/p"})
        ghostlight.cmd_hook("session-start")
        assert ghostlight.read_status_files()[0]["state"] == "idle"

    def test_session_start_compact_source_skips_reresolve(
            self, state_env, monkeypatch):
        self._spy_spawn(monkeypatch)
        ghostlight.write_status(_entry(session="s1", tab="tab-old", state="idle"))

        def boom(cwd, sid):
            raise AssertionError("resolve_identity should not be called")
        monkeypatch.setattr(ghostlight, "resolve_identity", boom)
        _feed(monkeypatch,
              {"session_id": "s1", "cwd": "/p", "source": "compact"})
        assert ghostlight.cmd_hook("session-start") == 0
        assert ghostlight.read_status_files()[0]["state"] == "working"

    def test_session_start_clear_source_goes_idle(self, state_env, monkeypatch):
        self._spy_spawn(monkeypatch)
        ghostlight.write_status(_entry(session="s1", tab="tab-old", state="working"))

        def boom(cwd, sid):
            raise AssertionError("resolve_identity should not be called")
        monkeypatch.setattr(ghostlight, "resolve_identity", boom)
        _feed(monkeypatch, {"session_id": "s1", "cwd": "/p", "source": "clear"})
        assert ghostlight.cmd_hook("session-start") == 0
        assert ghostlight.read_status_files()[0]["state"] == "idle"

    def test_session_start_resume_source_still_reresolves(
            self, state_env, monkeypatch):
        self._spy_spawn(monkeypatch)
        ghostlight.write_status(_entry(session="s1", tab="tab-old"))
        monkeypatch.setattr(ghostlight, "resolve_identity",
                            lambda cwd, sid: ("TERM-N", "tab-new"))
        _feed(monkeypatch,
              {"session_id": "s1", "cwd": "/p", "source": "resume"})
        ghostlight.cmd_hook("session-start")
        names = [p.name for p in ghostlight.find_session_files("s1")]
        assert names == ["tab-new__s1.json"]

    def test_state_event_updates_and_spawns_once(self, state_env, monkeypatch):
        spawns = self._spy_spawn(monkeypatch)
        ghostlight.write_status(_entry(session="s1", state="working"))
        _feed(monkeypatch, {"session_id": "s1", "cwd": "/p"})
        ghostlight.cmd_hook("stop")
        assert ghostlight.read_status_files()[0]["state"] == "idle"
        assert spawns == [1]
        # same state again -> no second spawn
        _feed(monkeypatch, {"session_id": "s1", "cwd": "/p"})
        ghostlight.cmd_hook("stop")
        assert spawns == [1]

    def test_all_event_states(self, state_env, monkeypatch):
        self._spy_spawn(monkeypatch)
        expected = {
            "user-prompt-submit": "working", "pretooluse": "working",
            "postcompact": "working", "notification": "waiting",
            "precompact": "compacting", "stop": "idle",
        }
        for event, state in expected.items():
            ghostlight.delete_session("s1")
            ghostlight.write_status(_entry(session="s1", state="none-yet"))
            _feed(monkeypatch, {"session_id": "s1", "cwd": "/p"})
            ghostlight.cmd_hook(event)
            assert ghostlight.read_status_files()[0]["state"] == state, event

    def test_event_for_unknown_session_is_noop(self, state_env, monkeypatch):
        spawns = self._spy_spawn(monkeypatch)
        _feed(monkeypatch, {"session_id": "ghost", "cwd": "/p"})
        ghostlight.cmd_hook("pretooluse")
        assert ghostlight.read_status_files() == []
        assert spawns == []

    def test_stop_recovers_lost_status_file(self, state_env, monkeypatch):
        # status file swept while session alive -> next event recreates it
        spawns = self._spy_spawn(monkeypatch)
        monkeypatch.setattr(ghostlight, "resolve_identity",
                            lambda cwd, sid: ("TERM-A", "tab-1"))
        _feed(monkeypatch, {"session_id": "s1", "cwd": "/p"})
        ghostlight.cmd_hook("stop")
        e = ghostlight.read_status_files()[0]
        assert (e["tab_id"], e["terminal_id"], e["state"], e["cwd"]) == \
            ("tab-1", "TERM-A", "idle", "/p")
        assert spawns == [1]

    def test_pretooluse_never_attempts_recovery(self, state_env, monkeypatch):
        spawns = self._spy_spawn(monkeypatch)
        calls = []
        monkeypatch.setattr(ghostlight, "resolve_identity",
                            lambda cwd, sid: calls.append(1) or None)
        _feed(monkeypatch, {"session_id": "ghost", "cwd": "/p"})
        ghostlight.cmd_hook("pretooluse")
        assert calls == []
        assert ghostlight.read_status_files() == []
        assert spawns == []

    def test_recovery_resolution_failure_stays_dotless(
            self, state_env, monkeypatch):
        spawns = self._spy_spawn(monkeypatch)
        monkeypatch.setattr(ghostlight, "resolve_identity", lambda cwd, sid: None)
        _feed(monkeypatch, {"session_id": "ghost", "cwd": "/p"})
        assert ghostlight.cmd_hook("stop") == 0
        assert ghostlight.read_status_files() == []
        assert spawns == []

    def test_session_end_deletes_and_spawns(self, state_env, monkeypatch):
        spawns = self._spy_spawn(monkeypatch)
        ghostlight.write_status(_entry(session="s1"))
        _feed(monkeypatch, {"session_id": "s1", "cwd": "/p"})
        ghostlight.cmd_hook("session-end")
        assert ghostlight.read_status_files() == []
        assert spawns == [1]

    def test_bad_json_returns_zero(self, state_env, monkeypatch):
        self._spy_spawn(monkeypatch)
        monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
        assert ghostlight.cmd_hook("stop") == 0

    def test_interactive_tty_stdin_is_treated_as_empty(
            self, state_env, monkeypatch):
        # a human poking at `ghostlight hook X` must not hang on stdin
        spawns = self._spy_spawn(monkeypatch)

        class FakeTty:
            def isatty(self):
                return True

            def read(self, *a):
                raise AssertionError("must not read from an interactive tty")
        monkeypatch.setattr("sys.stdin", FakeTty())
        ghostlight._hook_impl("stop")
        assert spawns == []

    def test_exception_inside_still_returns_zero(self, state_env, monkeypatch):
        def boom(cwd, sid):
            raise RuntimeError("kaboom")
        monkeypatch.setattr(ghostlight, "resolve_identity", boom)
        monkeypatch.setattr(ghostlight, "spawn_update", lambda: None)
        _feed(monkeypatch, {"session_id": "s1", "cwd": "/p"})
        assert ghostlight.cmd_hook("session-start") == 0


class TestSettingsMerge:
    CMD = "/abs/ghostlight"

    def test_merge_into_empty(self):
        out = ghostlight.merge_hooks({}, self.CMD)
        assert len(out["hooks"]["SessionStart"]) == 1
        assert out["hooks"]["SessionStart"][0]["hooks"][0]["command"] == \
            f"{self.CMD} hook session-start"
        matchers = {s["matcher"] for s in out["hooks"]["Notification"]}
        assert matchers == {"permission_prompt"}
        # every event from HOOK_EVENTS is present
        assert {ev for ev, _, _ in ghostlight.HOOK_EVENTS} <= set(out["hooks"])

    def test_merge_preserves_user_hooks_and_settings(self):
        settings = {
            "model": "opus",
            "hooks": {"Stop": [{"hooks": [
                {"type": "command", "command": "/usr/bin/say done"}]}]},
        }
        out = ghostlight.merge_hooks(settings, self.CMD)
        assert out["model"] == "opus"
        stop_cmds = [h["command"] for s in out["hooks"]["Stop"] for h in s["hooks"]]
        assert "/usr/bin/say done" in stop_cmds
        assert f"{self.CMD} hook stop" in stop_cmds

    def test_merge_is_idempotent(self):
        once = ghostlight.merge_hooks({}, self.CMD)
        twice = ghostlight.merge_hooks(once, self.CMD)
        assert once == twice

    def test_remove_restores_original(self):
        settings = {"hooks": {"Stop": [{"hooks": [
            {"type": "command", "command": "/usr/bin/say done"}]}]}}
        merged = ghostlight.merge_hooks(settings, self.CMD)
        assert ghostlight.remove_hooks(merged) == settings

    def test_remove_from_pristine_settings(self):
        assert ghostlight.remove_hooks({"model": "opus"}) == {"model": "opus"}

    def test_merge_migrates_legacy_agentdots_hooks(self):
        # upgrading from the project's old name must not leave dead stanzas
        legacy = {"hooks": {"Stop": [
            {"hooks": [{"type": "command",
                        "command": "/old/repo/agentdots hook stop"}]}]}}
        merged = ghostlight.merge_hooks(legacy, "/new/repo/ghostlight")
        cmds = [h["command"] for st in merged["hooks"]["Stop"]
                for h in st["hooks"]]
        assert cmds == ["/new/repo/ghostlight hook stop"]

    def test_remove_strips_legacy_agentdots_hooks(self):
        legacy = {"hooks": {"Stop": [
            {"hooks": [{"type": "command",
                        "command": "/old/repo/agentdots hook stop"}]}]}}
        assert ghostlight.remove_hooks(legacy) == {}

    def test_hook_command_paths_extracts_quoted(self):
        s = ghostlight.merge_hooks({}, "/My Proj/ghostlight")
        assert ghostlight.hook_command_paths(s) == ["/My Proj/ghostlight"]

    def test_hook_command_paths_ignores_foreign_and_invalid(self):
        s = {"hooks": {"Stop": [
            {"hooks": [{"command": "other-tool run"}]},
            {"hooks": [{"command": "ghostlight hook stop"}]},
        ], "Broken": "not-a-list"}}
        assert ghostlight.hook_command_paths(s) == ["ghostlight"]

    def test_hook_command_paths_survives_non_dict(self):
        assert ghostlight.hook_command_paths([1, 2]) == []

    def test_spaced_path_idempotent_and_removable(self):
        spaced = "/My Proj/ghostlight"
        once = ghostlight.merge_hooks({}, spaced)
        # quoted path is still recognized as ours -> no duplicate stanzas
        assert ghostlight.merge_hooks(once, spaced) == once
        assert once["hooks"]["Stop"][0]["hooks"][0]["command"] == \
            "'/My Proj/ghostlight' hook stop"
        assert ghostlight.remove_hooks(once) == {}


class TestGhosttyVersion:
    @staticmethod
    def _fake_run(monkeypatch, defaults_rc, defaults_out, ghostty_out=""):
        def fake(cmd, **kw):
            class R:
                pass
            if cmd[0] == "defaults":
                R.returncode = defaults_rc
                R.stdout = defaults_out
            elif cmd[0] == "ps":  # ghostty_running probe: not running
                R.returncode = 0
                R.stdout = ""
            else:
                assert cmd[0] == "ghostty"
                R.returncode = 0
                R.stdout = ghostty_out
            return R()
        monkeypatch.setattr(ghostlight.subprocess, "run", fake)

    def test_defaults_read_success(self, monkeypatch):
        self._fake_run(monkeypatch, 0, "1.3.1\n")
        assert ghostlight.ghostty_version() == (1, 3, 1)

    def test_falls_back_to_ghostty_version(self, monkeypatch):
        self._fake_run(monkeypatch, 1, "",
                       ghostty_out="Ghostty 1.3.1\n  - version: 1.3.1\n")
        assert ghostlight.ghostty_version() == (1, 3, 1)

    def test_both_paths_fail(self, monkeypatch):
        self._fake_run(monkeypatch, 1, "", ghostty_out="no match here\n")
        assert ghostlight.ghostty_version() is None

    def test_falls_back_to_home_applications(self, monkeypatch):
        def fake(cmd, **kw):
            class R:
                returncode = 1
                stdout = ""
            if cmd[0] == "defaults" and not cmd[2].startswith("/Applications"):
                R.returncode = 0
                R.stdout = "1.4.2\n"
            return R()
        monkeypatch.setattr(ghostlight.subprocess, "run", fake)
        assert ghostlight.ghostty_version() == (1, 4, 2)

    def test_falls_back_to_applescript_when_running(self, monkeypatch):
        self._fake_run(monkeypatch, 1, "", ghostty_out="no match here\n")
        monkeypatch.setattr(ghostlight, "ghostty_running", lambda: True)
        monkeypatch.setattr(ghostlight, "run_osascript", lambda *a: "1.5.0")
        assert ghostlight.ghostty_version() == (1, 5, 0)

    def test_applescript_fallback_needs_running_ghostty(self, monkeypatch):
        self._fake_run(monkeypatch, 1, "", ghostty_out="no match here\n")
        monkeypatch.setattr(ghostlight, "ghostty_running", lambda: False)

        def boom(*a):
            raise AssertionError("osascript must not run when Ghostty is down")
        monkeypatch.setattr(ghostlight, "run_osascript", boom)
        assert ghostlight.ghostty_version() is None


class TestInstallCommands:
    @staticmethod
    def _isolate(tmp_path, monkeypatch):
        sp = tmp_path / "settings.json"
        monkeypatch.setenv("GHOSTLIGHT_SETTINGS", str(sp))
        monkeypatch.setattr(ghostlight, "ghostty_version", lambda: (1, 3, 1))
        monkeypatch.setattr(ghostlight, "ghostty_running", lambda: False)
        return sp

    def test_install_creates_settings_and_state_dir(
            self, state_env, tmp_path, monkeypatch, capsys):
        sp = self._isolate(tmp_path, monkeypatch)
        assert ghostlight.cmd_install() == 0
        data = json.loads(sp.read_text())
        assert "SessionStart" in data["hooks"]
        assert ghostlight.status_dir().is_dir()

    def test_install_backs_up_existing(self, state_env, tmp_path, monkeypatch):
        sp = self._isolate(tmp_path, monkeypatch)
        sp.write_text('{"model": "opus"}')
        ghostlight.cmd_install()
        backup = tmp_path / "settings.json.ghostlight-backup"
        assert json.loads(backup.read_text()) == {"model": "opus"}
        assert json.loads(sp.read_text())["model"] == "opus"

    def test_reinstall_preserves_pristine_backup(
            self, state_env, tmp_path, monkeypatch):
        sp = self._isolate(tmp_path, monkeypatch)
        sp.write_text('{"model": "opus"}')
        ghostlight.cmd_install()
        # the user accretes settings, then re-installs: the backup must
        # stay the pre-ghostlight copy, not pick up our hooks
        data = json.loads(sp.read_text())
        data["theme"] = "dark"
        sp.write_text(json.dumps(data))
        ghostlight.cmd_install()
        backup = tmp_path / "settings.json.ghostlight-backup"
        assert json.loads(backup.read_text()) == {"model": "opus"}

    def test_install_refuses_corrupt_settings(
            self, state_env, tmp_path, monkeypatch):
        sp = self._isolate(tmp_path, monkeypatch)
        sp.write_text("{broken")
        assert ghostlight.cmd_install() == 1
        assert sp.read_text() == "{broken"

    def test_install_refuses_schema_invalid_settings(
            self, state_env, tmp_path, monkeypatch):
        sp = self._isolate(tmp_path, monkeypatch)
        content = '{"hooks": {"Stop": "not-a-list"}}'
        sp.write_text(content)
        assert ghostlight.cmd_install() == 1
        assert sp.read_text() == content
        assert not (tmp_path / "settings.json.ghostlight-backup").exists()

    def test_uninstall_removes_our_hooks(self, state_env, tmp_path, monkeypatch):
        sp = self._isolate(tmp_path, monkeypatch)
        ghostlight.cmd_install()
        assert ghostlight.cmd_uninstall() == 0
        assert "hooks" not in json.loads(sp.read_text())

    def test_uninstall_purge_removes_state_dir(
            self, state_env, tmp_path, monkeypatch):
        sp = self._isolate(tmp_path, monkeypatch)
        ghostlight.cmd_install()
        ghostlight.status_dir().mkdir(parents=True, exist_ok=True)
        assert ghostlight.cmd_uninstall(purge=True) == 0
        assert not ghostlight.state_dir().exists()

    def test_uninstall_strips_dots_from_tabs(
            self, state_env, tmp_path, monkeypatch):
        self._isolate(tmp_path, monkeypatch)
        ghostlight.cmd_install()
        monkeypatch.setattr(ghostlight, "enumerate_tabs", lambda: [
            {"tab_id": "t1", "terminal_id": "T1", "name": "🟢 demo"},
            {"tab_id": "t2", "terminal_id": "T2", "name": "plain"},
            {"tab_id": "t3", "terminal_id": "T3", "name": "🟠"},
        ])
        titles = []
        monkeypatch.setattr(ghostlight, "set_tab_title",
                            lambda tid, title: titles.append((tid, title))
                            or True)
        assert ghostlight.cmd_uninstall() == 0
        # only the dotted tab with a non-empty base is retitled
        assert titles == [("T1", "demo")]

    def test_uninstall_skips_retitle_when_ghostty_absent(
            self, state_env, tmp_path, monkeypatch):
        self._isolate(tmp_path, monkeypatch)
        ghostlight.cmd_install()
        monkeypatch.setattr(ghostlight, "enumerate_tabs", lambda: None)
        called = []
        monkeypatch.setattr(ghostlight, "set_tab_title",
                            lambda *a: called.append(a) or True)
        assert ghostlight.cmd_uninstall() == 0
        assert called == []


import pytest


class TestCli:
    def test_unknown_command_exits_2(self):
        with pytest.raises(SystemExit) as exc:
            ghostlight.main(["frobnicate"])
        assert exc.value.code == 2

    def test_unknown_hook_event_returns_zero(self):
        assert ghostlight.main(["hook", "bogus-event"]) == 0

    def test_bare_hook_returns_zero(self, state_env):
        assert ghostlight.main(["hook"]) == 0

    def test_hook_help_falls_through_to_argparse(self, capsys):
        with pytest.raises(SystemExit) as exc:
            ghostlight.main(["hook", "-h"])
        assert exc.value.code == 0
        assert "usage: ghostlight hook" in capsys.readouterr().out

    def test_help_hides_internal_commands(self, capsys):
        with pytest.raises(SystemExit) as exc:
            ghostlight.main(["--help"])
        assert exc.value.code == 0
        out = capsys.readouterr().out
        assert "{install,uninstall,status,doctor}" in out
        assert "internal" not in out
        assert "reconcile" not in out

    def test_hook_dispatch(self, monkeypatch):
        seen = []
        monkeypatch.setattr(ghostlight, "cmd_hook",
                            lambda ev: seen.append(ev) or 0)
        assert ghostlight.main(["hook", "stop"]) == 0
        assert seen == ["stop"]

    def test_update_dispatch_swallows_errors(self, state_env, monkeypatch):
        def boom():
            raise RuntimeError("x")
        monkeypatch.setattr(ghostlight, "cmd_update", boom)
        assert ghostlight.main(["update"]) == 0

    def test_status_empty(self, state_env, monkeypatch, capsys):
        monkeypatch.setattr(ghostlight, "enumerate_tabs", lambda: None)
        assert ghostlight.main(["status"]) == 0
        assert "no active sessions" in capsys.readouterr().out

    def test_status_lists_sessions(self, state_env, monkeypatch, capsys):
        ghostlight.write_status(_entry(session="abcdef123", tab="tab-1",
                                      state="waiting"))
        monkeypatch.setattr(ghostlight, "enumerate_tabs", lambda: [
            {"tab_id": "tab-1", "terminal_id": "T1", "name": "🟠 demo"}])
        assert ghostlight.main(["status"]) == 0
        out = capsys.readouterr().out
        assert "🟠" in out and "waiting" in out and "🟠 demo" in out

    def test_doctor_all_good(self, state_env, tmp_path, monkeypatch, capsys):
        sp = tmp_path / "settings.json"
        monkeypatch.setenv("GHOSTLIGHT_SETTINGS", str(sp))
        monkeypatch.setattr(ghostlight, "ghostty_version", lambda: (1, 3, 1))
        monkeypatch.setattr(ghostlight, "ghostty_running", lambda: True)
        monkeypatch.setattr(ghostlight, "run_osascript", lambda *a: "1.3.1")
        ghostlight.cmd_install()
        assert ghostlight.main(["doctor"]) == 0
        assert "✗" not in capsys.readouterr().out

    def test_doctor_flags_missing_hooks(self, state_env, tmp_path,
                                        monkeypatch, capsys):
        sp = tmp_path / "settings.json"
        monkeypatch.setenv("GHOSTLIGHT_SETTINGS", str(sp))
        monkeypatch.setattr(ghostlight, "ghostty_version", lambda: (1, 3, 1))
        monkeypatch.setattr(ghostlight, "ghostty_running", lambda: False)
        assert ghostlight.main(["doctor"]) == 1
        assert "✗" in capsys.readouterr().out

    def test_doctor_flags_partial_notification_install(self, state_env, tmp_path,
                                                       monkeypatch, capsys):
        sp = tmp_path / "settings.json"
        monkeypatch.setenv("GHOSTLIGHT_SETTINGS", str(sp))
        monkeypatch.setattr(ghostlight, "ghostty_version", lambda: (1, 3, 1))
        monkeypatch.setattr(ghostlight, "ghostty_running", lambda: True)
        monkeypatch.setattr(ghostlight, "run_osascript", lambda *a: "1.3.1")
        ghostlight.cmd_install()
        data = json.loads(sp.read_text())
        data["hooks"]["Notification"] = [
            st for st in data["hooks"]["Notification"]
            if st.get("matcher") != "permission_prompt"]
        sp.write_text(json.dumps(data))
        assert ghostlight.main(["doctor"]) == 1
        assert "7/8" in capsys.readouterr().out

    def test_doctor_flags_dead_hook_path(self, state_env, tmp_path,
                                         monkeypatch, capsys):
        sp = tmp_path / "settings.json"
        monkeypatch.setenv("GHOSTLIGHT_SETTINGS", str(sp))
        monkeypatch.setattr(ghostlight, "ghostty_version", lambda: (1, 3, 1))
        monkeypatch.setattr(ghostlight, "ghostty_running", lambda: True)
        monkeypatch.setattr(ghostlight, "run_osascript", lambda *a: "1.3.1")
        sp.write_text(json.dumps(
            ghostlight.merge_hooks({}, "/nonexistent/ghostlight")))
        assert ghostlight.main(["doctor"]) == 1
        out = capsys.readouterr().out
        assert "✗ hook command runnable (/nonexistent/ghostlight)" in out

    def test_doctor_notes_hooks_running_a_different_copy(
            self, state_env, tmp_path, monkeypatch, capsys):
        sp = tmp_path / "settings.json"
        monkeypatch.setenv("GHOSTLIGHT_SETTINGS", str(sp))
        monkeypatch.setattr(ghostlight, "ghostty_version", lambda: (1, 3, 1))
        monkeypatch.setattr(ghostlight, "ghostty_running", lambda: True)
        monkeypatch.setattr(ghostlight, "run_osascript", lambda *a: "1.3.1")
        other = tmp_path / "ghostlight"
        other.write_text("#!/bin/sh\n")
        other.chmod(0o755)
        sp.write_text(json.dumps(ghostlight.merge_hooks({}, str(other))))
        assert ghostlight.main(["doctor"]) == 0
        out = capsys.readouterr().out
        assert "✗" not in out
        assert "not this copy" in out

    @pytest.mark.parametrize("content", [
        '{"hooks": {"Stop": "not-a-list"}}',
        '[1, 2]',  # valid JSON, non-dict top level
    ])
    def test_doctor_survives_schema_invalid_hooks(self, state_env, tmp_path,
                                                   monkeypatch, capsys, content):
        sp = tmp_path / "settings.json"
        monkeypatch.setenv("GHOSTLIGHT_SETTINGS", str(sp))
        monkeypatch.setattr(ghostlight, "ghostty_version", lambda: (1, 3, 1))
        monkeypatch.setattr(ghostlight, "ghostty_running", lambda: False)
        sp.write_text(content)
        assert ghostlight.main(["doctor"]) == 1
        out = capsys.readouterr().out
        assert "unreadable structure" in out
