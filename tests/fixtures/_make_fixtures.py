#!/usr/bin/env python3
"""
Synthetic fixtures reproducing each real-world failure the profiler missed.

EVERY VALUE HERE IS FABRICATED. No real taxpayer, firm or record is represented.
These exist so the test suite can assert the profiler now *reports* the things
that previously had to be discovered by hand-debugging analysis code.

Run:  python tests/fixtures/_make_fixtures.py
"""
from __future__ import annotations

import os

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
SEED = 20260707


def _p(name: str) -> str:
    return os.path.join(HERE, name)


def make_utf16_semicolon() -> None:
    """Failure 1 + 2 + 3: a semicolon-delimited UTF-16-LE file with a BOM, whose
    amount columns are text holding numbers with a decimal comma and space
    thousands separators, and whose 'missingness' is mostly empty strings."""
    rng = np.random.default_rng(SEED)
    n = 600

    ic = [f"{40000000 + i:08d}" for i in range(n)]          # company reg no, leading digits
    rok = rng.choice([2023], size=n)
    # annual net turnover held as TEXT with decimal comma + space thousands sep
    raw = rng.lognormal(mean=12.0, sigma=1.4, size=n).round(2)
    kc_dpp_i1 = [f"{v:,.2f}".replace(",", " ").replace(".", ",") for v in raw]
    # a handful of placeholder / awkward tokens that block a naive destring
    for i in rng.choice(n, size=18, replace=False):
        kc_dpp_i1[i] = rng.choice(["NULL", "N/A", "-", "(1 234,50)", "12 000,00-"])

    # a column that is 85% *empty string*, not 85% truly sparse
    kc_ii10_10 = ["" if rng.random() < 0.85 else f"{rng.integers(1, 900)}" for _ in range(n)]

    # multinational-status flag: coded, no code list anywhere
    spoj_zahr = rng.choice(["A", "N", "T", "Z"], size=n, p=[0.55, 0.30, 0.10, 0.05])

    # contaminated column: a few values a thousand times too large (failure 6)
    nbemployees = rng.integers(1, 400, size=n).astype(float)
    for i in rng.choice(n, size=4, replace=False):
        nbemployees[i] = nbemployees[i] * 1000.0 * rng.integers(500, 3000)

    df = pd.DataFrame({
        "ic": ic,
        "rok": rok,
        "kc_dpp_i1": kc_dpp_i1,
        "kc_ii10_10": kc_ii10_10,
        "spoj_zahr": spoj_zahr,
        "nbemployees": nbemployees.astype(int),
    })
    csv = df.to_csv(index=False, sep=";", lineterminator="\r\n")
    with open(_p("cz_returns_utf16.csv"), "wb") as fh:
        fh.write(csv.encode("utf-16"))       # utf-16 with BOM


def make_type_drift() -> None:
    """Failure 4: two vintages of one table whose key columns disagree on type.
    Appending these and merging on them would silently match zero rows."""
    rng = np.random.default_rng(SEED + 1)
    n = 400
    ids = np.arange(40000000, 40000000 + n)

    # 2018-2022 vintage: keys stored NUMERIC
    old = pd.DataFrame({
        "ic": ids,
        "c_ds": rng.integers(100, 999, size=n),
        "dicc_poplatnika": rng.integers(1000, 9999, size=n),
        "rok": rng.choice([2018, 2019, 2020, 2021, 2022], size=n),
        "turnover": rng.lognormal(12, 1.2, size=n).round(2),
    })
    old.to_csv(_p("returns_2018_2022.csv"), index=False)
    old.to_stata(_p("returns_2018_2022.dta"), write_index=False)

    # 2023 vintage: same keys stored as TEXT (and one column absent, one new)
    new = pd.DataFrame({
        "ic": [f"{i:08d}" for i in ids[: n // 2]],
        "c_ds": [str(v) for v in rng.integers(100, 999, size=n // 2)],
        "dicc_poplatnika": [str(v) for v in rng.integers(1000, 9999, size=n // 2)],
        "rok": np.full(n // 2, 2023),
        "turnover": rng.lognormal(12, 1.2, size=n // 2).round(2),
        "new_levy": rng.lognormal(8, 1.0, size=n // 2).round(2),
    })
    new.to_csv(_p("returns_2023.csv"), index=False)
    new.to_stata(_p("returns_2023.dta"), write_index=False)


def make_long_over_subentity() -> None:
    """Failure 5: the file is long over transfer-pricing annex counterparties,
    so no single column is a key -- (ic, rok) is not unique either."""
    rng = np.random.default_rng(SEED + 2)
    rows = []
    for firm in range(200):
        ic = 50000000 + firm
        for rok in (2022, 2023):
            n_cp = rng.integers(1, 5)           # 1-4 counterparties per firm-year
            for cp in range(n_cp):
                rows.append({
                    "ic": ic,
                    "rok": rok,
                    "counterparty_seq": cp + 1,
                    "cp_country": rng.choice(["DE", "AT", "NL", "CY", "LU"]),
                    "tp_amount": round(float(rng.lognormal(11, 1.1)), 2),
                    "turnover": round(float(rng.lognormal(13, 0.9)), 2),
                })
    pd.DataFrame(rows).to_csv(_p("tp_annex_long.csv"), index=False)


def main() -> None:
    make_utf16_semicolon()
    make_type_drift()
    make_long_over_subentity()
    for f in sorted(os.listdir(HERE)):
        if f.endswith(".csv"):
            print(f"  wrote {f} ({os.path.getsize(_p(f))} bytes)")


if __name__ == "__main__":
    main()
