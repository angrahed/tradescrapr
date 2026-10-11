"""Worker process control for the dashboard and `tradescrapr start/stop`.

A background worker is an ordinary `tradescrapr run` in its own session, so it
outlives the dashboard (and the terminal) that started it; on macOS a
`caffeinate -w <pid>` companion keeps idle sleep from pausing it. Stopping is
the same SIGTERM a shell would send: the worker finishes its current task and
exits. Stopping *now* adds a SIGINT — a different signal, so the two cannot
merge into one while the worker is busy inside a long C call — and the worker
treats the second signal as "exit at once, return the task to the queue".
"""

from __future__ import annotations

import fcntl
import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from ..config import Config
from ..errors import TradescraprError
from ..livestatus import STATUS_FILE, read_status

LOCK_FILE = "worker.lock"
SPAWN_LOG = "worker.out"
_STOPPED_STATES = ("T", "t")  # stopped by job control (Ctrl+Z) or a debugger


def probe_worker(lock_path: Path) -> int | None:
    """PID of the process holding the worker lock, or None. Probing briefly
    takes a free lock, which is harmless — a real worker holds it for its whole
    run. -1 means locked by a holder whose pid is unreadable."""
    if not lock_path.exists():
        return None
    try:
        with open(lock_path) as fd:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                txt = fd.read().strip()
                return int(txt) if txt.isdigit() else -1
            fcntl.flock(fd, fcntl.LOCK_UN)
            return None
    except OSError:
        return None


def proc_state(pid: int) -> str:
    """Single-letter process state ('R','S','T',…), or '' if unreadable. A worker
    suspended with Ctrl+Z sits in 'T' yet still holds the lock."""
    stat = Path(f"/proc/{pid}/stat")
    if stat.exists():  # Linux
        try:
            data = stat.read_text()
            return data[data.rindex(")") + 2]  # state char follows the "(comm) " field
        except (OSError, ValueError, IndexError):
            return ""
    try:  # macOS/BSD have no /proc — ask ps
        out = subprocess.run(
            ["ps", "-o", "state=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=2,
        )
        return out.stdout.strip()[:1]
    except (OSError, subprocess.SubprocessError):
        return ""


def worker_pid(cfg: Config) -> int | None:
    """The running worker's pid, None when no worker holds the lock, -1 when
    one does but can't be identified. A lock file without a pid (truncated by
    an older version's refused second worker) is backed up by the status file."""
    pid = probe_worker(cfg.paths.state_dir / LOCK_FILE)
    if pid is None or pid > 0:
        return pid
    doc = read_status(cfg.paths.state_dir / STATUS_FILE)
    if doc and doc.get("state") != "stopped" and isinstance(doc.get("pid"), int):
        return doc["pid"]
    return -1


def is_stopped(pid: int | None) -> bool:
    return bool(pid and pid > 0 and proc_state(pid) in _STOPPED_STATES)


def worker_command(cfg: Config, job_id: str | None = None) -> list[str]:
    cmd = [
        sys.executable,
        "-m",
        "tradescrapr",
        "--root",
        str(cfg.paths.root),
        "--tier",
        cfg.tier,
        "--quiet",
        "run",
    ]
    if job_id:
        cmd += ["--job", job_id]
    return cmd


@dataclass
class Spawned:
    pid: int
    proc: subprocess.Popen
    started: float
    log_path: Path
    keep_awake: bool

    def failure(self) -> str | None:
        """Why the worker exited, from the last line of its captured output;
        None while it runs, after draining the queue (0), or after being
        stopped by a signal (130)."""
        rc = self.proc.poll()
        if rc is None or rc in (0, 130):
            return None
        try:
            lines = self.log_path.read_text(errors="replace").strip().splitlines()
        except OSError:
            lines = []
        tail = next((ln for ln in reversed(lines) if ln.strip()), "")
        return tail.removeprefix("error: ") or f"worker exited with status {rc}"


def start_worker(cfg: Config, *, job_id: str | None = None, keep_awake: bool = True) -> Spawned:
    """Launch `tradescrapr run` detached from this terminal. Raises if a worker
    already holds the lock or no API key is configured."""
    if probe_worker(cfg.paths.state_dir / LOCK_FILE):
        pid = worker_pid(cfg)
        who = f"pid {pid}" if pid and pid > 0 else "pid unknown"
        raise TradescraprError(f"a worker is already running ({who})")
    cfg.require_key()
    cfg.paths.logs.mkdir(parents=True, exist_ok=True)
    log_path = cfg.paths.logs / SPAWN_LOG
    env = dict(os.environ, TRADESCRAPR_ROOT=str(cfg.paths.root), PYTHONUNBUFFERED="1")
    with open(log_path, "wb") as out:  # startup errors and tracebacks only
        proc = subprocess.Popen(
            worker_command(cfg, job_id),
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=subprocess.STDOUT,
            cwd=cfg.paths.root,
            env=env,
            start_new_session=True,  # no SIGHUP/SIGINT from this terminal
            close_fds=True,
        )
    awake = keep_awake and sys.platform == "darwin" and shutil.which("caffeinate") is not None
    if awake:
        subprocess.Popen(
            ["caffeinate", "-i", "-w", str(proc.pid)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    return Spawned(proc.pid, proc, time.time(), log_path, awake)


def stop_worker(cfg: Config, *, now: bool = False, again: bool = False) -> int | None:
    """Ask the worker to stop after its current task; with now=True, at once.
    again=True is the follow-up "stop now" after an earlier graceful request.
    Returns the pid signalled, or None when nothing was running. A suspended
    worker is resumed so it can act on the signals."""
    pid = worker_pid(cfg)
    if not pid or pid < 0:
        return None
    try:
        if is_stopped(pid):
            os.kill(pid, signal.SIGCONT)
        if not again:
            os.kill(pid, signal.SIGTERM)
        if now or again:
            os.kill(pid, signal.SIGINT)
    except ProcessLookupError:
        return None
    except PermissionError as e:
        raise TradescraprError(f"not allowed to signal worker pid {pid}: {e}") from e
    return pid


def continue_worker(cfg: Config) -> int | None:
    """Resume a worker suspended with Ctrl+Z (SIGCONT)."""
    pid = worker_pid(cfg)
    if not pid or pid < 0:
        return None
    try:
        os.kill(pid, signal.SIGCONT)
    except ProcessLookupError:
        return None
    except PermissionError as e:
        raise TradescraprError(f"not allowed to signal worker pid {pid}: {e}") from e
    return pid
