"""Form state machine, YAML round trip, and estimate math for `tradescrapr new`."""

from __future__ import annotations

import pytest

from tradescrapr.planner import load_spec
from tradescrapr.wizard import (
    Action,
    Estimate,
    FormState,
    Mode,
    PickerState,
    commodity_items,
    default_values,
    dump_spec_yaml,
    estimate,
    flow_items,
    partner_items,
    reporter_items,
)


def _mk_disabled(_key: str) -> PickerState | None:
    raise AssertionError("no picker expected")


def _state(tmp_path) -> FormState:
    return FormState.initial(tmp_path / "jobs")


def _goto(state: FormState, key: str) -> None:
    while state.current_field().key != key:
        state.handle_key("down", _mk_disabled)


def _type(state: FormState, text: str) -> None:
    for ch in text:
        state.handle_key(" " if ch == " " else ch, _mk_disabled)


# ---------------------------------------------------------------------------
# navigation + inline edits
# ---------------------------------------------------------------------------


def test_navigation_wraps(tmp_path):
    s = _state(tmp_path)
    assert s.current_field().key == "name"
    s.handle_key("up", _mk_disabled)
    assert s.current_field().key == "use_availability"
    s.handle_key("down", _mk_disabled)
    assert s.current_field().key == "name"


def test_cycle_and_toggle(tmp_path):
    s = _state(tmp_path)
    _goto(s, "dataset")
    s.handle_key("enter", _mk_disabled)
    assert s.values["dataset"] == "tariffline"
    s.handle_key("right", _mk_disabled)
    assert s.values["dataset"] == "final"
    s.handle_key("left", _mk_disabled)
    assert s.values["dataset"] == "tariffline"

    _goto(s, "include_desc")
    s.handle_key("enter", _mk_disabled)
    assert s.values["include_desc"] is True
    s.handle_key("space", _mk_disabled)
    assert s.values["include_desc"] is False


def test_commodities_mode_cycles_and_quit(tmp_path):
    s = _state(tmp_path)
    _goto(s, "commodities")
    assert s.values["commodities"]["mode"] == "level6"
    s.handle_key("right", _mk_disabled)
    assert s.values["commodities"]["mode"] == "codes"
    s.handle_key("right", _mk_disabled)
    assert s.values["commodities"]["mode"] == "total"
    s.handle_key("left", _mk_disabled)
    assert s.values["commodities"]["mode"] == "codes"
    assert s.handle_key("q", _mk_disabled) is Action.QUIT


# ---------------------------------------------------------------------------
# text editing + validation
# ---------------------------------------------------------------------------


def test_text_edit_commit_and_revert(tmp_path):
    s = _state(tmp_path)
    s.handle_key("enter", _mk_disabled)  # edit name
    assert s.mode is Mode.EDIT_TEXT
    _type(s, "nordic")
    s.handle_key("backspace", _mk_disabled)
    s.handle_key("enter", _mk_disabled)
    assert s.mode is Mode.FORM and s.values["name"] == "nordi"

    s.handle_key("enter", _mk_disabled)
    _type(s, "xxx")
    s.handle_key("esc", _mk_disabled)  # revert
    assert s.mode is Mode.FORM and s.values["name"] == "nordi"


def test_name_slug_rejected(tmp_path):
    s = _state(tmp_path)
    s.handle_key("enter", _mk_disabled)
    _type(s, "bad name")
    s.handle_key("enter", _mk_disabled)
    assert s.mode is Mode.EDIT_TEXT  # stays in edit so the user can fix it
    assert "slug" in s.error
    assert s.values["name"] == ""


@pytest.mark.parametrize(
    ("periods", "freq", "fragment"),
    [
        ("2024:2020", "A", "start > end"),
        ("2022", "M", "YYYYMM"),
        ("20221:20223", "M", "YYYYMM"),
    ],
)
def test_periods_validation(tmp_path, periods, freq, fragment):
    s = _state(tmp_path)
    s.values["freq"] = freq
    _goto(s, "periods")
    s.handle_key("enter", _mk_disabled)
    _type(s, periods)
    s.handle_key("enter", _mk_disabled)
    assert s.mode is Mode.EDIT_TEXT
    assert fragment in s.error


def test_freq_flip_revalidates_periods(tmp_path):
    s = _state(tmp_path)
    s.values["periods"] = "2022"
    _goto(s, "freq")
    s.handle_key("enter", _mk_disabled)  # A -> M
    assert s.values["freq"] == "M"
    assert "YYYYMM" in s.error


def test_save_jumps_to_first_invalid_field(tmp_path):
    s = _state(tmp_path)
    assert s.handle_key("s", _mk_disabled) is Action.NONE
    assert s.current_field().key == "name" and "required" in s.error

    s.values["name"] = "ok"
    s.values["reporters"] = ["8"]
    assert s.handle_key("s", _mk_disabled) is Action.NONE
    assert s.current_field().key == "periods"

    s.values["periods"] = "2022"
    assert s.handle_key("s", _mk_disabled) is Action.SAVE


def test_save_confirms_overwrite(tmp_path):
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    (jobs / "ok.yaml").write_text("existing")
    s = FormState.initial(jobs)
    s.values.update(name="ok", reporters=["8"], periods="2022")
    assert s.handle_key("s", _mk_disabled) is Action.NONE
    assert s.mode is Mode.CONFIRM_OVERWRITE
    assert s.handle_key("n", _mk_disabled) is Action.NONE
    assert s.mode is Mode.FORM
    s.handle_key("s", _mk_disabled)
    assert s.handle_key("y", _mk_disabled) is Action.SAVE


# ---------------------------------------------------------------------------
# picker
# ---------------------------------------------------------------------------


def test_picker_items_tolerate_missing_text(refs):
    reps = reporter_items(refs)
    assert [i.label for i in reps] == ["ALB", "DEU", "ISL"]  # groups excluded
    parts = partner_items(refs)
    assert parts[0].code == "0"  # World aggregate kept
    hs = commodity_items(refs, "HS")
    assert all(i.code != "TOTAL" for i in hs) and len(hs) == 8


def test_picker_search_and_toggle(refs):
    p = PickerState(title="reporters", field_key="reporters", items=reporter_items(refs))
    p.handle_key("a")
    p.handle_key("l")
    p.handle_key("b")
    assert [i.code for i in p.visible()] == ["8"]
    p.handle_key("space")
    assert p.selected == {"8"}
    p.handle_key("backspace")
    p.handle_key("backspace")
    p.handle_key("backspace")
    assert len(p.visible()) == 3
    p.handle_key("down")
    p.handle_key("space")
    assert p.selected == {"8", "276"}
    p.handle_key("*")
    assert p.all_selected
    p.handle_key("space")  # any manual toggle clears "all"
    assert not p.all_selected


def _picker_state(tmp_path, refs, field_key: str, picker: PickerState) -> FormState:
    s = _state(tmp_path)
    _goto(s, field_key)
    s.handle_key("enter", lambda _key: picker)
    assert s.mode is Mode.PICKER
    return s


def test_picker_commit_reporters_all_and_empty(tmp_path, refs):
    p = PickerState(title="reporters", field_key="reporters", items=reporter_items(refs))
    s = _picker_state(tmp_path, refs, "reporters", p)
    s.handle_key("enter", _mk_disabled)  # empty commit refused
    assert s.mode is Mode.PICKER and "at least one" in s.error
    s.handle_key("*", _mk_disabled)
    s.handle_key("enter", _mk_disabled)
    assert s.mode is Mode.FORM and s.values["reporters"] == "all"


def test_picker_commit_partners_empty_folds_to_all(tmp_path, refs):
    p = PickerState(title="partners", field_key="partners", items=partner_items(refs))
    s = _picker_state(tmp_path, refs, "partners", p)
    s.handle_key("enter", _mk_disabled)
    assert s.mode is Mode.FORM and s.values["partners"] == "all"


def test_picker_cancel_leaves_values(tmp_path, refs):
    p = PickerState(title="flows", field_key="flows", items=flow_items(), allow_all=False)
    s = _picker_state(tmp_path, refs, "flows", p)
    s.handle_key("space", _mk_disabled)
    s.handle_key("esc", _mk_disabled)
    assert s.mode is Mode.FORM and s.values["flows"] == ["M", "X"]


def test_picker_flows_keep_canonical_order(tmp_path, refs):
    p = PickerState(title="flows", field_key="flows", items=flow_items(), allow_all=False)
    p.selected = {"X", "M"}
    s = _picker_state(tmp_path, refs, "flows", p)
    s.handle_key("enter", _mk_disabled)
    assert s.values["flows"] == ["M", "X"]


def test_make_picker_failure_sets_error(tmp_path):
    s = _state(tmp_path)
    _goto(s, "reporters")
    s.handle_key("enter", lambda _key: None)
    assert s.mode is Mode.FORM and "refs refresh" in s.error


# ---------------------------------------------------------------------------
# YAML round trip
# ---------------------------------------------------------------------------


def _values(**over):
    v = default_values()
    v.update(
        name="roundtrip",
        reporters=["276", "8"],
        partners=["76"],
        periods="2015:2024",
    )
    v.update(over)
    return v


def test_yaml_round_trip(tmp_path, refs):
    v = _values()
    path = tmp_path / "jobs" / "roundtrip.yaml"
    path.parent.mkdir()
    path.write_text(dump_spec_yaml(v, refs))

    spec = load_spec(path)
    assert spec.name == "roundtrip"
    assert spec.dataset == "final" and spec.type_code == "C" and spec.freq == "A"
    assert spec.classification == "HS"
    assert spec.reporters == ("ALB", "DEU")  # numeric codes emitted as ISO3, sorted
    assert spec.partners == ("BRA",)
    assert spec.flows == ("M", "X")
    assert spec.cmd_level == 6 and spec.cmd_codes is None
    assert spec.periods == ("2015:2024",)
    assert spec.breakdown == "classic"
    assert spec.include_desc is False and spec.use_availability is True


def test_yaml_total_mode_omits_commodities(tmp_path, refs):
    v = _values(commodities={"mode": "total", "codes": []})
    text = dump_spec_yaml(v, refs)
    assert "commodities" not in text
    path = tmp_path / "t.yaml"
    path.write_text(text)
    spec = load_spec(path)
    assert spec.cmd_level is None and spec.cmd_codes is None


def test_yaml_explicit_codes(tmp_path, refs):
    v = _values(commodities={"mode": "codes", "codes": ["010121", "020110"]})
    path = tmp_path / "c.yaml"
    path.write_text(dump_spec_yaml(v, refs))
    spec = load_spec(path)
    assert spec.cmd_codes == ("010121", "020110") and spec.cmd_level is None


def test_yaml_deterministic_job_id(tmp_path, refs):
    a = dump_spec_yaml(_values(), refs)
    b = dump_spec_yaml(_values(reporters=["8", "276"]), refs)  # order-insensitive
    assert a == b
    p1, p2 = tmp_path / "a.yaml", tmp_path / "b.yaml"
    p1.write_text(a)
    p2.write_text(b)
    assert load_spec(p1).job_id() == load_spec(p2).job_id()


# ---------------------------------------------------------------------------
# estimate
# ---------------------------------------------------------------------------


def test_estimate_math(refs):
    v = _values(reporters="all", periods="2020:2021")
    est = estimate(v, refs, combine_flows=False, daily_budget=10)
    assert est == Estimate(tasks=12, days=1, n_reporters=3, n_periods=2, n_flow_groups=2)
    est = estimate(v, refs, combine_flows=True, daily_budget=10)
    assert est.tasks == 6 and est.n_flow_groups == 1
    est = estimate(v, refs, combine_flows=False, daily_budget=None)
    assert est.tasks == 12 and est.days is None


def test_estimate_errors(refs):
    est = estimate(_values(periods=""), refs, combine_flows=False, daily_budget=10)
    assert "periods" in est.error
    est = estimate(_values(periods="20x0"), refs, combine_flows=False, daily_budget=10)
    assert "YYYY" in est.error
    est = estimate(_values(reporters=[]), refs, combine_flows=False, daily_budget=10)
    assert "reporters" in est.error
