import gzip
import json

import polars as pl

from tradescrapr import storage

META = {
    "dataset": "final",
    "source_url": "https://example/get",
    "params": {},
    "job_id": "j1",
}


def _records(n=3):
    return [
        {
            "period": "2022",
            "reporterCode": 8,
            "cmdCode": "010121",
            "primaryValue": 1.5 * i,
            "isAggregate": False,
            "flowCode": "M",
        }
        for i in range(n)
    ]


def test_write_chunk_atomic_with_manifest(tmp_path):
    part = tmp_path / "final" / "freq=A" / "period=2022" / "reporter=8"
    wr = storage.write_chunk(_records(), part_dir=part, task_id="abc123", manifest_meta=META)
    assert wr.rows == 3
    assert wr.path.name == "part-abc123.parquet"
    assert not list(part.glob("*.tmp-*"))  # no tmp leftovers

    m = json.loads((part / "part-abc123.parquet.manifest.json").read_text())
    assert m["rows"] == 3 and m["sha256"] == wr.sha256 and m["task_id"] == "abc123"
    assert "subscription" not in json.dumps(m).lower()

    df = pl.read_parquet(wr.path)
    assert df["cmdCode"].dtype == pl.Utf8
    assert df["cmdCode"][0] == "010121"  # leading zero survives
    assert df["reporterCode"].dtype == pl.Utf8
    assert df["primaryValue"].dtype == pl.Float64


def test_write_chunk_overwrites_same_task(tmp_path):
    part = tmp_path / "p"
    storage.write_chunk(_records(5), part_dir=part, task_id="abc", manifest_meta=META)
    wr = storage.write_chunk(_records(2), part_dir=part, task_id="abc", manifest_meta=META)
    assert pl.read_parquet(wr.path).height == 2
    assert len(list(part.glob("part-abc*.parquet"))) == 1


def test_remove_task_outputs_and_clean_partition(tmp_path):
    part = tmp_path / "p"
    storage.write_chunk(_records(), part_dir=part, task_id="aaa", manifest_meta=META)
    storage.write_chunk(_records(), part_dir=part, task_id="bbb", manifest_meta=META)
    assert storage.remove_task_outputs(part, "aaa") == 2  # parquet + manifest
    assert len(list(part.glob("part-*"))) == 2  # bbb's pair remains
    assert storage.clean_partition(part) == 2
    assert not list(part.glob("part-*"))


def test_convert_bulk_file(tmp_path):
    tsv = "period\treporterCode\tcmdCode\tprimaryValue\tisReported\n" + "".join(
        f"2022\t8\t0101{i:02d}\t{i}.5\t{'1' if i % 2 else '0'}\n" for i in range(10)
    )
    gz = tmp_path / "file.gz"
    with gzip.open(gz, "wt") as f:
        f.write(tsv)

    part = tmp_path / "p"
    results = storage.convert_bulk_file(
        gz, part_dir=part, task_id="bulk1", manifest_meta=META, batch_rows=4
    )
    assert sum(r.rows for r in results) == 10
    assert len(results) >= 2  # batched
    df = pl.concat([pl.read_parquet(r.path) for r in results])
    assert df["cmdCode"].dtype == pl.Utf8 and df["cmdCode"][0] == "010100"
    assert df["primaryValue"].dtype == pl.Float64
    assert df["isReported"].dtype == pl.Boolean
    assert df["isReported"].sum() == 5
    assert not list(tmp_path.glob("*.tsv"))  # intermediate cleaned up
