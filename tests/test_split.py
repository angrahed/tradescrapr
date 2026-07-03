import pytest

from tradescrapr.errors import CannotSplit
from tradescrapr.planner import TaskParams, _batch_cmd_codes, _partner_groups, split

FIXTURE_PARTNERS = ["0", "4", "8", "76", "251"]  # from conftest, incl. World


def _task(**kw) -> TaskParams:
    base = dict(
        dataset="final",
        type_code="C",
        freq="A",
        cl_code="HS",
        reporter="276",
        period="2022",
        flows=("M", "X"),
        cmd_level=6,
        cmd_codes=None,
        partners=None,
    )
    base.update(kw)
    return TaskParams(**base)


def test_ladder_flow_first(refs):
    children = split(_task(), refs)
    assert [c.flows for c in children] == [("M",), ("X",)]
    # everything else inherited
    assert all(c.cmd_level == 6 and c.partners is None for c in children)


def test_ladder_partner_second_default_groups(refs):
    children = split(_task(flows=("M",)), refs)
    assert len(children) == 4  # DEFAULT_PARTNER_GROUPS, no estimate given
    got = sorted(p for c in children for p in c.partners)
    assert got == sorted(FIXTURE_PARTNERS)  # exact partition, World included
    assert all(c.cmd_level == 6 for c in children)  # commodity detail preserved


def test_partner_groups_right_sized_by_estimate(refs):
    # ceil(150k / (0.75 * 100k)) = 2 groups
    children = split(_task(flows=("M",)), refs, est_rows=150_000, cap=100_000)
    assert len(children) == 2
    # huge estimate clamps to one partner per group
    children = split(_task(flows=("M",)), refs, est_rows=5_000_000, cap=100_000)
    assert len(children) == len(FIXTURE_PARTNERS)
    # tiny/wrong estimate still yields at least a 2-way split
    children = split(_task(flows=("M",)), refs, est_rows=10, cap=100_000)
    assert len(children) == 2


def test_partner_bisection_and_floor(refs):
    children = split(_task(flows=("M",), partners=("4", "8", "76")), refs)
    assert [c.partners for c in children] == [("4",), ("8", "76")]


def test_ladder_cmd_after_single_partner(refs):
    children = split(_task(flows=("M",), partners=("4",)), refs)
    # 5 six-digit codes in fixture, chapters 01 (2 codes) + 02 (3 codes) pack into one batch
    assert len(children) == 1
    assert children[0].cmd_codes == ("010121", "010129", "020110", "020120", "020130")
    assert children[0].cmd_level is None
    assert children[0].partners == ("4",)


def test_cmd_bisection_then_floor(refs):
    children = split(
        _task(flows=("M",), cmd_level=None, cmd_codes=("a", "b", "c"), partners=("4",)), refs
    )
    assert [c.cmd_codes for c in children] == [("a",), ("b", "c")]
    with pytest.raises(CannotSplit):
        split(_task(flows=("M",), cmd_level=None, cmd_codes=("010121",), partners=("4",)), refs)


def test_bulk_tasks_cannot_split(refs):
    with pytest.raises(CannotSplit):
        split(_task(kind="bulk"), refs)


def test_partner_groups_interleave():
    codes = tuple(str(i) for i in range(10))
    groups = _partner_groups(codes, 3)
    assert sorted(c for g in groups for c in g) == sorted(codes)  # exact partition
    assert groups[0] == ("0", "3", "6", "9")  # every n-th, spreading heavy codes
    assert _partner_groups(codes, 1) and len(_partner_groups(codes, 1)) == 2  # floor 2
    assert len(_partner_groups(codes, 99)) == 10  # ceiling: one per code


def test_batch_cmd_codes_packs_chapters():
    codes = [f"01{i:04d}" for i in range(30)] + [f"02{i:04d}" for i in range(30)]
    batches = _batch_cmd_codes(codes, max_per_call=100)
    assert len(batches) == 1 and len(batches[0]) == 60
    batches = _batch_cmd_codes(codes, max_per_call=40)
    assert [len(b) for b in batches] == [30, 30]  # chapters kept whole


def test_batch_cmd_codes_slices_oversized_chapter():
    codes = [f"84{i:04d}" for i in range(250)]
    batches = _batch_cmd_codes(codes, max_per_call=100)
    assert [len(b) for b in batches] == [100, 100, 50]
    assert sorted(c for b in batches for c in b) == sorted(codes)
