"""Polars dtype pins for Comtrade API columns.

Every *Code column (and period) is pinned to Utf8 — HS commodity codes carry
leading zeros and corrupt silently as integers. Values are Float64 so chunk
schemas stay identical even when a reporter never populates a field (all-null
columns otherwise infer as Null and break cross-chunk scans).
"""

from __future__ import annotations

import polars as pl

SCHEMA_OVERRIDES: dict[str, pl.DataType] = {
    # identity / codes -> Utf8
    "typeCode": pl.Utf8,
    "freqCode": pl.Utf8,
    "period": pl.Utf8,
    "reporterCode": pl.Utf8,
    "reporterISO": pl.Utf8,
    "reporterDesc": pl.Utf8,
    "flowCode": pl.Utf8,
    "flowDesc": pl.Utf8,
    "partnerCode": pl.Utf8,
    "partnerISO": pl.Utf8,
    "partnerDesc": pl.Utf8,
    "partner2Code": pl.Utf8,
    "partner2ISO": pl.Utf8,
    "partner2Desc": pl.Utf8,
    "classificationCode": pl.Utf8,
    "classificationSearchCode": pl.Utf8,
    "cmdCode": pl.Utf8,
    "cmdDesc": pl.Utf8,
    "customsCode": pl.Utf8,
    "customsDesc": pl.Utf8,
    "mosCode": pl.Utf8,
    "motCode": pl.Utf8,
    "motDesc": pl.Utf8,
    "qtyUnitCode": pl.Utf8,
    "qtyUnitAbbr": pl.Utf8,
    "altQtyUnitCode": pl.Utf8,
    "altQtyUnitAbbr": pl.Utf8,
    # integers
    "refPeriodId": pl.Int64,
    "refYear": pl.Int64,
    "refMonth": pl.Int64,
    "aggrLevel": pl.Int64,
    "legacyEstimationFlag": pl.Int64,
    # measures -> Float64
    "qty": pl.Float64,
    "altQty": pl.Float64,
    "netWgt": pl.Float64,
    "grossWgt": pl.Float64,
    "cifvalue": pl.Float64,
    "fobvalue": pl.Float64,
    "primaryValue": pl.Float64,
    # flags
    "isOriginalClassification": pl.Boolean,
    "isQtyEstimated": pl.Boolean,
    "isAltQtyEstimated": pl.Boolean,
    "isNetWgtEstimated": pl.Boolean,
    "isGrossWgtEstimated": pl.Boolean,
    "isReported": pl.Boolean,
    "isAggregate": pl.Boolean,
    "isLeaf": pl.Boolean,
}


def enforce(df: pl.DataFrame) -> pl.DataFrame:
    """Cast known columns to pinned dtypes; unknown new API columns pass through."""
    casts = {c: t for c, t in SCHEMA_OVERRIDES.items() if c in df.columns}
    return df.cast(casts, strict=False)


def enforce_bulk(df: pl.DataFrame) -> pl.DataFrame:
    """Same pins for bulk TSV files read as all-Utf8: numeric casts tolerate empty
    strings, and boolean flags arrive as 0/1/true/false text."""
    exprs = []
    for c in df.columns:
        target = SCHEMA_OVERRIDES.get(c)
        if target is None:
            continue
        if target == pl.Boolean:
            low = pl.col(c).str.to_lowercase()
            exprs.append(
                pl.when(low.is_in(["1", "true", "t"]))
                .then(True)
                .when(low.is_in(["0", "false", "f"]))
                .then(False)
                .otherwise(None)
                .alias(c)
            )
        else:
            exprs.append(pl.col(c).cast(target, strict=False))
    return df.with_columns(exprs)
