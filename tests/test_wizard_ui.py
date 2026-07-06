"""Key decoding, rendering, and the scripted end-to-end flow of `tradescrapr new`."""

from __future__ import annotations

import io

from rich.console import Console

from tradescrapr.jobs.targeted import PlanResult
from tradescrapr.planner import load_spec
from tradescrapr.state import Store
from tradescrapr.wizard import Estimate, FormState, Mode, PickerState, estimate, reporter_items
from tradescrapr.wizard_ui import QuotaInfo, cmd_new, decode_key, make_picker, render_wizard


def _reader(*bytes_seq: str):
    buf = list(bytes_seq)

    def read(_timeout: float) -> str:
        return buf.pop(0) if buf else ""

    return read


def test_decode_key():
    assert decode_key(_reader("\x1b", "[", "A")) == "up"
    assert decode_key(_reader("\x1b", "[", "B")) == "down"
    assert decode_key(_reader("\x1b", "[", "C")) == "right"
    assert decode_key(_reader("\x1b", "[", "D")) == "left"
    assert decode_key(_reader("\x1b", "[", "Z")) == "esc"  # unknown sequence
    assert decode_key(_reader("\x1b")) == "esc"  # lone escape
    assert decode_key(_reader("\r")) == "enter"
    assert decode_key(_reader("\n")) == "enter"
    assert decode_key(_reader("\x7f")) == "backspace"
    assert decode_key(_reader(" ")) == "space"
    assert decode_key(_reader("x")) == "x"
    assert decode_key(_reader("", "", "a")) == "a"  # blocks through empty reads


def _render(state, refs, est=None, width=110, height=40, **kw) -> str:
    buf = io.StringIO()
    console = Console(file=buf, width=width, height=height, force_terminal=True)
    quota = QuotaInfo(tier="free", used=3, budget=10, reset_s=3600)
    est = est or Estimate(error="set periods to estimate")
    console.print(render_wizard(state, est, quota, refs, width, height, **kw))
    return buf.getvalue()


def test_render_form_smoke(cfg, refs):
    state = FormState.initial(cfg.paths.root / "jobs")
    state.values.update(reporters="all", periods="2020:2021")
    est = estimate(state.values, refs, combine_flows=False, daily_budget=10)
    out = _render(state, refs, est)
    assert "new job" in out and "quota" in out and "resets in 1h 00m" in out
    assert "reporters" in out and "periods" in out and "use availability" in out
    assert "estimate" in out and "~12 tasks" in out
    assert "3 reporters" in out and "2 periods" in out
    assert "save + plan" in out


def test_render_picker(cfg, refs):
    state = FormState.initial(cfg.paths.root / "jobs")
    state.mode = Mode.PICKER
    state.picker = PickerState(title="reporters", field_key="reporters", items=reporter_items(refs))
    state.picker.handle_key("a")
    state.picker.handle_key("l")
    out = _render(state, refs)
    assert "reporters" in out and "0 selected" in out
    assert "ALB" in out and "DEU" not in out  # search query "al" filters the list
    assert "import" in out


def test_render_import_path(cfg, refs):
    state = FormState.initial(cfg.paths.root / "jobs")
    state.mode = Mode.PICKER
    state.picker = PickerState(title="reporters", field_key="reporters", items=reporter_items(refs))
    state.handle_key("I", lambda _key: state.picker)
    state.edit_buffer = "countries.txt"
    out = _render(state, refs)
    assert "countries.txt" in out and "import" in out and "cancel" in out


def test_render_done_and_too_small(cfg, refs):
    state = FormState.initial(cfg.paths.root / "jobs")
    state.mode = Mode.DONE
    result = PlanResult("tgt-x-abcd1234", 6, "~1 day(s)", ("t1", "t2"), True, 6, False)
    out = _render(state, refs, result=result, has_key=False)
    assert "job planned" in out and "tgt-x-abcd1234" in out
    assert "no API key" in out and "open dashboard" in out

    out = _render(state, refs, width=40, height=10)
    assert "terminal too small" in out


def test_make_picker_preselects(cfg, refs):
    p = make_picker("reporters", {"reporters": ["8"]}, refs)
    assert p.selected == {"8"} and not p.all_selected
    p = make_picker("partners", {"partners": "all"}, refs)
    assert p.all_selected and p.selected == set()
    p = make_picker("flows", {"flows": ["M", "X"]}, refs)
    assert p.selected == {"M", "X"} and not p.allow_all


def _run(cfg, keys: list[str]) -> tuple[int, str]:
    buf = io.StringIO()
    console = Console(file=buf, width=110, height=40, force_terminal=True)
    it = iter(keys)
    rc = cmd_new(cfg, key_source=lambda: next(it), console=console)
    return rc, buf.getvalue()


def test_cmd_new_quit_writes_nothing(cfg, refs):
    rc, _out = _run(cfg, ["q"])
    assert rc == 0
    jobs_dir = cfg.paths.root / "jobs"
    assert not jobs_dir.exists() or not list(jobs_dir.glob("*.yaml"))


def test_cmd_new_full_flow_offline(cfg, refs):
    keys = [
        "enter",
        *"smoke2",
        "enter",  # name
        "down",
        "down",
        "down",
        "down",
        "down",  # -> reporters
        "enter",
        "*",
        "enter",  # pick all reporters
        "down",
        "down",
        "down",
        "down",  # -> periods
        "enter",
        *"2022",
        "enter",
        "down",
        "down",
        "down",  # -> use availability
        "enter",  # toggle off: keeps planning offline
        "s",  # save + plan
        "q",  # leave the result screen
    ]
    rc, out = _run(cfg, keys)
    assert rc == 0

    spec_path = cfg.paths.root / "jobs" / "smoke2.yaml"
    assert spec_path.exists()
    spec = load_spec(spec_path)
    assert spec.name == "smoke2"
    assert spec.reporters == "all"
    assert spec.use_availability is False
    assert spec.periods == ("2022",)

    store = Store(cfg.paths.state_db)
    try:
        jobs = store.job_progress()
        # 3 reporters x 1 period x 2 flows (combine_flows defaults to false)
        assert len(jobs) == 1 and jobs[0].job_id == spec.job_id() and jobs[0].total == 6
    finally:
        store.close()
    assert "job planned" in out


def test_cmd_new_imports_reporters_from_file(cfg, refs):
    import_file = cfg.paths.root / "reporters.txt"
    import_file.write_text("DEU\nALB\n")
    keys = [
        "enter",
        *"imported",
        "enter",  # name
        "down",
        "down",
        "down",
        "down",
        "down",  # -> reporters
        "enter",
        "I",
        *str(import_file),
        "enter",  # import file
        "enter",  # commit picker
        "down",
        "down",
        "down",
        "down",  # -> periods
        "enter",
        *"2022",
        "enter",
        "down",
        "down",
        "down",  # -> use availability
        "enter",
        "s",
        "q",
    ]
    rc, out = _run(cfg, keys)
    assert rc == 0

    spec_path = cfg.paths.root / "jobs" / "imported.yaml"
    spec = load_spec(spec_path)
    assert spec.reporters == ("ALB", "DEU")
    assert spec.periods == ("2022",)
    assert "job planned" in out
