import pytest

from tradescrapr.availability import ReleaseRow
from tradescrapr.errors import ConfigError
from tradescrapr.planner import TaskParams, expand, load_spec, parse_periods

# -- parse_periods -----------------------------------------------------------


def test_annual_range():
    assert parse_periods("2015:2018", "A") == ["2015", "2016", "2017", "2018"]


def test_monthly_range_across_year_boundary():
    assert parse_periods("202411:202502", "M") == ["202411", "202412", "202501", "202502"]


def test_mixed_list_and_string():
    assert parse_periods("2019,2021:2022", "A") == ["2019", "2021", "2022"]
    assert parse_periods(["2019", "2021:2022"], "A") == ["2019", "2021", "2022"]


@pytest.mark.parametrize(
    "bad,freq",
    [
        ("20222", "A"),
        ("202213", "M"),
        ("2022", "M"),
        ("202201", "A"),
        ("2024:2020", "A"),
    ],
)
def test_invalid_periods(bad, freq):
    with pytest.raises(ConfigError):
        parse_periods(bad, freq)


# -- load_spec ------------------------------------------------------------------


def _write_spec(tmp_path, text):
    p = tmp_path / "spec.yaml"
    p.write_text(text)
    return p


BASE_SPEC = """
name: myjob
dataset: final
freq: A
reporters: [DEU, ALB]
flows: [M, X]
commodities: {level: 6}
periods: "2020:2021"
"""


def test_load_spec_roundtrip(tmp_path):
    spec = load_spec(_write_spec(tmp_path, BASE_SPEC))
    assert spec.name == "myjob"
    assert spec.cmd_level == 6
    assert spec.breakdown == "classic"
    assert spec.job_id().startswith("tgt-myjob-")
    # identical content -> identical job id
    assert spec.job_id() == load_spec(_write_spec(tmp_path, BASE_SPEC)).job_id()


def test_load_spec_rejects_bad_values(tmp_path):
    with pytest.raises(ConfigError):
        load_spec(_write_spec(tmp_path, BASE_SPEC.replace("final", "nope")))
    with pytest.raises(ConfigError):
        load_spec(_write_spec(tmp_path, BASE_SPEC + "breakdown: everything\n"))
    with pytest.raises(ConfigError):
        load_spec(_write_spec(tmp_path, BASE_SPEC.replace("name: myjob", "name: 'my job'")))


def test_explicit_codes_win_over_level(tmp_path):
    spec = load_spec(
        _write_spec(tmp_path, BASE_SPEC.replace("{level: 6}", "{level: 6, codes: ['01']}"))
    )
    assert spec.cmd_codes == ("01",)
    assert spec.cmd_level is None


# -- expand -----------------------------------------------------------------------


def test_expand_cartesian(refs, tmp_path):
    spec = load_spec(_write_spec(tmp_path, BASE_SPEC))
    tasks = expand(spec, refs)
    # 2 reporters x 2 years x 2 flows
    assert len(tasks) == 8
    assert {t.reporter for t in tasks} == {"8", "276"}
    assert all(t.partner2 == "0" and t.customs == "C00" and t.mot == "0" for t in tasks)
    # deterministic hashing for dedup
    assert tasks[0].params_hash() == expand(spec, refs)[0].params_hash()


def test_expand_combine_flows(refs, tmp_path):
    spec = load_spec(_write_spec(tmp_path, BASE_SPEC))
    tasks = expand(spec, refs, combine_flows=True)
    assert len(tasks) == 4
    assert tasks[0].flows == ("M", "X")


def test_expand_filters_by_availability(refs, tmp_path):
    spec = load_spec(_write_spec(tmp_path, BASE_SPEC))
    avail = [
        ReleaseRow("final", "C", "A", "HS", "276", "2020", "2026-01-01", 10),
        ReleaseRow("final", "C", "A", "HS", "276", "2019", "2026-01-01", 10),  # outside periods
        ReleaseRow("final", "C", "A", "HS", "8", "2021", "2026-01-01", 10),
    ]
    tasks = expand(spec, refs, availability=avail)
    combos = {(t.reporter, t.period) for t in tasks}
    assert combos == {("276", "2020"), ("8", "2021")}
    assert len(tasks) == 4  # 2 combos x 2 flows


def test_expand_all_reporters_excludes_groups(refs, tmp_path):
    spec = load_spec(_write_spec(tmp_path, BASE_SPEC.replace("[DEU, ALB]", "all")))
    tasks = expand(spec, refs)
    assert "97" not in {t.reporter for t in tasks}  # EU group excluded
    assert {t.reporter for t in tasks} == {"8", "276", "352"}


def test_expand_full_breakdown(refs, tmp_path):
    spec = load_spec(_write_spec(tmp_path, BASE_SPEC + "breakdown: full\n"))
    t = expand(spec, refs)[0]
    assert t.partner2 is None and t.customs is None and t.mot is None
    q = t.api_query()
    assert "partner2Code" not in q and "customsCode" not in q and "motCode" not in q


# -- api_query ---------------------------------------------------------------------


def test_api_query_shapes():
    t = TaskParams(
        dataset="final",
        type_code="C",
        freq="M",
        cl_code="HS",
        reporter="276",
        period="202401",
        flows=("M",),
        cmd_level=6,
        cmd_codes=None,
        partners=None,
    )
    q = t.api_query()
    assert q["cmdCode"] == "AG6"
    assert "partnerCode" not in q  # all partners
    assert q == {
        "reporterCode": "276",
        "period": "202401",
        "flowCode": "M",
        "cmdCode": "AG6",
        "partner2Code": "0",
        "customsCode": "C00",
        "motCode": "0",
    }

    t2 = TaskParams(
        dataset="final",
        type_code="C",
        freq="A",
        cl_code="HS",
        reporter="8",
        period="2022",
        flows=("M", "X"),
        cmd_level=None,
        cmd_codes=("010121", "02"),
        partners=("0", "76"),
    )
    q2 = t2.api_query()
    assert q2["cmdCode"] == "010121,02"
    assert q2["partnerCode"] == "0,76"
    assert q2["flowCode"] == "M,X"

    t3 = TaskParams(
        dataset="final",
        type_code="C",
        freq="A",
        cl_code="HS",
        reporter="8",
        period="2022",
        flows=("M",),
        cmd_level=None,
        cmd_codes=None,
        partners=None,
    )
    assert t3.api_query()["cmdCode"] == "TOTAL"


def test_task_params_roundtrip():
    t = TaskParams(
        dataset="tariffline",
        type_code="C",
        freq="M",
        cl_code="HS",
        reporter="76",
        period="202312",
        flows=("X",),
        cmd_level=None,
        cmd_codes=("02",),
        partners=("4", "8"),
        partner2=None,
        customs=None,
        mot=None,
        include_desc=True,
    )
    assert TaskParams.from_dict(t.to_dict()) == t
