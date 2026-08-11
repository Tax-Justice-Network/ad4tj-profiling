#!/usr/bin/env python3
"""
AD4TJ disclosure-safe dataset profiler  (Python reference implementation)
=========================================================================

Run this INSIDE the secure data-lab environment, on the REAL confidential data.
It writes a *codebook log file* describing the dataset so that researchers can
write correct, runnable analysis code WITHOUT ever seeing the data.

The log NEVER releases an individual data point. It reports only aggregates,
and applies conservative statistical-disclosure-control (SDC) rules -- see
SPEC.md, which is authoritative:

  * counts below MIN_CELL_COUNT are suppressed,
  * exact minimum/maximum values are never released,
  * numeric summaries are rounded to a few significant figures,
  * string/identifier values are not printed.

The lab remains the final authority: review the log and your local SDC policy
before releasing it.

Beyond describing variables, the log records HOW THE FILE WAS READ (encoding,
delimiter, quoting) and emits a ready-made import command, so that code written
against the codebook actually runs against the real file.

----------------------------------------------------------------------------
HOW TO USE
----------------------------------------------------------------------------
1. Edit the CONFIG block below: set DATA_PATH to your data file.
2. Run:   python profile_dataset.py
3. A "<dataset>.codebook.log" and "<dataset>.codebook.json" are written next to
   your data. Review them, then release to the researcher.

Optional command line (overrides CONFIG):
    python profile_dataset.py FILE [FILE ...] [--key ic,rok] [--out DIR]

Supported input formats: .csv, .tsv, .txt, .xlsx/.xls, .dta (Stata), .parquet
"""

from __future__ import annotations

# ===========================================================================
# CONFIG  --  edit this block, nothing else is required
# ===========================================================================
DATA_PATH = "examples/income_tax/returns.csv"   # <-- path to your data file
OUTPUT_PATH = ""           # output log path; "" = derive from DATA_PATH
DATASET_LABEL = ""         # optional human title; "" = use the file name

# Candidate row key, e.g. "ic,rok". "" = infer from column names.
CANDIDATE_KEY = ""

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

# Text-column diagnostics
NUMERIC_AS_TEXT_THRESHOLD = 0.90   # share parsing as numeric => "really a number"
STRICT_PARSE_OK = 0.99             # above this, a plain import already works

# Contaminated-distribution flags
SUSPECT_MEAN_P99_RATIO = 2.0       # mean this many times p99 => flag
SUSPECT_MEAN_MEDIAN_RATIO = 1000.0 # mean this many times median => flag
IQR_OUTLIER_MULTIPLE = 50.0        # beyond Q3 + k*IQR (or Q1 - k*IQR) => outlier

SAMPLE_N = 0               # 0 = profile every row; else profile a random sample
SEED = 20260101
# ===========================================================================


import argparse
import codecs
import csv as _csv
import dataclasses
import hashlib
import json
import math
import os
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from itertools import combinations
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
NOT_SHOWN = "<withheld>"

# Tokens that stand in for "no value" in exported administrative data.
PLACEHOLDER_TOKENS = ("null", "na", "n/a", "-", ".", "none", "nan", "#n/a", "?", "*")

_CURRENCY_CHARS = "$€£¥₹"
_SPACE_CHARS = (" ", " ", " ", " ")
_DELIMITER_CANDIDATES = (";", ",", "\t", "|")

# Column names that commonly form a row key in administrative tax data.
KEY_NAME_HINTS = ("id", "ic", "dic", "tin", "tpin", "year", "rok", "period",
                  "month", "quarter", "seq", "no", "nr")


@dataclass
class Config:
    min_cell_count: int = MIN_CELL_COUNT
    round_significant: int = ROUND_SIGNIFICANT
    max_categories: int = MAX_CATEGORIES
    id_like_unique_ratio: float = ID_LIKE_UNIQUE_RATIO
    id_name_hints: tuple[str, ...] = ID_NAME_HINTS
    id_large_int_threshold: float = ID_LARGE_INT_THRESHOLD
    numeric_as_text_threshold: float = NUMERIC_AS_TEXT_THRESHOLD
    strict_parse_ok: float = STRICT_PARSE_OK
    suspect_mean_p99_ratio: float = SUSPECT_MEAN_P99_RATIO
    suspect_mean_median_ratio: float = SUSPECT_MEAN_MEDIAN_RATIO
    iqr_outlier_multiple: float = IQR_OUTLIER_MULTIPLE


# Exact name-tokens and end-of-token forms that denote a direct identifier.
_ID_NAME_TOKENS = {"tin", "tpin", "tpn", "id", "uid", "guid", "nrc", "ssn", "pin", "brn", "uin"}
_ID_FALSE_FRIENDS = {"valid", "invalid", "solid", "rapid", "humid", "rigid", "fluid",
                     "hybrid", "candid", "stupid", "liquid", "placid", "florid", "putrid"}


def is_text_dtype(series: pd.Series) -> bool:
    """True for text columns. pandas >= 2.3 may infer a dedicated ``str`` dtype
    rather than ``object``; both must be treated as text or every text
    diagnostic silently skips."""
    dt = series.dtype
    if dt == object:  # noqa: E721
        return True
    if pd.api.types.is_bool_dtype(dt) or pd.api.types.is_numeric_dtype(dt):
        return False
    if pd.api.types.is_datetime64_any_dtype(dt):
        return False
    try:
        return bool(pd.api.types.is_string_dtype(dt))
    except Exception:  # pragma: no cover - defensive
        return False


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


# ---------------------------------------------------------------------------
# Report structures
# ---------------------------------------------------------------------------
@dataclass
class SourceFormat:
    """How the file was actually read. Metadata only -- never a data row."""
    file_name: str = ""
    path: str = ""
    size_bytes: int = 0
    modified_utc: str = ""
    sha256: str = ""
    container: str = ""            # delimited | stata | excel | parquet
    encoding: str = ""
    encoding_confidence: str = ""  # from byte-order mark | verified | guess
    bom: str = "absent"
    delimiter: str = ""
    quote_char: str = ""
    escape_char: str = ""
    line_terminator: str = ""
    header_rows: int = 0
    preamble_rows_skipped: int = 0
    n_columns_in_header: int = 0
    header_line: str = ""
    header_line_withheld_reason: str = ""
    stata_version: str = ""
    dataset_label: str = ""
    notes: list[str] = field(default_factory=list)


@dataclass
class TextDiagnostics:
    """Why a text column may or may not survive a naive numeric import."""
    n_nonempty: int = 0
    share_numeric_strict: float = 0.0
    share_numeric_lenient: float = 0.0
    numeric_as_text: bool = False
    decimal_separator: str = ""
    thousands_separator: str = ""
    blockers: dict[str, Any] = field(default_factory=dict)   # pattern -> share
    share_leading_zeros: float = 0.0
    destring_hint: str = ""


@dataclass
class MissingBreakdown:
    true_null: Any = 0
    empty_string: Any = 0
    whitespace_only: Any = 0
    placeholder: Any = 0
    placeholder_tokens_seen: list[str] = field(default_factory=list)


@dataclass
class ColumnReport:
    name: str
    kind: str                      # numeric | categorical | datetime | boolean
                                   # | identifier | high_cardinality | empty
    storage: str = ""              # str8 / double / int64 / text(source) ...
    storage_dtype: str = ""        # raw pandas/source dtype
    n_obs: int = 0                 # non-missing count
    n_missing: int = 0
    pct_missing: float = 0.0
    label: str = ""
    facts: dict[str, Any] = field(default_factory=dict)
    text: TextDiagnostics | None = None
    missing_kinds: MissingBreakdown | None = None
    value_labels: dict[str, str] | None = None
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass
class StructureReport:
    n_rows: int = 0
    n_cols: int = 0
    candidate_key_columns: list[str] = field(default_factory=list)
    unique_keys: list[list[str]] = field(default_factory=list)
    tested_combinations: int = 0
    chosen_key: list[str] = field(default_factory=list)
    key_is_unique: bool = False
    dup_profile: dict[str, Any] = field(default_factory=dict)
    varying_within_key: list[str] = field(default_factory=list)
    constant_within_key: list[str] = field(default_factory=list)
    grain_note: str = ""


@dataclass
class ConsistencyReport:
    files: list[str] = field(default_factory=list)
    columns_everywhere: list[str] = field(default_factory=list)
    columns_partial: dict[str, list[str]] = field(default_factory=dict)
    storage_conflicts: dict[str, dict[str, str]] = field(default_factory=dict)
    key_overlap: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class DatasetReport:
    label: str
    source: str
    n_rows: int
    n_cols: int
    config: Config
    source_format: SourceFormat | None = None
    structure: StructureReport | None = None
    columns: list[ColumnReport] = field(default_factory=list)
    read_commands: dict[str, str] = field(default_factory=dict)
    code_lists_to_request: list[dict[str, Any]] = field(default_factory=list)
    sampled: bool = False
    sample_fraction: float = 1.0
    rows_in_file: int = 0


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


def safe_share(c: int, total: int, cfg: Config) -> float | str:
    """A share is only released if the underlying count clears the threshold."""
    if c == 0:
        return 0.0
    if c < cfg.min_cell_count:
        return SUPPRESSED
    return round(100.0 * c / total, 1) if total else 0.0


# ---------------------------------------------------------------------------
# A. Source-format detection
# ---------------------------------------------------------------------------
_BOMS = (
    (codecs.BOM_UTF32_LE, "utf-32-le", "UTF-32-LE"),
    (codecs.BOM_UTF32_BE, "utf-32-be", "UTF-32-BE"),
    (codecs.BOM_UTF8, "utf-8-sig", "UTF-8"),
    (codecs.BOM_UTF16_LE, "utf-16-le", "UTF-16-LE"),
    (codecs.BOM_UTF16_BE, "utf-16-be", "UTF-16-BE"),
)


def sha256_of(path: str, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            b = fh.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def _detect_encoding(raw: bytes) -> tuple[str, str, str]:
    """Return (encoding, bom_description, confidence)."""
    for bom, enc, desc in _BOMS:
        if raw.startswith(bom):
            return enc, f"present ({desc})", "from byte-order mark"
    # No BOM. UTF-16 without a BOM shows as regular NUL bytes.
    sample = raw[:4096]
    if sample:
        nul = sample.count(0)
        if nul > len(sample) * 0.25:
            even = sum(1 for i in range(0, len(sample), 2) if sample[i] == 0)
            odd = sum(1 for i in range(1, len(sample), 2) if sample[i] == 0)
            enc = "utf-16-be" if even > odd else "utf-16-le"
            return enc, "absent", "guess (NUL byte pattern)"
    try:
        raw.decode("utf-8")
        return "utf-8", "absent", "verified (decodes as UTF-8)"
    except UnicodeDecodeError:
        pass
    for enc in ("cp1250", "cp1252"):
        try:
            raw.decode(enc)
            return enc, "absent", f"guess (not UTF-8; {enc} decodes cleanly)"
        except UnicodeDecodeError:
            continue
    return "latin-1", "absent", "fallback (latin-1 always decodes; verify locally)"


def _count_outside_quotes(line: str, ch: str, quote: str = '"') -> int:
    n, inq = 0, False
    for c in line:
        if c == quote:
            inq = not inq
        elif c == ch and not inq:
            n += 1
    return n


def _detect_delimiter(lines: list[str]) -> tuple[str, str]:
    """Pick the delimiter with a consistent, non-zero field count across lines."""
    best, best_score = ",", (-1.0, 0)
    for cand in _DELIMITER_CANDIDATES:
        counts = [_count_outside_quotes(ln, cand) for ln in lines if ln.strip()]
        if not counts or counts[0] == 0:
            continue
        consistent = sum(1 for c in counts if c == counts[0]) / len(counts)
        score = (consistent, counts[0])
        if score > best_score:
            best_score, best = score, cand
    if best_score[0] < 0:
        # nothing looked delimited; fall back to csv.Sniffer, else comma
        try:
            best = _csv.Sniffer().sniff("\n".join(lines[:20])).delimiter
        except Exception:
            best = ","
    name = {"\t": "tab", ";": "semicolon", ",": "comma", "|": "pipe"}.get(best, repr(best))
    return best, name


def _header_looks_like_data(fields: list[str]) -> str:
    """Guard: if a file has no header row, the 'header' IS a data row and MUST
    NOT be printed (SPEC.md sec.1). Return a reason string, or '' if it looks
    like a genuine header."""
    if not fields:
        return "no fields parsed"
    stripped = [f.strip().strip('"') for f in fields]
    nonempty = [f for f in stripped if f]
    if not nonempty:
        return "header fields are all empty"
    numericish = sum(1 for f in nonempty if re.fullmatch(r"[-+]?[\d .,]+", f))
    if numericish > len(nonempty) * 0.5:
        return "more than half of the header fields are numeric -- file may have no header row"
    lowered = [f.lower() for f in nonempty]
    if len(set(lowered)) < len(lowered) * 0.7:
        return "header fields are largely duplicated -- file may have no header row"
    return ""


def detect_source_format(path: str, cfg: Config) -> SourceFormat:
    st = os.stat(path)
    fmt = SourceFormat(
        file_name=os.path.basename(path),
        path=path,
        size_bytes=st.st_size,
        modified_utc=datetime.fromtimestamp(st.st_mtime, tz=UTC)
        .strftime("%Y-%m-%d %H:%M:%S UTC"),
        sha256=sha256_of(path),
    )
    ext = os.path.splitext(path)[1].lower()

    if ext in (".csv", ".tsv", ".txt"):
        fmt.container = "delimited text"
        with open(path, "rb") as fh:
            raw = fh.read(1 << 16)
        enc, bom, conf = _detect_encoding(raw)
        fmt.encoding, fmt.bom, fmt.encoding_confidence = enc, bom, conf
        text = raw.decode(enc, errors="replace")
        # strip a decoded BOM character if the codec left one
        text = text.lstrip("﻿")
        if "\r\n" in text:
            fmt.line_terminator = "CRLF (\\r\\n)"
        elif "\n" in text:
            fmt.line_terminator = "LF (\\n)"
        elif "\r" in text:
            fmt.line_terminator = "CR (\\r)"
        else:
            fmt.line_terminator = "unknown (single line in sample)"
        lines = text.splitlines()
        # preamble: leading lines whose field count differs from the modal count
        delim, delim_name = _detect_delimiter(lines[:50])
        fmt.delimiter = f"{delim_name} ({delim!r})" if delim != "\t" else "tab ('\\t')"
        counts = [_count_outside_quotes(ln, delim) for ln in lines[:50] if ln.strip()]
        modal = Counter(counts).most_common(1)[0][0] if counts else 0
        preamble = 0
        for c in counts:
            if c == modal:
                break
            preamble += 1
        fmt.preamble_rows_skipped = preamble
        fmt.header_rows = 1
        header_line = ""
        for ln in lines[preamble:]:
            if ln.strip():
                header_line = ln
                break
        try:
            fields = next(_csv.reader([header_line], delimiter=delim))
        except Exception:
            fields = header_line.split(delim)
        fmt.n_columns_in_header = len(fields)
        quoted = '"' in text
        fmt.quote_char = '"'
        if not quoted:
            fmt.notes.append(
                'No quote characters seen in the sample; the standard \'"\' is assumed.'
            )
        fmt.escape_char = '"" (doubled quote)' if '""' in text else "(none detected)"
        reason = _header_looks_like_data(fields)
        if reason:
            fmt.header_line_withheld_reason = reason
            fmt.notes.append(
                "Header line WITHHELD: " + reason + ". A data row must never be "
                "printed, so only the parsed column count is reported."
            )
        else:
            fmt.header_line = header_line[:400] + ("..." if len(header_line) > 400 else "")
        fmt.notes.append(
            "Header line is column metadata and is shown verbatim; NO data row is "
            "ever printed."
        )
    elif ext == ".dta":
        fmt.container = "stata"
        fmt.encoding = "(declared inside the .dta)"
        try:
            with pd.io.stata.StataReader(path) as rdr:
                fmt.stata_version = str(getattr(rdr, "format_version", "") or "")
                lbl = ""
                try:
                    lbl = rdr.data_label or ""
                except Exception:
                    lbl = ""
                fmt.dataset_label = lbl
        except Exception as exc:  # pragma: no cover - defensive
            fmt.notes.append(f"Could not read Stata header metadata: {exc}")
    elif ext in (".xlsx", ".xls"):
        fmt.container = "excel"
        fmt.header_rows = 1
    elif ext == ".parquet":
        fmt.container = "parquet"
    else:
        raise ValueError(
            f"Unsupported file type {ext!r}. Supported: .csv .tsv .txt .xlsx .xls .dta .parquet"
        )
    return fmt


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------
def read_data(path: str, fmt: SourceFormat) -> tuple[pd.DataFrame, dict[str, dict]]:
    """Read the file using the DETECTED format.

    Delimited input is read as raw text (no type coercion) so that parse
    problems are visible to the profiler instead of being silently absorbed by
    the reader. Value labels are returned for .dta input.
    """
    ext = os.path.splitext(path)[1].lower()
    value_labels: dict[str, dict] = {}

    if ext in (".csv", ".tsv", ".txt"):
        delim = "\t" if "tab" in fmt.delimiter else fmt.delimiter.split("(")[-1].strip(")' ")
        if delim == "\\t":
            delim = "\t"
        df = pd.read_csv(
            path,
            sep=delim,
            encoding=fmt.encoding,
            dtype=str,
            keep_default_na=False,
            na_filter=False,
            skiprows=fmt.preamble_rows_skipped or None,
            engine="python",
        )
        df.columns = [str(c).strip().lstrip("﻿") for c in df.columns]
        return df, value_labels

    if ext == ".dta":
        with pd.io.stata.StataReader(path) as rdr:
            df = rdr.read()
            try:
                value_labels = rdr.value_labels()
            except Exception:
                value_labels = {}
            try:
                lbls = rdr.variable_labels()
            except Exception:
                lbls = {}
        df.attrs["variable_labels"] = lbls
        df.attrs["value_label_map"] = getattr(rdr, "lbllist", None)
        return df, value_labels

    if ext in (".xlsx", ".xls"):
        return pd.read_excel(path), value_labels
    if ext == ".parquet":
        return pd.read_parquet(path), value_labels
    raise ValueError(f"Unsupported file type {ext!r}")


# ---------------------------------------------------------------------------
# D + E. Text-column diagnostics and missing-kind breakdown
# ---------------------------------------------------------------------------
def _strip_wrappers(t: str) -> tuple[str, bool]:
    neg = False
    t = t.strip()
    if t.startswith("(") and t.endswith(")"):
        neg, t = True, t[1:-1].strip()
    if t.endswith("-"):
        neg, t = True, t[:-1].strip()
    return t, neg


def _infer_separators(values: list[str]) -> tuple[str, str]:
    """Vote on the decimal and thousands separators."""
    dec = Counter()
    for v in values[:2000]:
        t = v.strip()
        has_c, has_d = "," in t, "." in t
        if has_c and has_d:
            dec["," if t.rfind(",") > t.rfind(".") else "."] += 1
        elif has_c:
            tail = t.rsplit(",", 1)[-1]
            dec["," if (tail.isdigit() and len(tail) != 3) else "thou_c"] += 1
        elif has_d:
            tail = t.rsplit(".", 1)[-1]
            dec["." if (tail.isdigit() and len(tail) != 3) else "thou_d"] += 1
    if not dec:
        return ".", ""
    top = dec.most_common(1)[0][0]
    if top == ",":
        return ",", "space or ."
    if top == ".":
        return ".", "space or ,"
    if top == "thou_c":
        return ".", ","
    return ",", "."


def lenient_float(t: str, dec_sep: str) -> float | None:
    t, neg = _strip_wrappers(t)
    for ch in _CURRENCY_CHARS:
        t = t.replace(ch, "")
    for ch in _SPACE_CHARS:
        t = t.replace(ch, "")
    if dec_sep == ",":
        t = t.replace(".", "").replace(",", ".")
    else:
        t = t.replace(",", "")
    t = t.replace("+", "")
    if t in ("", "-", ".", "%"):
        return None
    t = t.rstrip("%")
    try:
        v = float(t)
    except ValueError:
        return None
    return -v if neg else v


def _strict_float(t: str) -> float | None:
    try:
        return float(t.strip())
    except (ValueError, AttributeError):
        return None


def analyse_text(values: pd.Series, cfg: Config) -> TextDiagnostics:
    """Report, for a text column, whether it really holds numbers and what
    would block a naive import. Counts and patterns only -- never values."""
    d = TextDiagnostics()
    s = values.astype(str)
    nonempty = s[s.str.strip() != ""]
    d.n_nonempty = int(len(nonempty))
    if d.n_nonempty == 0:
        return d

    vals = nonempty.tolist()
    # placeholders are "missing", not parse failures -- exclude from the base
    real = [v for v in vals if v.strip().lower() not in PLACEHOLDER_TOKENS]
    base = len(real) or 1

    dec, thou = _infer_separators(real)
    d.decimal_separator, d.thousands_separator = dec, thou

    n_strict = sum(1 for v in real if _strict_float(v) is not None)
    n_lenient = sum(1 for v in real if lenient_float(v, dec) is not None)
    d.share_numeric_strict = round(n_strict / base, 3)
    d.share_numeric_lenient = round(n_lenient / base, 3)

    # what would break a plain destring / import
    pat = {
        "contains a comma": lambda v: "," in v,
        "contains a space": lambda v: any(c in v for c in _SPACE_CHARS),
        "contains a currency symbol": lambda v: any(c in v for c in _CURRENCY_CHARS),
        "trailing minus": lambda v: v.strip().endswith("-"),
        "parenthesised negative": lambda v: v.strip().startswith("(") and v.strip().endswith(")"),
    }
    for name, fn in pat.items():
        c = sum(1 for v in real if fn(v))
        if c:
            d.blockers[name] = safe_share(c, base, cfg)
    n_placeholder = len(vals) - len(real)
    if n_placeholder:
        d.blockers["placeholder token (NULL/NA/-/.)"] = safe_share(n_placeholder, len(vals), cfg)

    lz = sum(1 for v in real if re.fullmatch(r"0\d+", v.strip()))
    lz_share = safe_share(lz, base, cfg)
    d.share_leading_zeros = lz_share if isinstance(lz_share, float) else 0.0

    d.numeric_as_text = (
        d.share_numeric_lenient >= cfg.numeric_as_text_threshold
        and d.share_numeric_strict < cfg.strict_parse_ok
    )
    if d.numeric_as_text:
        ignore = []
        if "contains a space" in d.blockers:
            ignore.append(" ")
        if dec == "," and "contains a comma" in d.blockers:
            pass  # handled by dpcomma
        elif "contains a comma" in d.blockers:
            ignore.append(",")
        opts = ""
        if ignore:
            opts += ' ignore("' + "".join(ignore) + '")'
        if dec == ",":
            opts += " dpcomma"
        d.destring_hint = f"destring VARNAME, replace{opts}"
    return d


def missing_breakdown(values: pd.Series, cfg: Config) -> MissingBreakdown:
    m = MissingBreakdown()
    isna = values.isna()
    m.true_null = safe_count(int(isna.sum()), cfg)
    if is_text_dtype(values):
        s = values.fillna("").astype(str)
        empty = int((s == "").sum())
        ws = int(((s.str.strip() == "") & (s != "")).sum())
        low = s.str.strip().str.lower()
        ph_mask = low.isin(PLACEHOLDER_TOKENS) & (s.str.strip() != "")
        m.empty_string = safe_count(empty, cfg)
        m.whitespace_only = safe_count(ws, cfg)
        m.placeholder = safe_count(int(ph_mask.sum()), cfg)
        # SPEC sec.4 rule 4: a placeholder token is a category value. List only
        # those whose own count clears the threshold, else we would disclose a
        # rare value whose count we just suppressed.
        tok_counts = low[ph_mask].value_counts()
        m.placeholder_tokens_seen = [
            str(t) for t, c in tok_counts.items() if c >= cfg.min_cell_count
        ][: cfg.max_categories]
        n_rare = int((tok_counts < cfg.min_cell_count).sum())
        if n_rare:
            m.placeholder_tokens_seen.append(f"<{n_rare} rare token(s) suppressed>")
    return m


def _blank_mask(values: pd.Series) -> pd.Series:
    """True where the cell carries no information: null, empty, whitespace, or
    a placeholder token."""
    if not is_text_dtype(values):
        return values.isna()
    s = values.fillna("").astype(str).str.strip().str.lower()
    return values.isna() | (s == "") | s.isin(PLACEHOLDER_TOKENS)


# ---------------------------------------------------------------------------
# Column classification and profiling
# ---------------------------------------------------------------------------
def _stata_storage_name(series: pd.Series) -> str:
    dt = series.dtype
    if pd.api.types.is_bool_dtype(dt):
        return "byte (boolean)"
    if pd.api.types.is_datetime64_any_dtype(dt):
        return "double (%td date)"
    if pd.api.types.is_integer_dtype(dt):
        return {1: "byte", 2: "int", 4: "long"}.get(dt.itemsize, "long")
    if pd.api.types.is_float_dtype(dt):
        return "float" if dt.itemsize == 4 else "double"
    if is_text_dtype(series):
        try:
            w = int(series.astype(str).str.len().max() or 0)
        except Exception:
            w = 0
        return f"str{max(w, 1)}" if w <= 2045 else "strL"
    return str(dt)


def _classify(series: pd.Series, n_obs: int, cfg: Config, numeric_as_text: bool) -> str:
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
    if pd.api.types.is_numeric_dtype(series) or numeric_as_text:
        nonnull = series.dropna()
        n_distinct = int(nonnull.nunique())
        if pd.api.types.is_numeric_dtype(series):
            arr = nonnull.to_numpy(dtype="float64")
            is_int_like = bool(np.all(np.equal(np.mod(arr, 1), 0)))
        else:
            is_int_like = False
        if is_int_like and (
            n_distinct == n_obs or n_distinct / n_obs > cfg.id_like_unique_ratio
        ):
            return "identifier"
        return "numeric"
    n_distinct = series.nunique(dropna=True)
    if n_distinct <= cfg.max_categories:
        return "categorical"
    if n_distinct >= cfg.id_like_unique_ratio * n_obs:
        return "identifier"
    return "high_cardinality"


def profile_column(
    series: pd.Series,
    cfg: Config,
    label: str = "",
    value_labels: dict | None = None,
) -> ColumnReport:
    n_total = len(series)
    raw = series
    blanks = _blank_mask(series)
    n_obs = int((~blanks).sum())
    n_missing = int(blanks.sum())
    pct_missing = round(100.0 * n_missing / n_total, 1) if n_total else 0.0

    text_diag: TextDiagnostics | None = None
    working = series
    if is_text_dtype(series):
        text_diag = analyse_text(series[~series.isna()], cfg)
        if text_diag.numeric_as_text or text_diag.share_numeric_strict >= cfg.strict_parse_ok:
            dec = text_diag.decimal_separator
            conv = series.where(~blanks).map(
                lambda v: lenient_float(str(v), dec) if isinstance(v, str) else v
            )
            working = pd.to_numeric(conv, errors="coerce")
            working.name = series.name

    kind = _classify(
        working, n_obs, cfg,
        numeric_as_text=bool(text_diag and text_diag.numeric_as_text),
    )
    rep = ColumnReport(
        name=str(series.name),
        kind=kind,
        storage=_stata_storage_name(raw),
        storage_dtype=str(raw.dtype),
        n_obs=n_obs,
        n_missing=n_missing,
        pct_missing=pct_missing,
        label=label,
        text=text_diag,
        missing_kinds=missing_breakdown(raw, cfg),
    )

    if text_diag and text_diag.numeric_as_text:
        rep.warnings.append(
            "NUMERIC-AS-TEXT -- will need destring. "
            f"{text_diag.share_numeric_lenient:.0%} of non-placeholder values are numeric "
            f"but only {text_diag.share_numeric_strict:.0%} parse without cleaning."
        )
    if text_diag and text_diag.share_leading_zeros > 0:
        rep.warnings.append(
            f"LEADING ZEROS on {text_diag.share_leading_zeros}% of values -- import as "
            "string or they will be destroyed (breaks merges silently)."
        )

    if value_labels:
        if len(value_labels) <= cfg.max_categories:
            rep.value_labels = {str(k): str(v) for k, v in value_labels.items()}
        else:
            rep.notes.append(
                f"{len(value_labels)} value labels attached -- too many to list under the "
                f"category limit ({cfg.max_categories}); request the code list separately."
            )

    if kind == "empty":
        rep.notes.append("Column is entirely missing.")
        return rep

    nonnull = working[~blanks] if is_text_dtype(working) else working.dropna()
    if kind == "numeric":
        _profile_numeric(nonnull.astype("float64"), rep, cfg)
    elif kind == "boolean":
        _profile_boolean(nonnull, rep, cfg)
    elif kind == "datetime":
        _profile_datetime(nonnull, rep, cfg)
    elif kind == "categorical":
        _profile_categorical(raw[~blanks], rep, cfg)
    elif kind == "high_cardinality":
        _profile_high_cardinality(raw[~blanks], rep, cfg)
    elif kind == "identifier":
        _profile_identifier(raw[~blanks], rep, cfg)
    return rep


def _profile_numeric(s: pd.Series, rep: ColumnReport, cfg: Config) -> None:
    arr = s.to_numpy(dtype="float64")
    arr = arr[~np.isnan(arr)]
    if arr.size == 0:
        rep.notes.append("No parseable numeric values.")
        return
    sig = cfg.round_significant
    is_int_like = bool(np.all(np.equal(np.mod(arr, 1), 0)))

    rep.facts["integer_valued"] = is_int_like
    rep.facts["n_zero"] = safe_count(int(np.sum(arr == 0)), cfg)
    rep.facts["n_negative"] = safe_count(int(np.sum(arr < 0)), cfg)
    rep.facts["n_positive"] = safe_count(int(np.sum(arr > 0)), cfg)

    # Integer column with few distinct values is almost certainly a coded
    # category (sex, region, filing status, year...). Researchers need the set
    # of valid codes and their frequencies, not a mean. Report both.
    n_distinct = int(pd.Series(arr).nunique())
    if is_int_like and n_distinct <= cfg.max_categories:
        rep.facts["looks_coded_categorical"] = True
        _add_category_counts(pd.Series(arr.astype("int64")), rep, cfg)

    if rep.n_obs < cfg.min_cell_count:
        rep.notes.append(
            "Too few observations to release summary statistics; suppressed."
        )
        return

    mean = float(np.mean(arr))
    rep.facts["mean"] = round_sig(mean, sig)
    rep.facts["sd"] = round_sig(float(np.std(arr, ddof=1)) if arr.size > 1 else 0.0, sig)
    qs = {
        "p1": 1, "p5": 5, "p10": 10, "p25": 25, "p50": 50,
        "p75": 75, "p90": 90, "p95": 95, "p99": 99,
    }
    quant = {k: round_sig(float(np.percentile(arr, q)), sig) for k, q in qs.items()}
    rep.facts["quantiles_rounded"] = quant
    rep.facts["approx_range_rounded"] = (quant["p1"], quant["p99"])
    rep.notes.append(
        "Exact min/max withheld; range shown is rounded p1-p99. "
        f"All numeric summaries rounded to {sig} significant figures."
    )

    _flag_contamination(arr, mean, rep, cfg)

    # Advisory: an integer column with uniformly huge values and no zeros or
    # negatives is very likely an identifier (e.g. a taxpayer ID) that slipped
    # the name test, not a measure. Flag it for the reviewer; do not reclassify.
    if (
        is_int_like
        and np.sum(arr == 0) == 0
        and np.sum(arr < 0) == 0
        and float(np.min(arr)) >= cfg.id_large_int_threshold
    ):
        rep.warnings.append(
            "SUSPECT: integer column with uniformly large values and no zeros or "
            "negatives -- verify this is a genuine measure and not an identifier "
            "that should be withheld."
        )


def _flag_contamination(arr, mean: float, rep: ColumnReport, cfg: Config) -> None:
    """G. Flag distributions that look contaminated by unit errors or corrupt
    rows. Counts and ratios only -- never the offending values."""
    median = float(np.percentile(arr, 50))
    p99 = float(np.percentile(arr, 99))
    q1, q3 = float(np.percentile(arr, 25)), float(np.percentile(arr, 75))
    iqr = q3 - q1

    if p99 > 0 and mean > p99 * cfg.suspect_mean_p99_ratio:
        rep.warnings.append(
            f"SUSPECT: mean is {mean / p99:.0f}x p99 -- likely unit errors or corrupt rows."
        )
    elif median > 0 and mean > median * cfg.suspect_mean_median_ratio:
        rep.warnings.append(
            f"SUSPECT: mean is {mean / median:.0f}x the median -- likely unit errors "
            "or corrupt rows."
        )
    elif p99 == 0 and mean > 0:
        rep.notes.append(
            "Extreme concentration: p99 is zero while the mean is positive -- almost "
            "all rows are zero and the total is driven by very few records."
        )

    if iqr > 0:
        hi = q3 + cfg.iqr_outlier_multiple * iqr
        lo = q1 - cfg.iqr_outlier_multiple * iqr
        n_out = int(np.sum((arr > hi) | (arr < lo)))
        if n_out:
            rep.facts["extreme_outliers"] = {
                "beyond_iqr_multiple": cfg.iqr_outlier_multiple,
                "count": safe_count(n_out, cfg),
                "share_pct": safe_share(n_out, arr.size, cfg),
            }
            rep.warnings.append(
                f"SUSPECT: {safe_count(n_out, cfg)} value(s) beyond "
                f"{cfg.iqr_outlier_multiple:g}x the interquartile range -- inspect for "
                "unit errors before using this column."
            )


def _profile_boolean(s: pd.Series, rep: ColumnReport, cfg: Config) -> None:
    n_true = int(s.sum())
    rep.facts["n_true"] = safe_count(n_true, cfg)
    rep.facts["n_false"] = safe_count(int(rep.n_obs - n_true), cfg)


def _profile_datetime(s: pd.Series, rep: ColumnReport, cfg: Config) -> None:
    years = s.dt.year
    rep.facts["n_distinct_dates"] = int(s.nunique())
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
# F. Structure: what is a row?
# ---------------------------------------------------------------------------
def _key_candidates(df: pd.DataFrame, cfg: Config, nominated: list[str]) -> list[str]:
    if nominated:
        return [c for c in nominated if c in df.columns]
    out = []
    for c in df.columns:
        toks = set(re.split(r"[^a-z0-9]+", str(c).lower()))
        if toks & set(KEY_NAME_HINTS) or looks_like_identifier_name(str(c), cfg):
            out.append(c)
    return out[:8]


def analyse_structure(
    df: pd.DataFrame, cfg: Config, nominated: list[str] | None = None
) -> StructureReport:
    st = StructureReport(n_rows=int(df.shape[0]), n_cols=int(df.shape[1]))
    cands = _key_candidates(df, cfg, nominated or [])
    st.candidate_key_columns = [str(c) for c in cands]
    n = len(df)
    if n == 0 or not cands:
        st.grain_note = "No candidate key columns identified; row grain not determined."
        return st

    tested = 0
    for c in cands:
        tested += 1
        if int(df[c].nunique(dropna=False)) == n:
            st.unique_keys.append([str(c)])
    if not st.unique_keys:
        for a, b in combinations(cands, 2):
            tested += 1
            if int(df[[a, b]].drop_duplicates().shape[0]) == n:
                st.unique_keys.append([str(a), str(b)])
    st.tested_combinations = tested

    if st.unique_keys:
        st.chosen_key = st.unique_keys[0]
        st.key_is_unique = True
        st.grain_note = (
            "One row per " + " x ".join(st.chosen_key) + " (verified unique)."
        )
        return st

    # No unique combination: describe the duplication so the grain is obvious.
    key = (nominated or [])[:2] or cands[:2]
    key = [c for c in key if c in df.columns]
    if not key:
        return st
    st.chosen_key = [str(c) for c in key]
    sizes = df.groupby(key, dropna=False).size()
    once = int((sizes == 1).sum())
    twice = int((sizes == 2).sum())
    more = int((sizes >= 3).sum())
    st.dup_profile = {
        "n_distinct_keys": int(sizes.shape[0]),
        "keys_appearing_once": safe_count(once, cfg),
        "keys_appearing_twice": safe_count(twice, cfg),
        "keys_appearing_3plus": safe_count(more, cfg),
        "rows_per_key_p50": round_sig(float(sizes.quantile(0.50)), cfg.round_significant),
        "rows_per_key_p99": round_sig(float(sizes.quantile(0.99)), cfg.round_significant),
    }
    dup_keys = sizes[sizes > 1]
    if len(dup_keys) > 0:
        sample_keys = dup_keys.index[:200]
        sub = df.set_index(key).loc[sample_keys]
        varying, constant = [], []
        for c in df.columns:
            if c in key:
                continue
            try:
                nun = sub.groupby(level=list(range(len(key))))[c].nunique(dropna=False)
            except Exception:
                continue
            (varying if bool((nun > 1).any()) else constant).append(str(c))
        st.varying_within_key = varying
        st.constant_within_key = constant
        st.grain_note = (
            "NOT one row per " + " x ".join(st.chosen_key) + ". The file is LONG over "
            "a sub-entity: within a repeated key the columns listed under "
            "'varies within key' change, while the rest are constant. Aggregate or "
            "de-duplicate before treating this as a firm-year panel."
        )
    return st


# ---------------------------------------------------------------------------
# B. Ready-made read commands
# ---------------------------------------------------------------------------
_STATA_ENC = {
    "utf-8": "utf-8", "utf-8-sig": "utf-8",
    "utf-16-le": "utf-16", "utf-16-be": "utf-16", "utf-16": "utf-16",
    "cp1250": "windows-1250", "cp1252": "windows-1252", "latin-1": "latin1",
}


def build_read_commands(
    fmt: SourceFormat, columns: list[ColumnReport], cfg: Config
) -> dict[str, str]:
    cmds: dict[str, str] = {}
    path = fmt.path
    if fmt.container == "delimited text":
        delim = "\t" if "tab" in fmt.delimiter else fmt.delimiter.split("(")[-1].strip(")' ")
        names = [c.name for c in columns]
        force_str = [
            i + 1 for i, c in enumerate(columns)
            if (c.text and (c.text.numeric_as_text or c.text.share_leading_zeros > 0))
            or c.kind == "identifier"
        ]
        enc = _STATA_ENC.get(fmt.encoding, fmt.encoding)
        parts = [
            f'import delimited "{path}", clear varnames(1) case(lower)',
            f'    delimiter("{"tab" if delim == chr(9) else delim}") encoding("{enc}")',
        ]
        if force_str:
            parts[-1] += f' stringcols({" ".join(str(i) for i in force_str)})'
        stata = " ///\n".join(parts)
        destrings = [
            f"destring {c.name}, replace"
            + (
                ' ignore(" ")'
                if c.text and "contains a space" in (c.text.blockers or {})
                else ""
            )
            + (" dpcomma" if c.text and c.text.decimal_separator == "," else "")
            for c in columns
            if c.text and c.text.numeric_as_text
        ]
        if destrings:
            stata += (
                "\n\n* numeric-as-text columns detected -- convert after import:\n"
                + "\n".join(destrings)
            )
        cmds["stata"] = stata

        dtype_note = ""
        if force_str:
            keys = ", ".join(f'"{names[i - 1]}": str' for i in force_str)
            dtype_note = f",\n    dtype={{{keys}}}"
        cmds["pandas"] = (
            f'df = pd.read_csv(\n    r"{path}",\n    sep={delim!r},\n'
            f'    encoding="{fmt.encoding}"{dtype_note},\n)'
        )
    elif fmt.container == "stata":
        cmds["stata"] = f'use "{path}", clear'
        cmds["pandas"] = f'df = pd.read_stata(r"{path}", convert_categoricals=False)'
    elif fmt.container == "excel":
        cmds["stata"] = f'import excel "{path}", firstrow clear'
        cmds["pandas"] = f'df = pd.read_excel(r"{path}")'
    elif fmt.container == "parquet":
        cmds["pandas"] = f'df = pd.read_parquet(r"{path}")'
    return cmds


# ---------------------------------------------------------------------------
# H. Code lists to request
# ---------------------------------------------------------------------------
def _is_self_explanatory(c: ColumnReport) -> bool:
    """Years, periods and single-valued columns need no code list -- asking for
    one just buries the columns that genuinely do."""
    toks = set(re.split(r"[^a-z0-9]+", c.name.lower()))
    if toks & {"year", "rok", "yr", "period", "quarter", "month"}:
        return True
    cats = c.facts.get("categories") or {}
    if cats:
        try:
            vals = [float(v) for v in cats]
            if all(1900 <= v <= 2100 and v == int(v) for v in vals):
                return True             # a span of calendar years
        except (TypeError, ValueError):
            pass
    return (c.facts.get("n_distinct") or 0) <= 1


def build_code_list_requests(columns: list[ColumnReport], cfg: Config) -> list[dict]:
    out = []
    for c in columns:
        if c.value_labels:
            continue                      # already documented in the file
        if _is_self_explanatory(c):
            continue
        n_distinct = c.facts.get("n_distinct")
        coded = bool(c.facts.get("looks_coded_categorical"))
        if c.kind == "categorical" or coded:
            out.append({
                "column": c.name,
                "label": c.label,
                "n_distinct": n_distinct,
                "reason": "few distinct values, no value labels attached",
            })
        elif c.kind == "high_cardinality":
            out.append({
                "column": c.name,
                "label": c.label,
                "n_distinct": n_distinct,
                "reason": "looks like a code list but has too many values to publish",
            })
    return out


# ---------------------------------------------------------------------------
# I. Cross-file consistency
# ---------------------------------------------------------------------------
def _type_signature(rep: ColumnReport, cfg: Config, container: str) -> str:
    """The type another program will end up with.

    A delimited file carries no types, so storage there is always text and
    comparing it across files would be vacuous; what matters is the type a
    reader will INFER. A .dta carries real storage types, so compare those --
    that is the drift that silently breaks appends and merges.
    """
    if rep.kind in ("datetime", "boolean", "empty"):
        return rep.kind
    if container == "delimited text":
        if rep.text and rep.text.share_numeric_strict >= cfg.strict_parse_ok:
            return "numeric (inferred)"
        return "text (inferred)"
    return "text" if rep.storage.startswith("str") else "numeric"


def analyse_consistency(
    reports: list[DatasetReport], cfg: Config, nominated: list[str]
) -> ConsistencyReport:
    con = ConsistencyReport(files=[r.source_format.file_name if r.source_format else r.source
                                   for r in reports])
    colsets = {}
    containers = {}
    for r in reports:
        fname = r.source_format.file_name if r.source_format else r.source
        colsets[fname] = {c.name: c for c in r.columns}
        containers[fname] = r.source_format.container if r.source_format else ""

    all_cols: set[str] = set()
    for cols in colsets.values():
        all_cols |= set(cols)
    for col in sorted(all_cols):
        present = [f for f, cols in colsets.items() if col in cols]
        if len(present) == len(colsets):
            con.columns_everywhere.append(col)
        else:
            con.columns_partial[col] = present
        sigs = {f: _type_signature(colsets[f][col], cfg, containers.get(f, ""))
                for f in present}
        if len({s.split(" ")[0] for s in sigs.values()}) > 1:
            con.storage_conflicts[col] = {
                f: f"{sigs[f]}  [stored as {colsets[f][col].storage}]" for f in present
            }
        # leading zeros in one file but not another silently breaks string merges
        lz = {f: (colsets[f][col].text.share_leading_zeros if colsets[f][col].text else 0.0)
              for f in present}
        if len({bool(v) for v in lz.values()}) > 1:
            con.storage_conflicts.setdefault(col, {}).update(
                {f"{f} (leading zeros)": f"{v}% of values" for f, v in lz.items()}
            )

    for col in nominated or []:
        present = [f for f, cols in colsets.items() if col in cols]
        if len(present) < 2:
            continue
        for a, b in combinations(present, 2):
            va = colsets[a][col].facts.get("_key_values")
            vb = colsets[b][col].facts.get("_key_values")
            if va is None or vb is None:
                continue
            inter = len(va & vb)
            con.key_overlap.append({
                "column": col,
                "file_a": a, "file_b": b,
                "distinct_a": len(va), "distinct_b": len(vb),
                "in_both": safe_count(inter, cfg),
                "pct_of_a": safe_share(inter, len(va), cfg) if va else 0.0,
                "pct_of_b": safe_share(inter, len(vb), cfg) if vb else 0.0,
            })
    return con


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------
def profile_dataframe(
    df: pd.DataFrame,
    label: str,
    source: str,
    cfg: Config | None = None,
    source_format: SourceFormat | None = None,
    value_labels: dict[str, dict] | None = None,
    nominated_key: list[str] | None = None,
) -> DatasetReport:
    cfg = cfg or Config()
    report = DatasetReport(
        label=label,
        source=source,
        n_rows=int(df.shape[0]),
        n_cols=int(df.shape[1]),
        rows_in_file=int(df.shape[0]),
        config=cfg,
        source_format=source_format,
    )
    var_labels = df.attrs.get("variable_labels", {}) if hasattr(df, "attrs") else {}
    vl = value_labels or {}
    for col in df.columns:
        report.columns.append(
            profile_column(
                df[col], cfg,
                label=str(var_labels.get(col, "") or ""),
                value_labels=vl.get(col),
            )
        )
    report.structure = analyse_structure(df, cfg, nominated_key)
    if source_format and source_format.container == "delimited text":
        # A delimited file carries no types. Saying "storage: str9" alone invites
        # the reader to think Stata will import it as a string, so state what a
        # reader will actually infer.
        for c in report.columns:
            c.facts["reader_will_infer"] = _type_signature(
                c, cfg, "delimited text"
            ).replace(" (inferred)", "")
    if source_format:
        report.read_commands = build_read_commands(source_format, report.columns, cfg)
    report.code_lists_to_request = build_code_list_requests(report.columns, cfg)
    return report


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def _fmt(value: Any) -> str:
    if value is None:
        return "n/a"
    return str(value)


def _sample_tag(report: DatasetReport) -> str:
    if not report.sampled:
        return ""
    return f"   [SAMPLE {report.sample_fraction * 100:.2f}% -- counts are sample counts]"


def render_log(report: DatasetReport) -> str:
    cfg = report.config
    lines: list[str] = []
    w = lines.append
    bar = "=" * 76
    dash = "-" * 76

    def section(title: str) -> None:
        w(dash)
        w(title + _sample_tag(report))
        w(dash)

    w(bar)
    w("AD4TJ DISCLOSURE-SAFE CODEBOOK")
    w(bar)
    w(f"Dataset      : {report.label}")
    w(f"Source file  : {report.source}")
    w(f"Rows         : {report.rows_in_file}")
    if report.sampled:
        w(f"Rows profiled: {report.n_rows}  (random sample, "
          f"{report.sample_fraction * 100:.2f}% of the file)")
    w(f"Columns      : {report.n_cols}")
    w("")
    w("Disclosure-control rules applied to this log:")
    w(f"  - counts below {cfg.min_cell_count} are suppressed")
    w(f"  - numeric summaries rounded to {cfg.round_significant} significant figures")
    w("  - exact minimum and maximum values are NEVER released")
    w(f"  - category values listed only when distinct count <= {cfg.max_categories}")
    w("  - identifier / high-cardinality column values are not listed")
    w("  - columns whose NAME matches an identifier pattern are withheld entirely")
    w("  - the source header line is printed; NO data row is ever printed")
    w("")
    w("This file contains NO individual-level data points. The data lab must")
    w("still review it against local disclosure policy before release.")
    w("This tool applies frequency-style controls only; it does NOT implement the")
    w("dominance / p-percent rule for magnitude data -- see SPEC.md section 4.1.")
    w(bar)
    w("")

    if report.source_format:
        _render_source_format(report, section, w)
    if report.read_commands:
        _render_read_commands(report, section, w)
    if report.structure:
        _render_structure(report, section, w)

    section("VARIABLES")
    w("")
    for c in report.columns:
        w(dash)
        w(f"VARIABLE: {c.name}")
        if c.label:
            w(f"  label           : {c.label}")
        w(f"  kind            : {c.kind}")
        w(f"  storage         : {c.storage}   (source dtype: {c.storage_dtype})")
        if c.facts.get("reader_will_infer"):
            w(f"  reader infers   : {c.facts['reader_will_infer']}  "
              "(delimited files carry no types)")
        w(f"  non-missing     : {c.n_obs}")
        w(f"  missing         : {c.n_missing} ({c.pct_missing}%)")
        _render_missing_kinds(c, w)
        _render_text_diag(c, w)
        _render_facts(c, w)
        for warn in c.warnings:
            w(f"  ** {warn}")
        if c.value_labels:
            w("  value labels    :")
            for k, v in c.value_labels.items():
                w(f"      {k!r:>20} = {v}")
        for note in c.notes:
            w(f"  note            : {note}")
    w(dash)
    w("")

    _render_code_lists(report, section, w)

    w("END OF CODEBOOK")
    return "\n".join(lines) + "\n"


def _render_source_format(report: DatasetReport, section, w) -> None:
    f = report.source_format
    section("SOURCE FORMAT  -- how this file was actually read")
    w(f"  file            : {f.file_name}")
    w(f"  size            : {f.size_bytes:,} bytes")
    w(f"  modified        : {f.modified_utc}")
    w(f"  sha256          : {f.sha256}")
    w(f"  container       : {f.container}")
    if f.container == "delimited text":
        w(f"  encoding        : {f.encoding}   ({f.encoding_confidence})")
        w(f"  byte-order mark : {f.bom}")
        w(f"  delimiter       : {f.delimiter}")
        w(f"  quote char      : {f.quote_char!r}")
        w(f"  escape char     : {f.escape_char}")
        w(f"  line terminator : {f.line_terminator}")
        w(f"  header rows     : {f.header_rows}")
        w(f"  preamble skipped: {f.preamble_rows_skipped}")
        w(f"  columns parsed  : {f.n_columns_in_header}")
        if f.header_line:
            w(f"  header line     : {f.header_line}")
        else:
            w(f"  header line     : {NOT_SHOWN} ({f.header_line_withheld_reason})")
    if f.container == "stata":
        w(f"  stata version   : {f.stata_version}")
        w(f"  dataset label   : {f.dataset_label or '(none)'}")
    for n in f.notes:
        w(f"  note            : {n}")
    w("")


def _render_read_commands(report: DatasetReport, section, w) -> None:
    section("READ COMMAND  -- reproduces the read above")
    for lang in ("stata", "pandas"):
        if lang not in report.read_commands:
            continue
        w(f"  [{lang}]")
        for ln in report.read_commands[lang].splitlines():
            w(f"    {ln}")
        w("")
    if report.source_format and report.source_format.sha256:
        w("  Assert you have the same file before running the above:")
        w(f"    shasum -a 256 <file>   ->  {report.source_format.sha256}")
    w("")


def _render_structure(report: DatasetReport, section, w) -> None:
    st = report.structure
    section("STRUCTURE  -- what is a row?")
    w(f"  rows            : {st.n_rows}")
    w(f"  columns         : {st.n_cols}")
    w(f"  key candidates  : {', '.join(st.candidate_key_columns) or '(none identified)'}")
    w(f"  combinations tested: {st.tested_combinations}")
    if st.unique_keys:
        for k in st.unique_keys:
            w(f"  UNIQUE KEY      : ({', '.join(k)})")
    else:
        w("  UNIQUE KEY      : none found among the tested combinations")
    if st.dup_profile:
        d = st.dup_profile
        w(f"  duplication of ({', '.join(st.chosen_key)}):")
        w(f"      distinct keys        : {d['n_distinct_keys']}")
        w(f"      appearing once       : {_fmt(d['keys_appearing_once'])}")
        w(f"      appearing twice      : {_fmt(d['keys_appearing_twice'])}")
        w(f"      appearing 3+ times   : {_fmt(d['keys_appearing_3plus'])}")
        w(f"      rows per key p50/p99 : {_fmt(d['rows_per_key_p50'])} / "
          f"{_fmt(d['rows_per_key_p99'])}")
    if st.varying_within_key:
        w(f"  varies within key  : {', '.join(st.varying_within_key)}")
    if st.constant_within_key:
        w(f"  constant within key: {', '.join(st.constant_within_key)}")
    if st.grain_note:
        w("")
        for ln in _wrap(st.grain_note, 72):
            w(f"  {ln}")
    w("")


def _render_missing_kinds(c: ColumnReport, w) -> None:
    m = c.missing_kinds
    if not m:
        return
    parts = [f"null={_fmt(m.true_null)}"]
    if c.storage.startswith("str"):
        parts += [
            f"empty={_fmt(m.empty_string)}",
            f"whitespace={_fmt(m.whitespace_only)}",
            f"placeholder={_fmt(m.placeholder)}",
        ]
    w(f"  missing kinds   : {'  '.join(parts)}")
    if m.placeholder_tokens_seen:
        w(f"  placeholder toks: {', '.join(repr(t) for t in m.placeholder_tokens_seen)}")


def _render_text_diag(c: ColumnReport, w) -> None:
    d = c.text
    if not d or d.n_nonempty == 0:
        return
    w(f"  parses as number: {d.share_numeric_lenient:.0%} after cleaning, "
      f"{d.share_numeric_strict:.0%} as-is")
    if d.share_numeric_lenient > 0:
        w(f"  decimal sep     : {d.decimal_separator!r}   thousands sep: "
          f"{d.thousands_separator or '(none)'}")
    if d.blockers:
        w("  parse blockers  :")
        for k, v in d.blockers.items():
            w(f"      {k:<34}: {_fmt(v)}% of values")
    if d.destring_hint:
        w(f"  suggested fix   : {d.destring_hint.replace('VARNAME', c.name)}")


def _render_facts(c: ColumnReport, w) -> None:
    f = c.facts
    if c.kind == "numeric":
        w(f"  integer-valued  : {_fmt(f.get('integer_valued'))}")
        w(f"  # zero          : {_fmt(f.get('n_zero'))}")
        w(f"  # negative      : {_fmt(f.get('n_negative'))}")
        w(f"  # positive      : {_fmt(f.get('n_positive'))}")
        if f.get("looks_coded_categorical"):
            w("  looks coded     : yes (few distinct integer values - likely a code list)")
            w(f"  distinct values : {_fmt(f.get('n_distinct'))}")
            _render_categories(f, w)
        if "mean" in f:
            w(f"  mean (rounded)  : {_fmt(f.get('mean'))}")
            w(f"  sd (rounded)    : {_fmt(f.get('sd'))}")
            rng = f.get("approx_range_rounded")
            if rng:
                w(f"  approx range    : {_fmt(rng[0])} .. {_fmt(rng[1])}  (rounded p1-p99)")
            q = f.get("quantiles_rounded", {})
            if q:
                w("  quantiles       : " + "  ".join(f"{k}={_fmt(v)}" for k, v in q.items()))
        out = f.get("extreme_outliers")
        if out:
            w(f"  extreme outliers: {_fmt(out['count'])} "
              f"({_fmt(out['share_pct'])}%) beyond {out['beyond_iqr_multiple']:g}x IQR")
    elif c.kind == "boolean":
        w(f"  # true          : {_fmt(f.get('n_true'))}")
        w(f"  # false         : {_fmt(f.get('n_false'))}")
    elif c.kind == "datetime":
        w(f"  distinct dates  : {_fmt(f.get('n_distinct_dates'))}")
        w(f"  year span       : {_fmt(f.get('year_min'))} .. {_fmt(f.get('year_max'))}")
    elif c.kind == "categorical":
        w(f"  distinct values : {_fmt(f.get('n_distinct'))}")
        _render_categories(f, w)
    elif c.kind in ("high_cardinality", "identifier"):
        w(f"  distinct values : {_fmt(f.get('n_distinct'))}")
        if c.kind == "identifier":
            w(f"  unique per row  : {_fmt(f.get('looks_unique'))}")


def _render_categories(f: dict, w) -> None:
    cats = f.get("categories", {})
    if cats:
        w("  value counts    :")
        for value, cnt in cats.items():
            w(f"      {value!r:>20} : {cnt}")
    sup = f.get("suppressed_categories")
    if sup:
        w(f"      <{sup['n_categories']} rare value(s)> : combined "
          f"{_fmt(sup['combined_count'])}")


def _render_code_lists(report: DatasetReport, section, w) -> None:
    reqs = report.code_lists_to_request
    section("CODE LISTS TO REQUEST  -- send this list to the data owner")
    if not reqs:
        w("  (none: every coded column already carries value labels)")
        w("")
        return
    w("  The following columns are coded but carry no value labels in the source.")
    w("  Their meaning cannot be inferred from the data. Please supply the code")
    w("  list (value -> meaning) for each:")
    w("")
    for r in reqs:
        lbl = f"  [{r['label']}]" if r.get("label") else ""
        w(f"    - {r['column']}{lbl}: {r['n_distinct']} distinct values "
          f"({r['reason']})")
    w("")


def render_consistency(con: ConsistencyReport, cfg: Config) -> str:
    lines: list[str] = []
    w = lines.append
    dash = "-" * 76
    w(dash)
    w("CONSISTENCY ACROSS FILES")
    w(dash)
    w(f"  files compared  : {len(con.files)}")
    for f in con.files:
        w(f"      - {f}")
    w("")
    w(f"  columns in every file : {len(con.columns_everywhere)}")
    if con.columns_partial:
        w("  columns NOT in every file:")
        for col, present in sorted(con.columns_partial.items()):
            w(f"      {col:<28} present in: {', '.join(present)}")
    w("")
    if con.storage_conflicts:
        w("  ** STORAGE TYPE CONFLICTS -- these fail SILENTLY on append/merge:")
        for col, per_file in sorted(con.storage_conflicts.items()):
            w(f"      {col}:")
            for f, s in per_file.items():
                w(f"          {f:<32} {s}")
        w("      A merge on a column that is numeric in one file and text in")
        w("      another matches ZERO rows without raising an error. Force a")
        w("      common type before appending.")
    else:
        w("  storage types agree across files for all shared columns.")
    w("")
    if con.key_overlap:
        w("  key-value overlap (predicts whether a merge will work):")
        for o in con.key_overlap:
            w(f"      {o['column']}: {o['file_a']} vs {o['file_b']}")
            w(f"          distinct in A / B : {o['distinct_a']} / {o['distinct_b']}")
            w(f"          in both           : {_fmt(o['in_both'])} "
              f"({_fmt(o['pct_of_a'])}% of A, {_fmt(o['pct_of_b'])}% of B)")
    w(dash)
    w("")
    return "\n".join(lines) + "\n"


def _wrap(text: str, width: int) -> list[str]:
    words, out, cur = text.split(), [], ""
    for word in words:
        if len(cur) + len(word) + 1 > width:
            out.append(cur)
            cur = word
        else:
            cur = f"{cur} {word}".strip()
    if cur:
        out.append(cur)
    return out


# ---------------------------------------------------------------------------
# K. Machine-readable companion
# ---------------------------------------------------------------------------
def report_to_dict(report: DatasetReport) -> dict:
    d = dataclasses.asdict(report)
    d["ad4tj_codebook_version"] = "1.1-draft"
    for col in d.get("columns", []):
        col.get("facts", {}).pop("_key_values", None)
    return d


def write_json(report: DatasetReport, path: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(report_to_dict(report), fh, indent=2, default=str, ensure_ascii=False)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------
def _derive_output_path(data_path: str, out_dir: str = "") -> str:
    base = os.path.splitext(data_path)[0]
    if out_dir:
        base = os.path.join(out_dir, os.path.basename(base))
    return base + ".codebook.log"


def profile_file(
    path: str, cfg: Config, nominated: list[str], keep_key_values: bool = False
) -> DatasetReport:
    fmt = detect_source_format(path, cfg)
    df, value_labels = read_data(path, fmt)
    rows_in_file = int(df.shape[0])
    sampled, frac = False, 1.0
    if SAMPLE_N and rows_in_file > SAMPLE_N:
        df = df.sample(n=SAMPLE_N, random_state=SEED)
        sampled, frac = True, SAMPLE_N / rows_in_file
    label = DATASET_LABEL or os.path.basename(path)
    rep = profile_dataframe(
        df, label=label, source=path, cfg=cfg, source_format=fmt,
        value_labels=value_labels, nominated_key=nominated,
    )
    rep.rows_in_file = rows_in_file
    rep.sampled, rep.sample_fraction = sampled, frac
    if keep_key_values:
        for c in rep.columns:
            if c.name in nominated:
                c.facts["_key_values"] = set(
                    df[c.name].dropna().astype(str).unique().tolist()
                )
    return rep


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="AD4TJ disclosure-safe profiler")
    ap.add_argument("files", nargs="*", help="data file(s); defaults to CONFIG DATA_PATH")
    ap.add_argument("--key", default=CANDIDATE_KEY,
                    help="candidate row key, comma separated (e.g. ic,rok)")
    ap.add_argument("--out", default="", help="output directory")
    args = ap.parse_args(argv)

    paths = args.files or [DATA_PATH]
    nominated = [k.strip() for k in args.key.split(",") if k.strip()]
    cfg = Config()

    missing = [p for p in paths if not os.path.exists(p)]
    if missing:
        sys.stderr.write(f"ERROR: data file not found: {missing[0]}\n")
        return 2

    reports = []
    for p in paths:
        rep = profile_file(p, cfg, nominated, keep_key_values=len(paths) > 1)
        text = render_log(rep)
        single = OUTPUT_PATH and len(paths) == 1
        out_path = OUTPUT_PATH if single else _derive_output_path(p, args.out)
        with open(out_path, "w", encoding="utf-8") as fh:
            fh.write(text)
        write_json(rep, os.path.splitext(out_path)[0] + ".json")
        sys.stderr.write(f"Codebook written to: {out_path}\n")
        reports.append(rep)

    if len(reports) > 1:
        con = analyse_consistency(reports, cfg, nominated)
        con_path = os.path.join(args.out or os.path.dirname(paths[0]) or ".",
                                "consistency.codebook.log")
        with open(con_path, "w", encoding="utf-8") as fh:
            fh.write(render_consistency(con, cfg))
        sys.stderr.write(f"Consistency report written to: {con_path}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
