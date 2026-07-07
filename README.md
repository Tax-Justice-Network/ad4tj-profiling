# AD4TJ — disclosure-safe dataset profiler

A pilot toolkit for the **Admin Data for Tax Justice** initiative.

Researchers who want to study confidential tax-administration microdata normally
have to write and debug their analysis code *inside* a secure data lab — slow,
expensive, and a bottleneck for everyone. This toolkit lets a researcher develop
and test code **outside** the lab, then submit only the finished code to be run
on the real data.

The bridge is a single safe artifact: a **codebook** — a machine- and
human-readable description of a dataset that contains **no individual data
points**. The lab produces it from the real data and publishes it; the
researcher writes code against it.

---

## The three-step workflow

```
   ┌─────────────────────────────┐
   │  1. INSIDE THE LAB          │   Lab staff run profile_dataset.{py,R,do}
   │     real confidential data  │   on the real data. It writes a codebook
   │            │                │   log: variable names, types, % missing,
   │            ▼                │   # zeros, rounded summary stats, code
   │     codebook.log  ──────────┼─▶ lists. No raw value ever appears.
   └─────────────────────────────┘   The lab reviews it, then releases it.

   ┌─────────────────────────────┐
   │  2. OUTSIDE THE LAB         │   Researcher reads the codebook and writes
   │     researcher's laptop     │   analysis code in Python, R, or Stata that
   │            │                │   references the documented variables, types,
   │            ▼                │   and code lists — without ever seeing data.
   │     analysis script         │
   └─────────────────────────────┘

   ┌─────────────────────────────┐
   │  3. BACK INSIDE THE LAB     │   Lab staff run the submitted script on the
   │     real confidential data  │   real data and apply the administration's
   │            │                │   standard disclosure control to the results
   │            ▼                │   before anything is returned.
   │     released results        │
   └─────────────────────────────┘
```

### Governance — by design

- **Nothing confidential leaves the environment.** The only artifact published
  from the lab is the codebook, which carries aggregates only and is reviewed by
  lab staff before release.
- **No model and no researcher ever touches the real data.** The profiler is
  ordinary, auditable code. There is no AI in the data path.
- **Every result passes standard disclosure control.** Step 3 outputs go through
  the administration's existing disclosure-control process, exactly as today.
- **The lab is always the final authority.** The conservative rules below are a
  safe default, not a substitute for the lab's own policy.

---

## What the codebook guarantees

The profiler applies conservative statistical-disclosure-control (SDC) rules so
that the log can be released. By default:

| Rule | Default | Why |
|------|---------|-----|
| Small-count suppression | counts `< 10` suppressed | a tiny cell can identify individuals |
| No exact extremes | min/max **never** released | the maximum income *is* one real person's income |
| Rounding | 3 significant figures | so no released number equals an exact record |
| Category listing | only if ≤ 20 distinct values | high-cardinality columns leak |
| Identifier columns | values never listed | IDs, names, free text are not aggregated |

All five thresholds are set in the **CONFIG block at the top of each script** and
are printed into every codebook so a reviewer can see exactly what was applied.

See **[SPEC.md](SPEC.md)** for the codebook format and the normative SDC rules.

---

## Quick start

Pick the file for your language. Each is standalone — there is nothing to learn
about the format. In all three, set `DATA_PATH` in the **CONFIG block at the top**
to point at your data file, then run it.

### Python (the reference implementation)

```bash
pip install pandas numpy openpyxl   # openpyxl only needed for .xlsx
# edit DATA_PATH at the top of profile_dataset.py, then:
python profile_dataset.py
```

### R

```r
# edit DATA_PATH at the top of profile_dataset.R, then:
Rscript profile_dataset.R
# .xlsx needs install.packages("readxl"); .dta needs install.packages("haven")
```

### Stata

```stata
* edit DATA_PATH at the top of profile_dataset.do, then:
do profile_dataset.do
```

A quoted `DATA_PATH` handles paths containing spaces. From a terminal you can
also run it head-less: `stata -b do profile_dataset.do`.

Each writes `<yourdata>.codebook.log` next to your data file. Supported inputs:
CSV, TSV, Excel (`.xlsx`/`.xls`), and Stata `.dta`.

---

## Worked example: administrative personal income tax

The `examples/income_tax/` folder contains a **toy, fully synthetic** two-table
dataset (no real records — see `_make_demo_data.py`) used to demonstrate and test
the tool:

- `taxpayers.csv` — one row per person: `taxpayer_id`, `birth_year`,
  `region_code`, `sex`.
- `returns.csv` — one row per taxpayer-year: `taxpayer_id`, `tax_year`,
  `gross_income`, `taxable_income`, `deductions_claimed`, `tax_due`,
  `filing_status`.

The committed `*.codebook.log` files are the profiler's output for this example —
open them to see exactly what a researcher would receive. Note how `taxpayer_id`
is recognised as an identifier (no quantiles), `sex`/`region_code`/`filing_status`
are recognised as coded categories (value counts shown), and `gross_income`
reports rounded quantiles with its exact maximum withheld.

To regenerate the demo data and codebooks:

```bash
python examples/income_tax/_make_demo_data.py
python profile_dataset.py     # with DATA_PATH set to each table
```

---

## Development

```bash
pip install -e ".[dev]"
pytest        # parser/classifier/disclosure-safety tests
ruff check .
```

The Python implementation is the tested reference. The **Stata script has been
run on a range of real datasets** (StataBE) — CSV, `.dta`, and `.xlsx`; files
with date/quarter variables; and inputs up to 1.3 GB and 1,723 columns — and
produces an equivalent codebook with the same classification and suppression.
Minor cosmetic differences (Stata prints `1`/`0` for booleans and drops trailing
`.0`) and a small percentile-rule difference (`summarize, detail` vs numpy) are
expected and do not affect the disclosure guarantees. The **R script mirrors the
reference** (same thresholds, `quantile type=7` = numpy's linear interpolation,
same layout) and has been reviewed against it, but has **not yet been executed**
because R was not installed in the test environment — validating it on a real R
install is a recommended next step. The one code path not yet exercised on real
data is the `boolean` branch (no `0/1` byte-typed columns encountered).

## Status

This is a **pilot**. The codebook format is published as a draft **v1.0 open
standard** in [SPEC.md](SPEC.md); feedback and field-testing in real data labs
are explicitly invited.
