"""Form state machine, YAML round trip, and estimate math for `tradescrapr new`."""

from __future__ import annotations

import pytest

from tradescrapr.errors import ConfigError
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
    parse_import_file,
    partner_items,
    reporter_items,
    resolve_imported_selection,
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
    assert p.handle_key("i") is None
    assert p.query == "i"
    p.handle_key("backspace")
    assert p.handle_key("I") == "import"
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
# picker file import
# ---------------------------------------------------------------------------


def test_parse_import_file_txt_csv_json(tmp_path):
    txt = tmp_path / "codes.txt"
    txt.write_text("DEU\nALB, 352;010121\n")
    assert parse_import_file(txt) == ["DEU", "ALB", "352", "010121"]

    csv = tmp_path / "countries.csv"
    csv.write_text("code,name\nDEU,Germany\nALB,Albania\n")
    assert parse_import_file(csv) == ["DEU", "ALB"]

    json_array = tmp_path / "products.json"
    json_array.write_text('["010121", {"cmdCode": "020110"}]')
    assert parse_import_file(json_array) == ["010121", "020110"]

    json_obj = tmp_path / "wrapped.json"
    json_obj.write_text('{"product_codes": [{"product_code": "010129"}]}')
    assert parse_import_file(json_obj) == ["010129"]


def test_parse_import_file_rejects_bad_files(tmp_path):
    unsupported = tmp_path / "codes.yaml"
    unsupported.write_text("DEU")
    with pytest.raises(ConfigError, match="unsupported"):
        parse_import_file(unsupported)

    malformed = tmp_path / "codes.json"
    malformed.write_text("{")
    with pytest.raises(ConfigError, match="invalid JSON"):
        parse_import_file(malformed)

    empty = tmp_path / "codes.txt"
    empty.write_text(" \n, ;")
    with pytest.raises(ConfigError, match="no identifiers"):
        parse_import_file(empty)


def test_resolve_imported_reporters_partners_and_commodities(refs):
    reps = PickerState(title="reporters", field_key="reporters", items=reporter_items(refs))
    assert resolve_imported_selection(reps, refs, ["DEU", "8"]) == {"276", "8"}

    parts = PickerState(title="partners", field_key="partners", items=partner_items(refs))
    assert resolve_imported_selection(parts, refs, ["W00", "BRA"]) == {"0", "76"}

    hs = PickerState(title="HS codes", field_key="commodities", items=commodity_items(refs, "HS"))
    assert resolve_imported_selection(hs, refs, ["010121", "02"]) == {"010121", "02"}


def test_resolve_imported_selection_rejects_unknowns_without_mutating(refs):
    p = PickerState(
        title="reporters",
        field_key="reporters",
        items=reporter_items(refs),
        selected={"8"},
    )
    with pytest.raises(ConfigError, match="unknown reporters"):
        resolve_imported_selection(p, refs, ["DEU", "ZZZ"])
    assert p.selected == {"8"}


def test_import_path_mode_and_import_replace_selection(tmp_path, refs):
    p = PickerState(title="reporters", field_key="reporters", items=reporter_items(refs))
    p.selected = {"8"}
    s = _picker_state(tmp_path, refs, "reporters", p)
    s.handle_key("I", _mk_disabled)
    assert s.mode is Mode.IMPORT_PATH
    assert s.handle_key("esc", _mk_disabled) is Action.NONE
    assert s.mode is Mode.PICKER and p.selected == {"8"}

    import_file = tmp_path / "reporters.txt"
    import_file.write_text("DEU\nISL\n")
    s.handle_key("I", _mk_disabled)
    for ch in str(import_file):
        s.handle_key("space" if ch == " " else ch, _mk_disabled)
    assert s.handle_key("enter", _mk_disabled) is Action.IMPORT
    s.import_picker_file(import_file, refs)
    assert s.mode is Mode.PICKER
    assert p.selected == {"276", "352"}
    assert not p.all_selected


def test_imported_commodities_force_codes_mode(tmp_path, refs):
    p = PickerState(title="HS codes", field_key="commodities", items=commodity_items(refs, "HS"))
    s = _state(tmp_path)
    s.picker = p
    s.mode = Mode.PICKER
    import_file = tmp_path / "products.csv"
    import_file.write_text("code\n010121\n020110\n")
    s.import_picker_file(import_file, refs)
    s.handle_key("enter", _mk_disabled)
    assert s.values["commodities"] == {"mode": "codes", "codes": ["010121", "020110"]}


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
