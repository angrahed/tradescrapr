import fcntl
import io

from rich.console import Console

from tradescrapr.planner import TaskParams
from tradescrapr.ui import (
    auto_height,
    cmd_ui,
    gather,
    human_bytes,
    human_count,
    probe_worker,
    render,
    sparkline,
    tail_log,
)


def test_sparkline():
    assert sparkline([]) == ""
    assert sparkline([0, 0]) == "▁▁"
    line = sparkline([1, 4, 8])
    assert len(line) == 3 and line[-1] == "█" and line[0] == "▁"


def test_humanizers():
    assert human_bytes(512) == "512 B"
    assert human_bytes(2 * 1024**3) == "2.0 GB"
    assert human_count(950) == "950"
    assert human_count(28_262_000) == "28.3M"


def test_tail_log_filters_and_keeps_tail(tmp_path):
    log = tmp_path / "t.log"
    lines = [f"2026-07-03 10:00:{i:02d},000 INFO x task=t{i} status=done" for i in range(30)]
    noise = ["2026-07-03 10:00:00,000 DEBUG urllib3 connection reset"] * 10
    log.write_text("\n".join(noise + lines) + "\n")
    got = tail_log(log, keep=5)
    assert len(got) == 5
    assert got[-1].endswith("task=t29 status=done")
    assert all("task=" in ln for ln in got)


def test_probe_worker(tmp_path):
    lock = tmp_path / "worker.lock"
    assert probe_worker(lock) is None  # no file
    lock.write_text("123\n")
    assert probe_worker(lock) is None  # file exists but unlocked
    with open(lock, "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        held.write("4242\n")
        held.flush()
        assert probe_worker(lock) == 4242
    assert probe_worker(lock) is None  # released


def _seed(cfg, store):
    store.create_job("tgt-x-aaaa1111", "targeted", "{}")
    store.add_tasks(
        "tgt-x-aaaa1111",
        [
            TaskParams(
                dataset="final",
                type_code="C",
                freq="A",
                cl_code="HS",
                reporter="8",
                period="2022",
                flows=(f,),
                cmd_level=6,
                cmd_codes=None,
                partners=None,
            )
            for f in ("M", "X")
        ],
    )
    t = store.claim_next_task()
    store.finish_task(t.task_id, records=1234, paths=[])
    store.quota_try_reserve("2026-07-01", 10)
    store.upsert_release("final", "M", "8", "202505", "2026-07-01", "mon-1")
    (cfg.paths.logs / "tradescrapr.log").write_text(
        "2026-07-03 10:00:00,000 INFO w task=abc status=done records=5 dur=1.0s\n"
    )


def test_gather_and_render_smoke(cfg, store):
    _seed(cfg, store)
    snap = gather(cfg, store, (3, 4096))
    assert snap.quota_budget == 10
    assert snap.jobs and snap.jobs[0].job_id == "tgt-x-aaaa1111"
    assert snap.releases.get("pending") == 1
    assert snap.data_files == 3

    buf = io.StringIO()
    console = Console(file=buf, width=110, height=40, force_terminal=True)
    console.print(render(snap, height=auto_height(snap)))
    out = buf.getvalue()
    assert "tgt-x-aaaa1111" in out
    assert "quota" in out
    assert "task=abc" in out  # activity feed


def test_cmd_ui_once(cfg, store, capsys):
    _seed(cfg, store)  # cmd_ui opens its own Store; WAL allows both
    assert cmd_ui(cfg, once=True) == 0
    out = capsys.readouterr().out
    assert "tradescrapr" in out and "jobs" in out
