"""Regression tests for the eight real-world failures the profiler used to miss.

Each test reproduces a failure that cost debugging time and asserts the codebook
now *reports* it. Disclosure assertions are included alongside: a new diagnostic
must not leak values.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

import numpy as np
import pandas as pd
import pytest

import profile_dataset as pds
from profile_dataset import (
    Config,
    analyse_consistency,
    analyse_structure,
    analyse_text,
    detect_source_format,
    profile_column,
    profile_file,
    read_data,
    render_consistency,
    render_log,
)

HERE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(HERE, "fixtures")
UTF16 = os.path.join(FIX, "cz_returns_utf16.csv")
OLD = os.path.join(FIX, "returns_2018_2022.csv")
NEW = os.path.join(FIX, "returns_2023.csv")
OLD_DTA = os.path.join(FIX, "returns_2018_2022.dta")
NEW_DTA = os.path.join(FIX, "returns_2023.dta")
LONG = os.path.join(FIX, "tp_annex_long.csv")


def _ensure_fixtures():
    if not os.path.exists(UTF16):
        subprocess.run([sys.executable, os.path.join(FIX, "_make_fixtures.py")], check=True)


@pytest.fixture(scope="module", autouse=True)
def fixtures():
    _ensure_fixtures()


# --------------------------------------------------------------------------- #
# Failure 1: the profiler read a CSV Stata could not, and never said how
# --------------------------------------------------------------------------- #
def test_utf16_semicolon_format_is_detected_and_reported():
    fmt = detect_source_format(UTF16, Config())
    assert fmt.container == "delimited text"
    assert fmt.encoding.startswith("utf-16")
    assert "present" in fmt.bom and "UTF-16" in fmt.bom
    assert "semicolon" in fmt.delimiter
    assert fmt.encoding_confidence == "from byte-order mark"
    assert len(fmt.sha256) == 64
    assert fmt.size_bytes > 0


def test_read_command_names_encoding_and_delimiter():
    rep = profile_file(UTF16, Config(), nominated=["ic", "rok"])
    stata = rep.read_commands["stata"]
    assert 'delimiter(";")' in stata
    assert 'encoding("utf-16")' in stata
    assert "stringcols(" in stata          # ic has leading zeros
    assert "destring" in stata             # kc_dpp_i1 is numeric-as-text
    assert 'sep=' in rep.read_commands["pandas"]


def test_header_line_shown_but_no_data_row():
    rep = profile_file(UTF16, Config(), nominated=[])
    log = render_log(rep)
    assert "kc_dpp_i1" in log                       # header names are metadata
    df, _ = read_data(UTF16, detect_source_format(UTF16, Config()))
    # no verbatim data value from the amount column may appear anywhere
    for v in df["kc_dpp_i1"].head(30):
        if v.strip() and v.strip().lower() not in pds.PLACEHOLDER_TOKENS:
            assert v not in log


def test_headerless_file_refuses_to_print_the_header_line(tmp_path):
    # a file with no header: the "header" IS a data row and must be withheld
    p = tmp_path / "headerless.csv"
    p.write_text("101,2023,55.5\n102,2023,66.5\n103,2023,77.5\n", encoding="utf-8")
    fmt = detect_source_format(str(p), Config())
    assert fmt.header_line == ""
    assert "no header row" in fmt.header_line_withheld_reason
    assert "101" not in fmt.header_line


# --------------------------------------------------------------------------- #
# Failure 2 + 3: text columns holding numbers; missingness means two things
# --------------------------------------------------------------------------- #
def test_numeric_as_text_is_flagged_with_a_destring_hint():
    s = pd.Series(["1 234,50", "998,00", "12 000,00", "7,25"] * 30, name="kc_dpp_i1")
    d = analyse_text(s, Config())
    assert d.share_numeric_lenient >= 0.9
    assert d.share_numeric_strict < 0.5
    assert d.numeric_as_text is True
    assert d.decimal_separator == ","
    assert "dpcomma" in d.destring_hint
    rep = profile_column(s, Config())
    assert rep.storage.startswith("str")
    assert rep.kind == "numeric"                 # semantics, not storage
    assert any("NUMERIC-AS-TEXT" in w for w in rep.warnings)


def test_plain_numeric_text_is_not_falsely_flagged():
    s = pd.Series([f"{v}.50" for v in range(100, 400)], name="amount")
    d = analyse_text(s, Config())
    assert d.share_numeric_strict >= 0.99
    assert d.numeric_as_text is False            # a plain import already works


def test_leading_zeros_flagged_so_merge_keys_survive():
    s = pd.Series([f"{i:08d}" for i in range(1, 200)], name="ic")
    rep = profile_column(s, Config())
    assert any("LEADING ZEROS" in w for w in rep.warnings)


def test_missing_kinds_separate_empty_string_from_true_null():
    s = pd.Series(["" if i % 10 else "5" for i in range(500)] + [None] * 20
                  + ["   "] * 15 + ["NULL"] * 12, name="kc_ii10_10")
    rep = profile_column(s, Config())
    m = rep.missing_kinds
    assert m.empty_string != 0
    assert m.true_null != 0
    assert m.whitespace_only != 0
    assert m.placeholder != 0
    assert "null" in m.placeholder_tokens_seen


def test_parse_blockers_report_patterns_not_values():
    s = pd.Series(["1 234,50"] * 60 + ["€99,00"] * 20 + ["(7,50)"] * 15, name="amt")
    d = analyse_text(s, Config())
    assert "contains a space" in d.blockers
    assert "contains a currency symbol" in d.blockers
    assert "parenthesised negative" in d.blockers
    # blockers are shares, never values
    assert all(not isinstance(v, str) or v == pds.SUPPRESSED for v in d.blockers.values())
    log = render_log(pds.profile_dataframe(pd.DataFrame({"amt": s}), "t", "t"))
    assert "1 234,50" not in log
    assert "€99,00" not in log


# --------------------------------------------------------------------------- #
# Failure 4: storage types drift between vintages -- fails SILENTLY
# --------------------------------------------------------------------------- #
def test_storage_conflict_between_vintages_is_reported():
    # the real scenario: two Stata vintages whose key columns disagree on
    # storage type. Appending and merging these matches zero rows silently.
    cfg = Config()
    reps = [profile_file(p, cfg, nominated=["ic"], keep_key_values=True)
            for p in (OLD_DTA, NEW_DTA)]
    con = analyse_consistency(reps, cfg, nominated=["ic"])
    text = render_consistency(con, cfg)
    # ic is numeric in the old vintage and text (leading zeros) in the new one
    assert "ic" in con.storage_conflicts
    assert "STORAGE TYPE CONFLICTS" in text
    assert "matches ZERO rows" in text


def test_columns_present_in_only_some_files_are_listed():
    cfg = Config()
    reps = [profile_file(p, cfg, nominated=["ic"], keep_key_values=True) for p in (OLD, NEW)]
    con = analyse_consistency(reps, cfg, nominated=["ic"])
    assert "new_levy" in con.columns_partial
    assert len(con.columns_partial["new_levy"]) == 1


def test_key_overlap_is_reported_and_suppressed():
    cfg = Config()
    reps = [profile_file(p, cfg, nominated=["ic"], keep_key_values=True) for p in (OLD, NEW)]
    con = analyse_consistency(reps, cfg, nominated=["ic"])
    assert con.key_overlap, "expected an overlap row for the nominated key"
    o = con.key_overlap[0]
    assert o["column"] == "ic"
    assert o["in_both"] in (0, pds.SUPPRESSED) or isinstance(o["in_both"], int)


# --------------------------------------------------------------------------- #
# Failure 5: the row key was never stated
# --------------------------------------------------------------------------- #
def test_long_over_subentity_is_detected():
    cfg = Config()
    df, _ = read_data(LONG, detect_source_format(LONG, cfg))
    st = analyse_structure(df, cfg, nominated=["ic", "rok"])
    assert st.key_is_unique is False
    assert st.dup_profile["n_distinct_keys"] > 0
    assert "counterparty_seq" in st.varying_within_key
    assert "LONG over" in st.grain_note


def test_unique_key_is_detected_when_present():
    df = pd.DataFrame({
        "ic": np.repeat(np.arange(100), 3),
        "rok": np.tile([2021, 2022, 2023], 100),
        "turnover": np.random.default_rng(0).lognormal(12, 1, 300),
    })
    st = analyse_structure(df, Config(), nominated=["ic", "rok"])
    assert st.key_is_unique is True
    assert st.chosen_key == ["ic", "rok"]


# --------------------------------------------------------------------------- #
# Failure 6: contaminated columns invisible among hundreds of variables
# --------------------------------------------------------------------------- #
def test_contaminated_column_is_flagged():
    rng = np.random.default_rng(5)
    vals = rng.integers(1, 400, size=2000).astype(float)
    vals[:3] = vals[:3] * 3_000_000        # a handful a million times too large
    rep = profile_column(pd.Series(vals, name="nbemployees"), Config())
    assert any("SUSPECT" in w for w in rep.warnings)
    log = render_log(pds.profile_dataframe(pd.DataFrame({"nbemployees": vals}), "t", "t"))
    assert "SUSPECT" in log
    # the offending values themselves must never appear
    for v in vals[:3]:
        assert str(int(v)) not in log


def test_clean_column_is_not_flagged():
    rng = np.random.default_rng(6)
    vals = rng.normal(100, 15, size=2000)
    rep = profile_column(pd.Series(vals, name="score"), Config())
    assert not any("SUSPECT" in w for w in rep.warnings)


# --------------------------------------------------------------------------- #
# Failure 7: coded variables arrive without code lists
# --------------------------------------------------------------------------- #
def test_uncoded_categorical_appears_in_code_list_requests():
    rep = profile_file(UTF16, Config(), nominated=[])
    names = [r["column"] for r in rep.code_lists_to_request]
    assert "spoj_zahr" in names
    log = render_log(rep)
    assert "CODE LISTS TO REQUEST" in log
    assert "spoj_zahr" in log


def test_value_labels_are_capped_at_the_category_limit():
    # SPEC sec.4 rules 4/5: a large label set must not be dumped into the log
    big = {i: f"label {i}" for i in range(200)}
    s = pd.Series(np.arange(200) % 50, name="sector")
    rep = profile_column(s, Config(), value_labels=big)
    assert rep.value_labels is None
    assert any("request the code list separately" in n for n in rep.notes)
    small = {1: "Resident", 2: "Non-resident"}
    rep2 = profile_column(pd.Series([1, 2] * 50, name="residency"), Config(),
                          value_labels=small)
    assert rep2.value_labels == {"1": "Resident", "2": "Non-resident"}


# --------------------------------------------------------------------------- #
# K: machine-readable companion carries the same content and the same controls
# --------------------------------------------------------------------------- #
def test_json_companion_matches_and_leaks_nothing(tmp_path):
    cfg = Config()
    rep = profile_file(UTF16, cfg, nominated=["ic", "rok"])
    p = tmp_path / "out.json"
    pds.write_json(rep, str(p))
    d = json.loads(p.read_text(encoding="utf-8"))
    assert d["source_format"]["encoding"].startswith("utf-16")
    assert d["structure"]["n_rows"] > 0
    cols = {c["name"]: c for c in d["columns"]}
    assert cols["kc_dpp_i1"]["kind"] == "numeric"
    assert cols["kc_dpp_i1"]["storage"].startswith("str")
    # no raw value from the file may appear in the JSON
    df, _ = read_data(UTF16, detect_source_format(UTF16, cfg))
    blob = p.read_text(encoding="utf-8")
    for v in df["kc_dpp_i1"].head(30):
        if v.strip() and v.strip().lower() not in pds.PLACEHOLDER_TOKENS:
            assert v not in blob


def test_identifier_distribution_still_withheld_in_new_pipeline():
    rep = profile_file(UTF16, Config(), nominated=[])
    ic = [c for c in rep.columns if c.name == "ic"][0]
    assert ic.kind == "identifier"
    assert "mean" not in ic.facts
    assert "quantiles_rounded" not in ic.facts
