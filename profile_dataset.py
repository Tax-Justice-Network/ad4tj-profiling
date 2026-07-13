#!/usr/bin/env python3
"""
AD4TJ disclosure-safe dataset profiler  (Python reference implementation)
=========================================================================

Run this INSIDE the secure data-lab environment, on the REAL confidential data.
It writes a *codebook log file* describing the dataset so that researchers can
write correct, runnable analysis code WITHOUT ever seeing the data.

The log NEVER releases an individual data point. It reports only aggregates,
and applies conservative statistical-disclosure-control (SDC) rules:

  * counts below MIN_CELL_COUNT are suppressed,
  * exact minimum/maximum values are never released,
  * numeric summaries are rounded to a few significant figures,
  * string/identifier values are not printed.

The lab remains the final authority: review the log and your local SDC policy
before releasing it.

----------------------------------------------------------------------------
HOW TO USE
----------------------------------------------------------------------------
1. Edit the CONFIG block below: set DATA_PATH to your data file.
2. Run:   python profile_dataset.py
3. A "<dataset>.codebook.log" file is written next to your data (or to
   OUTPUT_PATH). Review it, then release it to the researcher.

Supported input formats: .csv, .tsv, .xlsx/.xls, .dta (Stata), .parquet
"""

from __future__ import annotations

# ===========================================================================
# CONFIG  --  edit this block, nothing else is required
# ===========================================================================
DATA_PATH = "examples/income_tax/returns.csv"   # <-- path to your data file
OUTPUT_PATH = ""           # output log path; "" = derive from DATA_PATH
DATASET_LABEL = ""         # optional human title; "" = use the file name

# Disclosure-control settings (defaults are conservative; adjust to lab policy)
MIN_CELL_COUNT = 10        # suppress any count strictly below this
ROUND_SIGNIFICANT = 3      # round numeric summaries to this many significant figures
MAX_CATEGORIES = 20        # list category values only if distinct count <= this
ID_LIKE_UNIQUE_RATIO = 0.9 # distinct/non-missing above this => treat as identifier

# Column-name hints that mark a variable as a direct identifier (its values and
# its distribution are NEVER released), even when the values repeat across a
# panel -- e.g. a taxpayer ID that appears in every period. Case-insensitive.
ID_NAME_HINTS = ("tpin", "taxpayer", "nrc", "ssn", "passport",
                 "reference", "refno", "reg_no", "regno", "national_id")
# Advisory only (does not reclassify): warn when an integer column's values are
# all this large and never zero/negative -- a tell-tale of an ID, not a measure.
ID_LARGE_INT_THRESHOLD = 1e9
# ===========================================================================


import math
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Any

try:
    import numpy as np
    import pandas as pd
except ImportError:  # pragma: no cover - environment guard
    sys.stderr.write(
        "This tool needs pandas and numpy. Install with:\n"
        "    pip install pandas numpy openpyxl pyreadstat\n"
    )
    raise


SUPPRESSED = "<suppressed (below min cell count)>"


@dataclass
class Config:
    min_cell_count: int = MIN_CELL_COUNT
    round_significant: int = ROUND_SIGNIFICANT
    max_categories: int = MAX_CATEGORIES
    id_like_unique_ratio: float = ID_LIKE_UNIQUE_RATIO
    id_name_hints: tuple[str, ...] = ID_NAME_HINTS
    id_large_int_threshold: float = ID_LARGE_INT_THRESHOLD


# Exact name-tokens and end-of-token forms that denote a direct identifier.
_ID_NAME_TOKENS = {"tin", "tpin", "tpn", "id", "uid", "guid", "nrc", "ssn", "pin", "brn", "uin"}
_ID_FALSE_FRIENDS = {"valid", "invalid", "solid", "rapid", "humid", "rigid", "fluid",
                     "hybrid", "candid", "stupid", "liquid", "placid", "florid", "putrid"}


def looks_like_identifier_name(name: str, cfg: Config) -> bool:
    """True if a column name looks like a direct identifier (taxpayer ID, TIN,
    reference number, ...). Catches panel IDs that repeat across periods and so
    are missed by the near-unique test."""
    n = name.lower()
    tokens = set(re.split(r"[^a-z0-9]+", n))
    if tokens & _ID_NAME_TOKENS:
        return True
    if any(len(t) >= 5 and t.endswith("id") and t not in _ID_FALSE_FRIENDS for t in tokens):
        return True
    return any(h in n for h in cfg.id_name_hints)


@dataclass
class ColumnReport:
    name: str
    kind: str                      # numeric | categorical | datetime | boolean | identifier | empty
    storage_dtype: str
    n_obs: int                     # non-missing count
    n_missing: int
    pct_missing: float
    facts: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


@dataclass
class DatasetReport:
    label: str
    source: str
    n_rows: int
    n_cols: int
    config: Config
    columns: list[ColumnReport] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Safe numeric helpers
# ---------------------------------------------------------------------------
def round_sig(x: float | None, sig: int) -> float | None:
    """Round to `sig` significant figures. Used so no released number equals an
    exact record value."""
    if x is None:
        return None
    if isinstance(x, float) and (math.isnan(x) or math.isinf(x)):
        return None
    if x == 0:
        return 0.0
    return round(x, -int(math.floor(math.log10(abs(x)))) + (sig - 1))


def safe_count(c: int, cfg: Config) -> int | str:
    """Suppress small non-zero counts. Zero is always safe to report."""
    if c == 0:
        return 0
    if c < cfg.min_cell_count:
        return SUPPRESSED
    return int(c)


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------
def read_data(path: str) -> pd.DataFrame:
    ext = os.path.splitext(path)[1].lower()
    if ext == ".csv":
        return pd.read_csv(path)
    if ext == ".tsv":
        return pd.read_csv(path, sep="\t")
    if ext in (".xlsx", ".xls"):
        return pd.read_excel(path)
    if ext == ".dta":
        return pd.read_stata(path)
    if ext == ".parquet":
        return pd.read_parquet(path)
    raise ValueError(
        f"Unsupported file type {ext!r}. Supported: .csv .tsv .xlsx .xls .dta .parquet"
    )


# ---------------------------------------------------------------------------
# Per-column profiling
# ---------------------------------------------------------------------------
def _classify(series: pd.Series, n_obs: int, cfg: Config) -> str:
    if n_obs == 0:
        return "empty"
    # A column whose NAME marks it as a direct identifier is withheld regardless
    # of cardinality (so panel IDs like a taxpayer TPIN never get profiled as a
    # measure). Dates are exempt so a date is still summarised as a date.
    if looks_like_identifier_name(str(series.name), cfg) and not (
        pd.api.types.is_datetime64_any_dtype(series)
    ):
        return "identifier"
    if pd.api.types.is_bool_dtype(series):
        return "boolean"
    if pd.api.types.is_datetime64_any_dtype(series):
        return "datetime"
    if pd.api.types.is_numeric_dtype(series):
        nonnull = series.dropna()
        n_distinct = int(nonnull.nunique())
        arr = nonnull.to_numpy(dtype="float64")
        is_int_like = bool(np.all(np.equal(np.mod(arr, 1), 0)))
        # An integer column that is (near-)unique per row is an identifier, not a
        # measure: its quantiles are meaningless and could leak an ID range.
        if is_int_like and (
            n_distinct == n_obs or n_distinct / n_obs > cfg.id_like_unique_ratio
        ):
            return "identifier"
        return "numeric"
    # object / category / string
    n_distinct = series.nunique(dropna=True)
    if n_distinct <= cfg.max_categories:
        return "categorical"                       # few enough to list safely
    if n_distinct >= cfg.id_like_unique_ratio * n_obs:
        return "identifier"                        # (near-)unique: IDs, names, free text
    return "high_cardinality"                      # a real category, but too many to list


def profile_column(series: pd.Series, cfg: Config) -> ColumnReport:
    n_total = len(series)
    nonnull = series.dropna()
    n_obs = int(len(nonnull))
    n_missing = int(n_total - n_obs)
    pct_missing = round(100.0 * n_missing / n_total, 1) if n_total else 0.0

    kind = _classify(series, n_obs, cfg)
    rep = ColumnReport(
        name=str(series.name),
        kind=kind,
        storage_dtype=str(series.dtype),
        n_obs=n_obs,
        n_missing=n_missing,
        pct_missing=pct_missing,
    )

    if kind == "empty":
        rep.notes.append("Column is entirely missing.")
        return rep

    if kind == "numeric":
        _profile_numeric(nonnull, rep, cfg)
    elif kind == "boolean":
        _profile_boolean(nonnull, rep, cfg)
    elif kind == "datetime":
        _profile_datetime(nonnull, rep, cfg)
    elif kind == "categorical":
        _profile_categorical(nonnull, rep, cfg)
    elif kind == "high_cardinality":
        _profile_high_cardinality(nonnull, rep, cfg)
    elif kind == "identifier":
        _profile_identifier(nonnull, rep, cfg)
    return rep


def _profile_numeric(s: pd.Series, rep: ColumnReport, cfg: Config) -> None:
    arr = s.to_numpy(dtype="float64")
    sig = cfg.round_significant
    is_int_like = bool(np.all(np.equal(np.mod(arr, 1), 0)))

    rep.facts["integer_valued"] = is_int_like
    rep.facts["n_zero"] = safe_count(int(np.sum(arr == 0)), cfg)
    rep.facts["n_negative"] = safe_count(int(np.sum(arr < 0)), cfg)
    rep.facts["n_positive"] = safe_count(int(np.sum(arr > 0)), cfg)

    # Integer column with few distinct values is almost certainly a coded
    # category (sex, region, filing status, year...). Researchers need the set
    # of valid codes and their frequencies, not a mean. Report both.
    n_distinct = int(s.nunique())
    if is_int_like and n_distinct <= cfg.max_categories:
        rep.facts["looks_coded_categorical"] = True
        _add_category_counts(s, rep, cfg)

    if rep.n_obs < cfg.min_cell_count:
        rep.notes.append(
            "Too few observations to release summary statistics; suppressed."
        )
        return

    rep.facts["mean"] = round_sig(float(np.mean(arr)), sig)
    rep.facts["sd"] = round_sig(float(np.std(arr, ddof=1)) if rep.n_obs > 1 else 0.0, sig)
    # Robust, rounded quantiles. Exact min/max are NEVER released.
    qs = {
        "p1": 1, "p5": 5, "p10": 10, "p25": 25, "p50": 50,
        "p75": 75, "p90": 90, "p95": 95, "p99": 99,
    }
    quant = {k: round_sig(float(np.percentile(arr, q)), sig) for k, q in qs.items()}
    rep.facts["quantiles_rounded"] = quant
    rep.facts["approx_range_rounded"] = (quant["p1"], quant["p99"])
    rep.notes.append(
        "Exact min/max withheld; range shown is rounded p1-p99. "
        "All numeric summaries rounded to "
        f"{sig} significant figures."
    )

    # Advisory: an integer column with uniformly huge values and no zeros or
    # negatives is very likely an identifier (e.g. a taxpayer ID) that slipped
    # the name test, not a measure. Flag it for the reviewer; do not reclassify.
    if (
        is_int_like
        and np.sum(arr == 0) == 0
        and np.sum(arr < 0) == 0
        and float(np.min(arr)) >= cfg.id_large_int_threshold
    ):
        rep.notes.append(
            "WARNING: integer column with uniformly large values and no zeros or "
            "negatives -- verify this is a genuine measure and not an identifier "
            "that should be withheld."
        )


def _profile_boolean(s: pd.Series, rep: ColumnReport, cfg: Config) -> None:
    n_true = int(s.sum())
    n_false = int(rep.n_obs - n_true)
    rep.facts["n_true"] = safe_count(n_true, cfg)
    rep.facts["n_false"] = safe_count(n_false, cfg)


def _profile_datetime(s: pd.Series, rep: ColumnReport, cfg: Config) -> None:
    years = s.dt.year
    rep.facts["n_distinct_dates"] = int(s.nunique())
    # Years are coarse enough to release as a span (rounded to the year).
    rep.facts["year_min"] = int(years.min())
    rep.facts["year_max"] = int(years.max())
    rep.notes.append("Date detail reduced to calendar-year span.")


def _add_category_counts(s: pd.Series, rep: ColumnReport, cfg: Config) -> None:
    """Populate a suppressed value-count table on the report. Shared by
    string categoricals and integer-coded categoricals."""
    counts = s.value_counts(dropna=True)
    rep.facts["n_distinct"] = int(counts.shape[0])
    shown: dict[str, int] = {}
    n_suppressed_cats = 0
    suppressed_total = 0
    for value, c in counts.items():
        if c < cfg.min_cell_count:
            n_suppressed_cats += 1
            suppressed_total += int(c)
            continue
        shown[str(value)] = int(c)
    rep.facts["categories"] = shown
    if n_suppressed_cats:
        rep.facts["suppressed_categories"] = {
            "n_categories": n_suppressed_cats,
            "combined_count": safe_count(suppressed_total, cfg),
        }
        rep.notes.append(
            f"{n_suppressed_cats} rare category value(s) suppressed (below min cell count)."
        )


def _profile_categorical(s: pd.Series, rep: ColumnReport, cfg: Config) -> None:
    _add_category_counts(s, rep, cfg)


def _profile_high_cardinality(s: pd.Series, rep: ColumnReport, cfg: Config) -> None:
    rep.facts["n_distinct"] = int(s.nunique())
    rep.notes.append(
        f"More than {cfg.max_categories} distinct values; individual values NOT "
        "listed. If the code list is needed, request it separately (subject to lab "
        "approval)."
    )


def _profile_identifier(s: pd.Series, rep: ColumnReport, cfg: Config) -> None:
    n_distinct = int(s.nunique())
    rep.facts["n_distinct"] = n_distinct
    rep.facts["looks_unique"] = bool(n_distinct == rep.n_obs)
    if looks_like_identifier_name(rep.name, cfg):
        rep.notes.append(
            "Column name matches an identifier pattern; treated as a direct "
            "identifier -- values and distribution (mean/quantiles) NOT released."
        )
    else:
        rep.notes.append(
            "High-cardinality / identifier-like column: individual values NOT listed."
        )


# ---------------------------------------------------------------------------
# Dataset-level driver
# ---------------------------------------------------------------------------
def profile_dataframe(
    df: pd.DataFrame, label: str, source: str, cfg: Config | None = None
) -> DatasetReport:
    cfg = cfg or Config()
    report = DatasetReport(
        label=label,
        source=source,
        n_rows=int(df.shape[0]),
        n_cols=int(df.shape[1]),
        config=cfg,
    )
    for col in df.columns:
        report.columns.append(profile_column(df[col], cfg))
    return report


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def _fmt(value: Any) -> str:
    if value is None:
        return "n/a"
    return str(value)


def render_log(report: DatasetReport) -> str:
    cfg = report.config
    lines: list[str] = []
    w = lines.append

    w("=" * 76)
    w("AD4TJ DISCLOSURE-SAFE CODEBOOK")
    w("=" * 76)
    w(f"Dataset      : {report.label}")
    w(f"Source file  : {report.source}")
    w(f"Rows         : {report.n_rows}")
    w(f"Columns      : {report.n_cols}")
    w("")
    w("Disclosure-control rules applied to this log:")
    w(f"  - counts below {cfg.min_cell_count} are suppressed")
    w(f"  - numeric summaries rounded to {cfg.round_significant} significant figures")
    w("  - exact minimum and maximum values are NEVER released")
    w(f"  - category values listed only when distinct count <= {cfg.max_categories}")
    w("  - identifier / high-cardinality column values are not listed")
    w("")
    w("This file contains NO individual-level data points. The data lab must")
    w("still review it against local disclosure policy before release.")
    w("=" * 76)
    w("")

    for c in report.columns:
        w("-" * 76)
        w(f"VARIABLE: {c.name}")
        w(f"  kind            : {c.kind}")
        w(f"  storage dtype   : {c.storage_dtype}")
        w(f"  non-missing     : {c.n_obs}")
        w(f"  missing         : {c.n_missing} ({c.pct_missing}%)")
        _render_facts(c, w)
        for note in c.notes:
            w(f"  note            : {note}")
    w("-" * 76)
    w("")
    w("END OF CODEBOOK")
    return "\n".join(lines) + "\n"


def _render_facts(c: ColumnReport, w: Any) -> None:
    f = c.facts
    if c.kind == "numeric":
        w(f"  integer-valued  : {_fmt(f.get('integer_valued'))}")
        w(f"  # zero          : {_fmt(f.get('n_zero'))}")
        w(f"  # negative      : {_fmt(f.get('n_negative'))}")
        w(f"  # positive      : {_fmt(f.get('n_positive'))}")
        if f.get("looks_coded_categorical"):
            w("  looks coded     : yes (few distinct integer values - likely a code list)")
            w(f"  distinct values : {_fmt(f.get('n_distinct'))}")
            cats = f.get("categories", {})
            if cats:
                w("  value counts    :")
                for value, cnt in cats.items():
                    w(f"      {value!r:>20} : {cnt}")
            sup = f.get("suppressed_categories")
            if sup:
                w(
                    f"      <{sup['n_categories']} rare value(s)> : "
                    f"combined {_fmt(sup['combined_count'])}"
                )
        if "mean" in f:
            w(f"  mean (rounded)  : {_fmt(f.get('mean'))}")
            w(f"  sd (rounded)    : {_fmt(f.get('sd'))}")
            rng = f.get("approx_range_rounded")
            if rng:
                w(f"  approx range    : {_fmt(rng[0])} .. {_fmt(rng[1])}  (rounded p1-p99)")
            q = f.get("quantiles_rounded", {})
            if q:
                qline = "  ".join(f"{k}={_fmt(v)}" for k, v in q.items())
                w(f"  quantiles       : {qline}")
    elif c.kind == "boolean":
        w(f"  # true          : {_fmt(f.get('n_true'))}")
        w(f"  # false         : {_fmt(f.get('n_false'))}")
    elif c.kind == "datetime":
        w(f"  distinct dates  : {_fmt(f.get('n_distinct_dates'))}")
        w(f"  year span       : {_fmt(f.get('year_min'))} .. {_fmt(f.get('year_max'))}")
    elif c.kind == "categorical":
        w(f"  distinct values : {_fmt(f.get('n_distinct'))}")
        cats = f.get("categories", {})
        if cats:
            w("  value counts    :")
            for value, cnt in cats.items():
                w(f"      {value!r:>20} : {cnt}")
        sup = f.get("suppressed_categories")
        if sup:
            w(
                f"      <{sup['n_categories']} rare value(s)> : "
                f"combined {_fmt(sup['combined_count'])}"
            )
    elif c.kind == "high_cardinality":
        w(f"  distinct values : {_fmt(f.get('n_distinct'))}")
    elif c.kind == "identifier":
        w(f"  distinct values : {_fmt(f.get('n_distinct'))}")
        w(f"  unique per row  : {_fmt(f.get('looks_unique'))}")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------
def _derive_output_path(data_path: str) -> str:
    base = os.path.splitext(data_path)[0]
    return base + ".codebook.log"


def main() -> int:
    data_path = DATA_PATH
    if not os.path.exists(data_path):
        sys.stderr.write(f"ERROR: data file not found: {data_path}\n")
        return 2

    label = DATASET_LABEL or os.path.basename(data_path)
    cfg = Config()

    df = read_data(data_path)
    report = profile_dataframe(df, label=label, source=data_path, cfg=cfg)
    text = render_log(report)

    out_path = OUTPUT_PATH or _derive_output_path(data_path)
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(text)

    sys.stderr.write(f"Codebook written to: {out_path}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
