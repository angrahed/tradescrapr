"""The worker's live status document: phases, throttling, and the reader."""

import json

from tradescrapr.livestatus import StatusBoard, StatusReader, read_status
from tradescrapr.planner import TaskParams
from tradescrapr.state import Task


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def _task(**kw) -> Task:
    p = TaskParams(
        dataset="final",
        type_code="C",
        freq="A",
        cl_code="HS",
        reporter="842",
        period="2023",
        flows=("M", "X"),
        cmd_level=6,
        cmd_codes=None,
        partners=None,
    )
    base = dict(
        task_id="t1",
        job_id="j1",
        parent_task_id=None,
        kind="get",
        params=p.to_dict(),
        params_hash="h",
        dataset="final",
        freq="A",
        period="2023",
        reporter="842",
        depth=0,
        status="in_progress",
        attempts=1,
        last_error=None,
    )
    base.update(kw)
    return Task(**base)


def test_phases_and_session_counters(tmp_path):
    clock = Clock()
    path = tmp_path / "worker.json"
    board = StatusBoard(path, clock=clock)
    board.started(mode="run", job_id=None)
    assert read_status(path)["state"] == "working"

    board.task_begin(_task())
    board.call()
    board.phase("request")
    clock.t += 3
    board.transfer("headers", 0, 1000)
    doc = read_status(path)
    assert doc["phase"] == "receive"  # the response began
    assert doc["task"]["reporter"] == "842" and doc["task"]["cmd"] == "AG6"
    assert doc["xfer"]["total"] == 1000 and doc["xfer"]["since"] == clock.t

    board.transfer("done", 1000, 1000)
    assert read_status(path)["phase"] == "parse"
    board.phase("write", rows=5)
    clock.t += 1
    board.task_end("done", rows=5, wire=1000)
    doc = read_status(path)
    assert doc["task"] is None and doc["phase"] == "claim"
    assert doc["session"] == {
        "calls": 1,
        "done": 1,
        "split": 0,
        "failed": 0,
        "dead": 0,
        "rows": 5,
        "wire": 1000,
    }
    assert doc["last"]["outcome"] == "done" and doc["last"]["secs"] == 4

    board.waiting("backoff", 30, "503 server error")
    doc = read_status(path)
    assert doc["state"] == "waiting" and doc["until"] == clock.t + 30

    board.stopped("drained")
    doc = read_status(path)
    assert doc["state"] == "stopped" and doc["reason"] == "drained"


def test_byte_progress_is_throttled_but_kept_in_memory(tmp_path):
    clock = Clock()
    path = tmp_path / "worker.json"
    board = StatusBoard(path, clock=clock, min_interval=1.0)
    board.task_begin(_task())
    board.phase("request")
    board.transfer("headers", 0, None)
    board.transfer("body", 100, None)  # within min_interval: not written
    assert read_status(path)["xfer"]["wire"] == 0
    assert board.snapshot()["xfer"]["wire"] == 100
    clock.t += 1.5
    board.transfer("body", 200, None)
    assert read_status(path)["xfer"]["wire"] == 200


def test_stopping_never_blocks_on_the_lock(tmp_path):
    board = StatusBoard(tmp_path / "worker.json")
    board._lock.acquire()  # as if the signal landed mid-update
    try:
        board.stopping()
    finally:
        board._lock.release()
    assert board.snapshot()["stopping"] is True


def test_listeners_hear_task_ends():
    board = StatusBoard(None)
    heard = []
    board.subscribe(heard.append)
    board.started(mode="run", job_id=None)
    board.task_begin(_task())
    board.task_end("split", note="2 parts")
    board.stopped("drained")
    assert heard == ["started", "task_end", "stopped"]
    assert board.snapshot()["session"]["split"] == 1


def test_reader_reparses_only_on_change(tmp_path):
    path = tmp_path / "worker.json"
    reader = StatusReader(path)
    assert reader.poll() == (None, False)
    board = StatusBoard(path)
    board.started(mode="run", job_id="j")
    doc, changed = reader.poll()
    assert changed and doc["job"] == "j"
    assert reader.poll()[1] is False
    path.write_text(json.dumps({"v": 99}))  # unknown version: ignored
    assert reader.poll()[0] is None
    path.write_text("{torn")
    assert read_status(path) is None
