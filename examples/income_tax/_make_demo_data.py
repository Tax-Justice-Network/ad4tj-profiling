#!/usr/bin/env python3
"""
Create TOY demo data for the income-tax example.

THIS IS NOT REAL DATA AND NOT PART OF THE TOOL. It exists only so the profiler
and the tests have something to run against. Every value here is randomly
generated; no real taxpayer is represented.

Run:  python examples/income_tax/_make_demo_data.py
"""
from __future__ import annotations

import os

import numpy as np
import pandas as pd

HERE = os.path.dirname(__file__)
N_TAXPAYERS = 4000
YEARS = range(2018, 2024)
SEED = 20240623


def main() -> None:
    rng = np.random.default_rng(SEED)

    # --- taxpayers (one row per person) ------------------------------------
    ids = np.arange(1, N_TAXPAYERS + 1)
    birth_year = np.clip(rng.normal(1975, 15, N_TAXPAYERS).round(), 1920, 2006).astype(int)
    region = rng.choice(["01", "02", "03", "04", "05"], size=N_TAXPAYERS,
                        p=[0.30, 0.20, 0.20, 0.15, 0.15])
    sex = rng.choice([1, 2], size=N_TAXPAYERS, p=[0.49, 0.51])

    taxpayers = pd.DataFrame(
        {"taxpayer_id": ids, "birth_year": birth_year, "region_code": region, "sex": sex}
    )
    taxpayers.to_csv(os.path.join(HERE, "taxpayers.csv"), index=False)

    # --- returns (one row per taxpayer-year) -------------------------------
    rows = []
    for year in YEARS:
        gross = rng.lognormal(mean=10.2, sigma=0.9, size=N_TAXPAYERS)
        gross = np.round(gross, 2)
        deductions = np.round(np.minimum(gross * rng.uniform(0.0, 0.3, N_TAXPAYERS),
                                         rng.lognormal(8.0, 1.0, N_TAXPAYERS)), 2)
        taxable = np.maximum(gross - deductions, 0).round(2)
        tax_due = np.round(taxable * 0.2, 2)
        filing_status = rng.choice([1, 2, 3, 4], size=N_TAXPAYERS,
                                   p=[0.45, 0.35, 0.10, 0.10])

        # inject some missingness in gross_income (~2%)
        miss = rng.random(N_TAXPAYERS) < 0.02
        gross_with_missing = gross.astype(object)
        gross_with_missing[miss] = np.nan

        rows.append(pd.DataFrame({
            "taxpayer_id": ids,
            "tax_year": year,
            "gross_income": gross_with_missing,
            "taxable_income": taxable,
            "deductions_claimed": deductions,
            "tax_due": tax_due,
            "filing_status": filing_status,
        }))

    returns = pd.concat(rows, ignore_index=True)
    returns.to_csv(os.path.join(HERE, "returns.csv"), index=False)

    print(f"Wrote taxpayers.csv ({len(taxpayers)} rows) and "
          f"returns.csv ({len(returns)} rows)")


if __name__ == "__main__":
    main()
