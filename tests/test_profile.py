"""Tests for the disclosure-safe profiler.

The most important tests assert the *safety* property: no individual data point
ever reaches the rendered log.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

import profile_dataset as pds
from profile_dataset import (
    Config,
    looks_like_identifier_name,
    profile_column,
    profile_dataframe,
    render_log,
    round_sig,
    safe_count,
)


# --------------------------------------------------------------------------- #
# Safe numeric helpers
# --------------------------------------------------------------------------- #
def test_round_sig():
    assert round_sig(123456.0, 3) == 123000.0
    assert round_sig(0.0123456, 3) == 0.0123
    assert round_sig(0.0, 3) == 0.0
    assert round_sig(None, 3) is None
    assert round_sig(float("nan"), 3) is None


def test_safe_count_suppresses_small_nonzero():
    cfg = Config(min_cell_count=10)
    assert safe_count(0, cfg) == 0          # zero always safe
    assert safe_count(3, cfg) == pds.SUPPRESSED
    assert safe_count(9, cfg) == pds.SUPPRESSED
    assert safe_count(10, cfg) == 10
    assert safe_count(5000, cfg) == 5000


# --------------------------------------------------------------------------- #
# Column classification
# --------------------------------------------------------------------------- #
def test_unique_integer_is_identifier():
    s = pd.Series(range(1000, 2000), name="person_id")
    rep = profile_column(s, Config())
    assert rep.kind == "identifier"
    # an identifier must NOT release quantiles or a range
    assert "quantiles_rounded" not in rep.facts
    assert "approx_range_rounded" not in rep.facts


def test_low_cardinality_integer_is_coded_categorical():
    s = pd.Series([1, 2, 3, 4] * 500, name="filing_status")
    rep = profile_column(s, Config())
    assert rep.kind == "numeric"
    assert rep.facts.get("looks_coded_categorical") is True
    assert set(rep.facts["categories"]) == {"1", "2", "3", "4"}


def test_panel_taxpayer_id_is_identifier_not_measure():
    # r_tpin from the ZRA data: a 12-digit taxpayer ID repeating across a panel.
    # Distinct/rows is low (IDs repeat), so the near-unique test misses it; the
    # name test must still classify it as an identifier and withhold its stats.
    rng = np.random.default_rng(7)
    ids = rng.integers(122_000_000_000, 353_000_000_000, size=500)
    panel = pd.Series(np.repeat(ids, 12), name="r_tpin")  # 12 periods each
    rep = profile_column(panel, Config())
    assert rep.kind == "identifier"
    assert "mean" not in rep.facts
    assert "quantiles_rounded" not in rep.facts
    log = render_log(profile_dataframe(pd.DataFrame({"r_tpin": panel}), "t", "t"))
    assert "122000000000" not in log      # no TPIN-range figures leak
    assert any("identifier pattern" in n for n in rep.notes)


def test_identifier_name_variants_and_false_friends():
    cfg = Config()
    for name in ["tin", "r_tpin", "taxpayer_id", "firm_id", "nrc", "national_id", "ReturnID"]:
        assert looks_like_identifier_name(name, cfg), name
    for name in ["valid_flag", "gross_income", "region_code", "rapid_ratio", "tax_due"]:
        assert not looks_like_identifier_name(name, cfg), name


def test_continuous_float_stays_numeric():
    rng = np.random.default_rng(0)
    s = pd.Series(rng.lognormal(10, 1, 5000), name="income")
    rep = profile_column(s, Config())
    assert rep.kind == "numeric"
    assert rep.facts.get("looks_coded_categorical") is None
    assert "quantiles_rounded" in rep.facts


# --------------------------------------------------------------------------- #
# Missingness
# --------------------------------------------------------------------------- #
def test_datetime_coarsened_to_year_span():
    # dates must be reduced to a year span; exact dates must not be listed
    days = pd.to_datetime("2016-03-01") + pd.to_timedelta(
        np.random.default_rng(3).integers(0, 1400, size=500), unit="D"
    )
    s = pd.Series(days, name="signed_on")
    rep = profile_column(s, Config())
    assert rep.kind == "datetime"
    assert rep.facts["year_min"] == 2016
    assert rep.facts["year_max"] >= 2019
    log = render_log(profile_dataframe(pd.DataFrame({"signed_on": s}), "t", "t"))
    assert "year span" in log
    assert "2016-03" not in log            # no exact date leaks


def test_missing_counts():
    s = pd.Series([1.0, 2.0, np.nan, np.nan, 5.0], name="x")
    rep = profile_column(s, Config(min_cell_count=1))
    assert rep.n_obs == 3
    assert rep.n_missing == 2
    assert rep.pct_missing == 40.0


# --------------------------------------------------------------------------- #
# DISCLOSURE SAFETY  --  the whole point of the tool
# --------------------------------------------------------------------------- #
def test_exact_extremes_never_leak():
    # a unique, recognisable maximum and minimum buried in ordinary data
    rng = np.random.default_rng(1)
    body = rng.integers(1000, 2000, size=500).astype(float)
    s = pd.Series(np.concatenate([[8675309.0, 1.0], body]), name="salary")
    log = render_log(profile_dataframe(pd.DataFrame({"salary": s}), "t", "t"))
    assert "8675309" not in log          # exact max must never appear
    # the rounded summaries should be present though
    assert "approx range" in log
    # no fact line may report an exact minimum or maximum
    fact_labels = [
        ln.split(":")[0].strip().lower()
        for ln in log.splitlines()
        if ln.startswith("  ") and ":" in ln
    ]
    assert "min" not in fact_labels
    assert "max" not in fact_labels
    assert "minimum" not in fact_labels
    assert "maximum" not in fact_labels


def test_rare_category_value_is_suppressed():
    # 'ZZ' appears only 3 times -> must be suppressed, value hidden
    vals = ["AA"] * 300 + ["BB"] * 200 + ["ZZ"] * 3
    s = pd.Series(vals, name="group")
    rep = profile_column(s, Config(min_cell_count=10))
    assert "ZZ" not in rep.facts["categories"]
    assert rep.facts["suppressed_categories"]["n_categories"] == 1
    log = render_log(profile_dataframe(pd.DataFrame({"group": s}), "t", "t"))
    assert "ZZ" not in log
    assert "AA" in log and "BB" in log    # safe categories still shown


def test_high_cardinality_values_not_listed():
    # 200 distinct codes among 2000 rows: a real category, but too many to list.
    # Not near-unique (so not an identifier), yet must NOT be enumerated.
    codes = [f"HS{i:04d}" for i in range(200)] * 10
    s = pd.Series(codes, name="hscode")
    rep = profile_column(s, Config(max_categories=20))
    assert rep.kind == "high_cardinality"
    assert rep.facts["n_distinct"] == 200
    assert "categories" not in rep.facts
    log = render_log(profile_dataframe(pd.DataFrame({"hscode": s}), "t", "t"))
    assert "HS0000" not in log
    assert "HS0199" not in log


def test_identifier_string_values_not_listed():
    s = pd.Series([f"name_{i}" for i in range(500)], name="full_name")
    rep = profile_column(s, Config())
    assert rep.kind == "identifier"
    log = render_log(profile_dataframe(pd.DataFrame({"full_name": s}), "t", "t"))
    assert "name_0" not in log
    assert "name_499" not in log


def test_too_few_observations_suppresses_summary():
    s = pd.Series([100.5, 100.5, 200.5, 300.5], name="x")  # 4 obs, below default 10
    rep = profile_column(s, Config(min_cell_count=10))
    assert "mean" not in rep.facts
    assert any("Too few observations" in n for n in rep.notes)


# --------------------------------------------------------------------------- #
# End-to-end on the worked example (if demo data has been generated)
# --------------------------------------------------------------------------- #
def test_end_to_end_income_tax(tmp_path):
    import os
    demo = os.path.join(os.path.dirname(__file__), "..", "examples", "income_tax", "returns.csv")
    if not os.path.exists(demo):
        pytest.skip("demo data not generated")
    df = pd.read_csv(demo)
    report = profile_dataframe(df, "returns", demo)
    log = render_log(report)
    assert "AD4TJ DISCLOSURE-SAFE CODEBOOK" in log
    assert "gross_income" in log
    # gross income exact maximum (a single taxpayer's income) must not leak
    gross_max = str(int(df["gross_income"].max()))
    assert gross_max not in log
