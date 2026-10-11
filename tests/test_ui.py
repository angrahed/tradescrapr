"""Dashboard: widgets, compositor, log parsing, metrics, snapshot, screens, input."""

import datetime as dt
import fcntl
import io
import json
import logging
import os
import signal
import time

import pytest
from rich.console import Console
from rich.segment import Segment
from rich.style import Style
from rich.text import Text

import tradescrapr.ui.app as app_mod
import tradescrapr.ui.snapshot as snapshot_mod
from tradescrapr.livestatus import StatusBoard
from tradescrapr.planner import TaskParams
from tradescrapr.state import Task
from tradescrapr.ui import cmd_ui, control
from tradescrapr.ui.app import App
from tradescrapr.ui.canvas import overlay, panel, text_line
from tradescrapr.ui.context import Ctx, UIState, reconcile
from tradescrapr.ui.events import LogTail, parse_line, parse_message
from tradescrapr.ui.fmt import Names, clock, human_bytes, human_count, job_name, span
from tradescrapr.ui.inline import InlineProgress
from tradescrapr.ui.metrics import Finish, build_pace, quota_bound, rates, series, work_left
from tradescrapr.ui.pages import period_axis, screen
from tradescrapr.ui.snapshot import Collector
from tradescrapr.ui.term import Terminal, changed_span, decode
from tradescrapr.ui.theme import CLASSIC, PHOSPHOR, THEMES, gradient, load_theme
from tradescrapr.ui.widgets import bar, columns, scanner, sparkline, stacked

# ---------------------------------------------------------------------------
# formatting and widgets
# ---------------------------------------------------------------------------


def test_humanizers():
    assert human_bytes(512) == "512 B"
    assert human_bytes(2 * 1024**3) == "2.0 GB"
    assert human_count(950) == "950"
    assert human_count(24_703) == "24.7k"
    assert human_count(250_000) == "250k"
    assert human_count(28_262_000) == "28.3M"
    assert clock(7) == "0:07" and clock(4324) == "1:12:04"
    assert span(45) == "45s" and span(125) == "2m 05s" and span(6720) == "1h 52m"
    assert span(3 * 86400 + 4 * 3600) == "3d 4h"


def test_job_names():
    assert job_name("tgt-military_core-a9382cee") == "military_core"
    assert job_name("tgt-x-aaaa1111", "pretty") == "pretty"
    assert job_name("mon-20260716T101500") == "monitor 2026-07-16 10:15"


def test_names_read_cached_refs_offline(cfg, refs):
    names = Names(cfg.paths.refs)
    assert names.iso("276") == "DEU"
    assert names.iso("999") == "999"  # unknown codes pass through
    assert Names(cfg.paths.root / "nowhere").iso("276") == "276"


def test_bars_have_exact_width():
    for frac in (0, 0.013, 0.5, 0.999, 1, 1.7):
        assert bar(CLASSIC, 37, frac).cell_len == 37
    assert scanner(CLASSIC, 20, 3.3).cell_len == 20
    assert sparkline([]) == "" and sparkline([1, 4, 8])[-1] == "█"


def test_stacked_bar_keeps_rare_problems_visible():
    t = stacked(CLASSIC, 40, [(9998, CLASSIC.ok), (2, CLASSIC.bad)], 10000)
    assert t.cell_len == 40
    assert any(CLASSIC.bad in str(span.style) for span in t.spans)


def test_column_graph_scales_to_peak():
    lines = columns([0, 1, 2, 4], 4, 2, CLASSIC.bar)
    assert len(lines) == 2 and all(ln.cell_len == 4 for ln in lines)
    assert lines[0].plain[-1] == "█" and lines[1].plain[0] == " "


def test_gradient_endpoints():
    g = gradient(("#000000", "#ffffff"), 3)
    assert g == ("#000000", "#808080", "#ffffff")


# ---------------------------------------------------------------------------
# compositor and terminal
# ---------------------------------------------------------------------------


def test_text_line_keeps_single_style_text_coloured():
    # Rich's Text.render drops the base style when a Text has no spans
    segs = text_line(Text("abc", Style(color="red")), 5)
    assert segs[0].style.color.name == "red"
    assert sum(len(s.text) for s in segs) == 5


def test_panel_and_overlay_geometry():
    p = panel(CLASSIC, 30, 6, [Text("hello"), "x" * 80], title="T", right="r", footer="f")
    assert p.height == 6
    assert all(sum(len(s.text) for s in ln) == 30 for ln in p.lines)
    plain = p.plain().splitlines()
    assert plain[0].startswith("╭─ T ") and plain[0].endswith(" r ─╮")
    assert "…" in plain[2]  # long body lines get an ellipsis
    top = panel(CLASSIC, 10, 3, ["hi"])
    merged = overlay(p, top, 5, 1)
    assert merged.plain().splitlines()[1][5:15] == top.plain().splitlines()[0]


def test_decode_keys():
    assert decode("\x1b[A\x1b[1;5B\x1b[5~\x1bOP")[0] == ["up", "down", "pgup", "f1"]
    assert decode("q\r ") == (["q", "enter", "space"], "")
    assert decode("ab\x1b") == (["a", "b"], "\x1b")  # lone ESC waits for more
    assert decode("\x1b[") == ([], "\x1b[")


def test_text_line_neutralises_control_characters():
    segs = text_line(Text("busy\r\nretry\tlater\x1b[2J", Style(color="red")), 30)
    plain = "".join(s.text for s in segs)
    assert len(plain) == 30 and not any(ch in plain for ch in "\r\n\t\x1b")
    assert plain.startswith("busy retry later")  # Rich drops the CR itself


def test_changed_span_finds_minimal_range():
    a = [Segment("abcdef")]
    assert changed_span(a, [Segment("abcdef")]) is None
    assert changed_span(a, [Segment("abXdef")]) == (2, 3)
    assert changed_span(a, [Segment("abcdef", Style(bold=True))]) == (0, 6)
    assert changed_span(a, [Segment("abc")]) == (3, None)


def test_terminal_draw_sends_only_changes():
    r, w = os.pipe()
    with os.fdopen(w, "w") as out, os.fdopen(os.dup(r)) as fake_in:
        term = Terminal(stdin=fake_in, stdout=out)
        first = [[Segment("hello world")], [Segment("second line")]]
        assert term.draw(first, (11, 2), None) > 0
        os.read(r, 4096)
        second = [[Segment("hello World")], [Segment("second line")]]
        term.draw(second, (11, 2), None)
        sent = os.read(r, 4096).decode()
        assert "\x1b[1;7HW" in sent and "second" not in sent
        assert term.draw(second, (11, 2), None) == 0  # nothing changed, nothing sent
        term.draw([[Segment("hi")], [Segment("second line")]], (11, 2), None)
        sent = os.read(r, 4096).decode()
        assert "\x1b[1;1Hhi" in sent and "\x1b[K" in sent  # shorter line: whole repaint
    os.close(r)


# ---------------------------------------------------------------------------
# log events
# ---------------------------------------------------------------------------


def test_parse_done_lines_old_and_new():
    old = (
        "2026-07-16 17:09:00,531 INFO tradescrapr.worker task=7bcb6b2b job=tgt-2025-ed009f2f "
        "final C/A/HS reporter=842 period=2025 flows=X cmd=AG6 partners=155 partners "
        "status=done records=133422 dur=101.8s"
    )
    ev = parse_line(old)
    assert (ev.kind, ev.reporter, ev.period, ev.flows, ev.rows) == (
        "done",
        "842",
        "2025",
        ("X",),
        133422,
    )
    assert ev.partners == "155 partners" and ev.secs == 101.8 and ev.when == "17:09:00"
    new = old.replace("dur=101.8s", "dur=101.8s ttfb=90.2s bytes=2878058")
    assert parse_line(new).wire == 2878058


def test_parse_other_events():
    assert parse_message(
        "task=ab truncated at cap=250000 -> split into 2 children (depth 1)"
    ).kind == ("split")
    retry = parse_message("task=ab transient failure (-> failed): 503 server error")
    assert retry.kind == "retry" and "503" in retry.text
    assert parse_message("task=ab transient failure (-> dead): boom").kind == "dead"
    assert parse_message("task=ab DEAD (permanent): 400").kind == "dead"
    assert parse_message("transport retry 2/5 after HTTP 503").kind == "backoff"
    assert parse_message("worker start pid=12 job=* kind=*").text.startswith("worker started")
    stop = parse_message("worker stop reason=normal calls=3 done=2 split=1 failed=0 quota_used=3")
    assert stop.kind == "stop" and stop.extra["reason"] == "normal"
    assert parse_message("expand: 12 reporter-periods exist") is None  # noise
    assert parse_message("something odd", level="WARNING").kind == "warn"


def test_log_tail_is_incremental_and_survives_rotation(tmp_path):
    path = tmp_path / "t.log"
    line = "2026-07-03 10:00:{:02d},000 INFO w task=t{} job=j x status=done records=1 dur=1.0s\n"
    path.write_text("2026-07-03 10:00:00,000 DEBUG urllib3 noise\n" + line.format(1, 1))
    tail = LogTail(path)
    assert tail.poll() and [e.task_id for e in tail.events] == ["t1"]
    assert not tail.poll()
    with open(path, "a") as f:
        f.write(line.format(2, 2) + line.format(3, 3)[:20])  # last line half-written
    tail.poll()
    assert [e.task_id for e in tail.events] == ["t1", "t2"]
    with open(path, "a") as f:
        f.write(line.format(3, 3)[20:])
    tail.poll()
    assert tail.events[-1].task_id == "t3"
    path.unlink()  # rotation: a new, shorter file appears
    path.write_text(line.format(9, 9))
    tail.poll()
    assert tail.events[-1].task_id == "t9"


# ---------------------------------------------------------------------------
# pace, ETA, throughput
# ---------------------------------------------------------------------------


def test_pace_prefers_the_reporter_then_the_job():
    rows = [
        {"job_id": "j", "reporter": "842", "n": 2, "secs": 90.0},
        {"job_id": "j", "reporter": "8", "n": 6, "secs": 10.0},
    ]
    pace = build_pace(rows, [])
    assert pace.expect("j", "842") == (90.0, "reporter")
    assert pace.expect("j", "999") == (30.0, "job")  # (2*90 + 6*10) / 8
    assert pace.expect("other", "8") == (None, "")
    left = work_left(pace, [{"job_id": "j", "reporter": "842", "n": 3}])
    assert left == {"j": 270.0}
    assert work_left(pace, [{"job_id": "new", "reporter": "1", "n": 1}]) == {"new": None}


def test_quota_bound_eta():
    # 1000 tasks of 10s each, 50 calls left today, 450/day: quota is the limit
    eta, bound = quota_bound(10_000, 1000, budget=450, used_today=400, reset_s=3600)
    assert bound and eta > 2 * 86400
    eta, bound = quota_bound(100, 10, budget=450, used_today=0, reset_s=3600)
    assert (eta, bound) == (100, False)
    assert quota_bound(100, 10, budget=None, used_today=0, reset_s=0) == (100, False)


def test_rates_and_series():
    now = 10_000.0
    recent = [Finish("j", now - 600, now - 540, 6000), Finish("j", now - 120, now - 60, 600)]
    tasks_h, rows_h = rates(recent, now)
    assert tasks_h == pytest.approx(60.0)  # 2 tasks in 120 busy seconds
    assert rows_h == pytest.approx(6600 / 120 * 3600)
    vals = series(recent, now, 900, 15)  # one bucket per minute
    assert len(vals) == 15 and sum(vals) == pytest.approx(6600)  # rows/min over 1-min buckets
    assert vals[-1] == 0.0


def test_period_axis_labels():
    assert period_axis(["2022", "2023", "2024"], 3) == "22 23 24 "
    axis = period_axis([str(y) for y in range(2000, 2026)], 1)
    assert axis.startswith("00   05   10")
    months = [f"{y}{m:02d}" for y in (2024, 2025) for m in range(1, 13)][:18]
    assert period_axis(months, 1).split() == ["2024", "2025"]
    assert period_axis(months[:13], 1).split() == ["2024"]  # no room for a whole "2025"


# ---------------------------------------------------------------------------
# snapshot
# ---------------------------------------------------------------------------


def _params(rep="8", period="2022", flow="M"):
    return TaskParams(
        dataset="final",
        type_code="C",
        freq="A",
        cl_code="HS",
        reporter=rep,
        period=period,
        flows=(flow,),
        cmd_level=6,
        cmd_codes=None,
        partners=None,
    )


def _seed(cfg, store):
    store.create_job("tgt-x-aaaa1111", "targeted", json.dumps({"name": "x", "periods": "2022"}))
    store.add_tasks(
        "tgt-x-aaaa1111",
        [_params(rep, "2022", f) for rep in ("8", "276") for f in ("M", "X")],
    )
    t = store.claim_next_task()
    out = cfg.paths.data / "part.parquet"
    out.write_bytes(b"x" * 1234)
    store.finish_task(t.task_id, records=1234, paths=[str(out.relative_to(cfg.paths.root))])
    dead = store.claim_next_task()
    store.fail_task(dead.task_id, "400 client error: bad", permanent=True, max_attempts=3)
    store.claim_next_task()  # one in flight
    store.quota_try_reserve("2026-07-01", 10)
    store.upsert_release("final", "M", "8", "202505", "2026-07-01", "mon-1")
    (cfg.paths.logs / "tradescrapr.log").write_text(
        "2026-07-03 10:00:00,000 INFO w task=abc job=j final C/A/HS reporter=8 period=2022 "
        "flows=M cmd=AG6 partners=all status=done records=5 dur=1.0s\n"
    )


def test_gather_snapshot(cfg, store):
    _seed(cfg, store)
    col = Collector(cfg)
    col.focus = "tgt-x-aaaa1111"
    snap = col.prime()
    job = snap.job("tgt-x-aaaa1111")
    assert job.name == "x" and job.n("done") == 1 and job.n("dead") == 1
    assert job.remaining == 2 and job.state == "running"
    assert job.size == 1234 and snap.data_bytes == 1234
    assert len(snap.flights) == 1 and len(snap.upcoming) == 1
    assert snap.problems[0].status == "dead" and "bad" in snap.problems[0].error
    assert snap.releases == {"pending": 1}
    assert snap.events and snap.events[-1].reporter == "8"
    d = snap.detail
    assert d.reporters == ["8", "276"] and d.periods == ["2022"]
    assert sorted(d.tasks_by_reporter.values()) == [(0, 2), (1, 2)]


def test_collector_thread_publishes(cfg, store):
    _seed(cfg, store)
    col = Collector(cfg, interval=0.05)
    col.start()
    try:
        deadline = time.time() + 5
        while col.latest is None and time.time() < deadline:
            time.sleep(0.02)
        first = col.latest.version
        col.poke()
        while col.latest.version == first and time.time() < deadline:
            time.sleep(0.02)
        assert col.latest.version > first
    finally:
        col.stop()


# ---------------------------------------------------------------------------
# worker control
# ---------------------------------------------------------------------------


def _hold_lock(cfg, pid=4242):
    held = open(cfg.paths.state_dir / "worker.lock", "w")
    fcntl.flock(held, fcntl.LOCK_EX)
    held.write(f"{pid}\n")
    held.flush()
    return held


def test_probe_worker(cfg):
    lock = cfg.paths.state_dir / "worker.lock"
    assert control.probe_worker(lock) is None
    lock.write_text("123\n")
    assert control.probe_worker(lock) is None  # file but no holder
    held = _hold_lock(cfg)
    assert control.probe_worker(lock) == 4242
    held.close()
    assert control.probe_worker(lock) is None


def test_proc_state_this_process_is_running():
    assert control.proc_state(os.getpid()) not in ("", "T", "t")


def test_worker_command(cfg):
    cmd = control.worker_command(cfg, "tgt-x")
    assert cmd[1:3] == ["-m", "tradescrapr"] and "--quiet" in cmd
    assert cmd[-3:] == ["run", "--job", "tgt-x"]


def test_start_refuses_without_key_or_with_a_running_worker(cfg):
    from tradescrapr.errors import ConfigError, TradescraprError

    with pytest.raises(ConfigError):
        control.start_worker(cfg)
    held = _hold_lock(cfg)
    try:
        with pytest.raises(TradescraprError, match="already running"):
            control.start_worker(cfg)
    finally:
        held.close()
    assert control.stop_worker(cfg) is None  # nothing to stop


def test_spawn_failure_reads_the_captured_output(tmp_path):
    import subprocess
    import sys

    log = tmp_path / "worker.out"
    log.write_text("Traceback...\nerror: no API key found\n")

    def spawned(code):
        proc = subprocess.Popen([sys.executable, "-c", f"raise SystemExit({code})"])
        proc.wait()
        return control.Spawned(proc.pid, proc, time.time(), log, False)

    assert spawned(1).failure() == "no API key found"
    assert spawned(0).failure() is None
    assert spawned(130).failure() is None  # stopped by a signal is not a failure


# ---------------------------------------------------------------------------
# screens
# ---------------------------------------------------------------------------


def _live_doc(task_id, pid):
    now = time.time()
    return {
        "v": 1,
        "pid": pid,
        "mode": "run",
        "job": None,
        "started": now - 100,
        "updated": now,
        "state": "working",
        "phase": "receive",
        "phase_since": now - 5,
        "until": None,
        "note": "",
        "stopping": False,
        "reason": None,
        "task": {
            "task_id": task_id,
            "job_id": "tgt-x-aaaa1111",
            "kind": "get",
            "reporter": "276",
            "period": "2022",
            "flows": ["M"],
            "cmd": "AG6",
            "partners": None,
            "attempt": 1,
            "claimed": now - 12,
            "rows": None,
        },
        "xfer": {"wire": 300_000, "total": 1_000_000, "since": now - 5, "at": now},
        "session": {"calls": 3, "done": 2, "split": 0, "failed": 0, "dead": 1, "rows": 9000},
        "last": None,
    }


def _ctx(cfg, snap, doc=None, ui=None, theme=CLASSIC):
    ui = ui or UIState()
    now = time.time()
    return Ctx(
        theme=theme,
        names=Names(cfg.paths.refs),
        snap=snap,
        worker=reconcile(snap, doc, ui, now),
        ui=ui,
        now=now,
        t=1.0,
    )


def _plain(block):
    return block.plain()


@pytest.mark.parametrize("size", [(150, 46), (96, 30), (60, 16)])
@pytest.mark.parametrize("view", ["overview", "job", "log"])
def test_every_screen_fills_the_terminal_exactly(cfg, store, refs, size, view):
    _seed(cfg, store)
    col = Collector(cfg)
    col.focus = "tgt-x-aaaa1111"
    snap = col.prime()
    ui = UIState(view=view, selected="tgt-x-aaaa1111")
    for theme in THEMES.values():
        block = screen(_ctx(cfg, snap, ui=ui, theme=theme), *size)
        assert block.height == size[1]
        assert all(sum(len(s.text) for s in ln) == size[0] for ln in block.lines)


def test_overview_idle(cfg, store, refs):
    _seed(cfg, store)
    out = _plain(screen(_ctx(cfg, Collector(cfg).prime()), 150, 46))
    assert "worker idle" in out and "press" in out and "to start downloading" in out
    assert "JOBS (1)" in out and "x" in out
    assert "1 dead task(s)" in out
    assert "ALB 2022 X 400 client error" in out  # problems name the reporter
    assert "UP NEXT" in out


def test_overview_with_a_live_download(cfg, store, refs):
    _seed(cfg, store)
    held = _hold_lock(cfg, pid=os.getpid())
    try:
        snap = Collector(cfg).prime()
        flight = snap.flights[0]
        doc = _live_doc(flight.task_id, os.getpid())
        out = _plain(screen(_ctx(cfg, snap, doc), 150, 46))
    finally:
        held.close()
    assert "worker running" in out and "RECEIVING" in out
    assert "30% 293.0 KB of 976.6 KB" in out
    assert "server 7.0s" in out  # first byte 7s after the claim


def test_reconcile_states(cfg, store, monkeypatch):
    snap = snapshot_mod.Snapshot(version=1, taken=time.time(), tier="free", budget=10)
    now = time.time()
    assert reconcile(snap, None, UIState(), now).state == "idle"
    snap.worker_pid = os.getpid()
    assert reconcile(snap, None, UIState(), now).state == "running"
    assert reconcile(snap, None, UIState(stop_pid=os.getpid()), now).state == "stopping"
    doc = _live_doc("t", os.getpid())
    doc.update(state="waiting", phase="backoff", until=now + 30)
    w = reconcile(snap, doc, UIState(), now)
    assert w.state == "waiting" and w.live is doc
    snap.worker_stalled = True
    assert reconcile(snap, None, UIState(), now).state == "stalled"
    snap.worker_pid, snap.worker_stalled = None, False
    doc["state"] = "stopped"
    w = reconcile(snap, doc, UIState(), now)
    assert w.state == "idle" and w.last is doc  # the previous run's summary


def test_help_and_confirm_dialogs(cfg, store):
    _seed(cfg, store)
    snap = Collector(cfg).prime()
    out = _plain(screen(_ctx(cfg, snap, ui=UIState(help=True)), 120, 40))
    assert "HELP" in out and "retry dead tasks" in out
    app = App(cfg, theme=CLASSIC)
    app.ui.selected = "tgt-x-aaaa1111"
    app.frame(120, 40)
    app.handle("C")
    out = _plain(app.frame(120, 40))
    assert "CANCEL JOB x" in out and "unfinished task(s)" in out


def test_too_small(cfg, store):
    out = _plain(screen(_ctx(cfg, Collector(cfg).prime()), 40, 10))
    assert "terminal too small" in out


# ---------------------------------------------------------------------------
# app input and actions
# ---------------------------------------------------------------------------


def test_navigation_and_views(cfg, store):
    _seed(cfg, store)
    store.create_job("tgt-y-bbbb2222", "targeted", "{}")
    app = App(cfg, theme=CLASSIC)
    app.frame(120, 40)
    first = app.ui.selected
    app.handle("down")
    assert app.ui.selected != first
    app.handle("up")
    assert app.ui.selected == first
    app.handle("enter")
    assert app.ui.view == "job" and app.collector.focus == first
    app.handle("esc")
    assert app.ui.view == "overview" and app.collector.focus is None
    app.handle("l")
    assert app.ui.view == "log"
    app.handle("f")
    assert app.ui.log_filter == "done"
    app.handle("]")
    assert app.ui.window == 2
    app.handle("q")
    assert app.running is False


def test_theme_choice_is_remembered(cfg, store):
    app = App(cfg, theme=CLASSIC)
    app.handle("t")
    assert app.theme is PHOSPHOR
    assert load_theme(cfg.paths.state_dir) is PHOSPHOR
    assert load_theme(cfg.paths.state_dir, "amber").name == "amber"


def test_retry_and_cancel_go_through_confirmation(cfg, store):
    _seed(cfg, store)
    app = App(cfg, theme=CLASSIC)
    app.ui.selected = "tgt-x-aaaa1111"
    app.frame(120, 40)
    app.handle("R")
    assert app.ui.confirm is not None
    app.handle("n")
    assert app.ui.confirm is None and store.job_progress()[0].counts.get("dead") == 1
    app.handle("R")
    app.handle("y")
    assert store.job_progress()[0].counts.get("dead") is None
    app.handle("C")
    app.handle("y")
    counts = store.job_progress()[0].counts
    assert counts.get("cancelled") and not counts.get("pending")


def test_start_reports_problems_and_success(cfg, store, monkeypatch):
    _seed(cfg, store)
    app = App(cfg, theme=CLASSIC)
    app.frame(120, 40)
    app.handle("s")  # the fixture config has no API key
    assert "no API key" in app.ui.toast.text and app.ui.spawn_error

    class FakeProc:
        def poll(self):
            return None

    started = []

    def fake_start(cfg, *, job_id=None):
        started.append(job_id)
        return control.Spawned(999999, FakeProc(), time.time(), cfg.paths.logs / "w.out", False)

    monkeypatch.setattr(app_mod.control, "start_worker", fake_start)
    app.handle("S")
    assert started == [app.ui.selected]
    assert "worker started" in app.ui.toast.text and app.ui.spawn_error is None
    app.frame(120, 40)
    assert app.worker.state == "starting"


def test_stop_signals_then_escalates(cfg, store, monkeypatch):
    held = _hold_lock(cfg, pid=os.getpid())
    sent = []
    monkeypatch.setattr(control.os, "kill", lambda pid, sig: sent.append(sig))
    monkeypatch.setattr(control, "is_stopped", lambda pid: False)
    try:
        app = App(cfg, theme=CLASSIC)
        app.collector.prime()
        app.frame(120, 40)
        app.handle("x")
        assert app.ui.stop_pid == os.getpid() and sent == [signal.SIGTERM]
        app.frame(120, 40)
        assert app.worker.state == "stopping"
        app.handle("x")  # again: stop now, with a signal that can't merge with the first
        assert sent == [signal.SIGTERM, signal.SIGINT] and "now" in app.ui.toast.text
    finally:
        held.close()


def test_stop_now_and_a_suspended_worker(cfg, monkeypatch):
    held = _hold_lock(cfg, pid=os.getpid())
    sent = []
    monkeypatch.setattr(control.os, "kill", lambda pid, sig: sent.append(sig))
    monkeypatch.setattr(control, "is_stopped", lambda pid: True)
    try:
        assert control.stop_worker(cfg, now=True) == os.getpid()
    finally:
        held.close()
    assert sent == [signal.SIGCONT, signal.SIGTERM, signal.SIGINT]


def test_refused_worker_keeps_the_running_pid(cfg):
    from tradescrapr.errors import TradescraprError
    from tradescrapr.worker import worker_lock

    lock = cfg.paths.state_dir / "worker.lock"
    with worker_lock(lock):
        mine = control.probe_worker(lock)
        with pytest.raises(TradescraprError):
            with worker_lock(lock):
                pass
        assert control.probe_worker(lock) == mine == os.getpid()


def test_pid_falls_back_to_the_status_file(cfg):
    held = open(cfg.paths.state_dir / "worker.lock", "w")  # locked, but no pid inside
    fcntl.flock(held, fcntl.LOCK_EX)
    try:
        assert control.worker_pid(cfg) == -1
        StatusBoard(cfg.paths.state_dir / "worker.json").started(mode="run", job_id=None)
        assert control.worker_pid(cfg) == os.getpid()
    finally:
        held.close()


def test_cmd_ui_once(cfg, store, capsys):
    _seed(cfg, store)
    assert cmd_ui(cfg, once=True) == 0
    out = capsys.readouterr().out
    assert "tradescrapr" in out and "JOBS" in out and "ACTIVITY" in out


# ---------------------------------------------------------------------------
# inline display of a foreground run
# ---------------------------------------------------------------------------


def _task(t):
    return Task(
        task_id=t.task_id,
        job_id=t.job_id,
        parent_task_id=None,
        kind="get",
        params=t.params,
        params_hash=t.params_hash,
        dataset="final",
        freq="A",
        period="2022",
        reporter="276",
        depth=0,
        status="in_progress",
        attempts=1,
        last_error=None,
    )


def test_inline_progress_renders_every_state(cfg, store, refs):
    _seed(cfg, store)
    board = StatusBoard(None)
    console = Console(file=io.StringIO(), force_terminal=True, width=110)
    root = logging.getLogger()
    stream = logging.StreamHandler(io.StringIO())
    root.addHandler(stream)
    level = root.level
    root.setLevel(logging.INFO)
    try:
        with InlineProgress(cfg, store, board, console=console) as ip:
            assert stream not in root.handlers  # swapped for the live handler
            board.started(mode="run", job_id=None)
            task = store.claim_next_task() or store.claim_next_task()
            board.task_begin(_task(task))
            board.phase("request")
            text = _render(ip, console)
            assert "waiting on server" in text and "DEU 2022" in text
            board.transfer("headers", 0, 1000)
            board.transfer("body", 400, 1000)
            assert "40%" in _render(ip, console)
            board.task_end("done", rows=10)
            board.waiting("backoff", 30, "503 server error")
            assert "BACKING OFF" in _render(ip, console)
            logging.getLogger("tradescrapr.worker").info("backing off 30s")
        assert stream in root.handlers  # restored
    finally:
        root.removeHandler(stream)
        root.setLevel(level)
    assert "backing off 30s" in console.file.getvalue()
    assert "queue" in _render(ip, console)


def _render(ip, console):
    buf = io.StringIO()
    Console(file=buf, width=110).print(ip._render())
    return buf.getvalue()


def test_inline_queue_numbers(cfg, store, refs):
    _seed(cfg, store)
    ip = InlineProgress(cfg, store, StatusBoard(None), console=Console(file=io.StringIO()))
    ip._refresh_queue("init")
    q = ip._queue
    assert q["total"] == 4 and q["remaining"] == 2 and q["dead"] == 1


def test_finished_at_index_exists(store):
    rows = store.db.execute("PRAGMA index_list(tasks)").fetchall()
    assert "ix_tasks_finished" in {r["name"] for r in rows}


def test_events_age_today():
    from tradescrapr.ui.events import Event, event_age

    now = dt.datetime(2026, 7, 3, 10, 0, 30)
    assert event_age(Event(when="10:00:00", day="2026-07-03", kind="info"), now) == 30


def test_quiet_mode_still_reports_errors(cfg, capsys):
    from tradescrapr.cli import main

    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    held = _hold_lock(cfg)
    try:
        rc = main(["--root", str(cfg.paths.root), "--quiet", "start", "--no-ui"])
    finally:
        held.close()
        for h in root.handlers:
            if h not in handlers:
                root.removeHandler(h)
                h.close()
        root.setLevel(level)
    assert rc == 1
    assert "error: a worker is already running" in capsys.readouterr().err
