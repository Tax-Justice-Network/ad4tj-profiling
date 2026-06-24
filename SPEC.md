# AD4TJ Disclosure-Safe Codebook — draft v1.0

**Status:** draft open standard, pilot. Comments invited.
**Date:** 2026.

This document specifies the **codebook** produced by the AD4TJ profiler: what it
contains, and the statistical-disclosure-control (SDC) rules it must obey. The
codebook is the only artifact that leaves the secure environment, so its safety
properties are normative.

The key words **MUST**, **MUST NOT**, **SHOULD**, and **MAY** are used as in
RFC 2119.

---

## 1. Purpose and threat model

A codebook describes a confidential dataset well enough that a researcher can
write correct, runnable analysis code without access to the data, **while
disclosing nothing about any individual record**.

The adversary is assumed to have the codebook and arbitrary external knowledge.
The codebook therefore MUST contain only aggregate quantities, and MUST apply the
controls in §4. A conforming codebook reveals the *structure and shape* of a
dataset, never its *contents*.

The producing tool is the final line of defence in code, but the **data lab
remains the final human authority**: a codebook MUST be reviewed against local
policy before release.

---

## 2. Codebook structure

A codebook is a UTF-8 text file (conventionally `<dataset>.codebook.log`). It has
a **header** followed by one **variable block** per column.

### 2.1 Header

The header MUST contain:

- a title line identifying it as an AD4TJ disclosure-safe codebook;
- the dataset label and source file name;
- the number of rows and columns;
- an explicit list of the SDC rules and threshold values actually applied;
- a statement that the file contains no individual-level data and requires lab
  review before release.

Row and column counts are dataset dimensions, not record values, and MAY be
reported exactly.

### 2.2 Variable block

Each variable block MUST report:

- `name` — the variable name;
- `label` — the variable label, if the source carries one (optional);
- `kind` — one of the classes in §3;
- `non-missing` — count of non-missing observations;
- `missing` — count and percentage missing.

It then reports kind-specific facts (§3) and free-text `note` lines describing any
suppression or transformation applied.

---

## 3. Variable kinds

The profiler classifies each column into exactly one kind. Classification is
inferred from the data alone (no external documentation is required).

| kind | inferred when | reported facts |
|------|---------------|----------------|
| `numeric` | numeric, not identifier-like | integer-valued flag; # zero / # negative / # positive (suppressed); mean, sd (rounded); rounded quantiles p1–p99; approx range (rounded p1–p99) |
| `numeric` (coded) | numeric, integer-valued, ≤ `MAX_CATEGORIES` distinct | as numeric, **plus** a suppressed value-count table; flagged `looks coded` |
| `categorical` | non-numeric, < identifier threshold | distinct count; suppressed value-count table |
| `boolean` | logical/boolean dtype | # true / # false (suppressed) |
| `datetime` | date/time dtype | distinct dates; **calendar-year span only** |
| `identifier` | integer near-unique, **or** non-numeric at/above identifier threshold | distinct count; unique-per-row flag; values **never** listed |
| `empty` | entirely missing | note only |

**Identifier threshold.** A non-numeric column is an identifier when its distinct
count ≥ `max(MAX_CATEGORIES + 1, ID_LIKE_UNIQUE_RATIO × n_obs)`. A numeric column
is an identifier when it is integer-valued **and** either fully unique or has a
distinct/non-missing ratio above `ID_LIKE_UNIQUE_RATIO`.

**Coded categoricals.** Integer columns with few distinct values (sex, region,
filing status, year, …) are almost always code lists. The codebook reports the
set of codes and their frequencies — exactly what a researcher needs — in
addition to (harmless) numeric summaries.

---

## 4. Disclosure-control rules (normative)

A conforming codebook MUST apply all of the following. Threshold values are
configurable but their defaults are the recommended floor.

1. **Small-count suppression.** Any reported count that is greater than zero and
   strictly below `MIN_CELL_COUNT` (default **10**) MUST be suppressed and
   replaced with an explicit suppression marker. A count of exactly zero MAY be
   reported.

2. **No exact extremes.** The exact minimum and maximum of a variable MUST NOT be
   released. Where a range is useful, the codebook reports a **rounded p1–p99**
   range instead. (The exact maximum of, e.g., income is a single real person's
   value.)

3. **Rounding.** Released means, standard deviations, and quantiles MUST be
   rounded to `ROUND_SIGNIFICANT` significant figures (default **3**) so that no
   released number coincides with an exact record value.

4. **Category listing limit.** Individual category values MUST be listed only when
   the distinct count is ≤ `MAX_CATEGORIES` (default **20**). Within a listed
   table, any category with a count below `MIN_CELL_COUNT` MUST be suppressed and
   reported only as a combined "rare values" total (itself subject to rule 1).

5. **Identifier and free-text protection.** Columns classified as `identifier`
   MUST NOT have their values listed. String columns are never printed verbatim
   and are treated as categories only under rules 1 and 4.

6. **Too-few-observations.** If a variable has fewer than `MIN_CELL_COUNT`
   non-missing observations, summary statistics MUST be suppressed entirely.

7. **Datetime coarsening.** Date/time variables MUST be reduced to a
   calendar-year span; finer detail MUST NOT be released.

8. **Transparency.** The header MUST record the actual threshold values applied,
   and each suppression MUST leave a visible marker or note, so a reviewer can
   audit what was withheld.

---

## 5. Configuration parameters

| parameter | default | meaning |
|-----------|---------|---------|
| `MIN_CELL_COUNT` | 10 | suppress counts strictly below this |
| `ROUND_SIGNIFICANT` | 3 | significant figures for numeric summaries |
| `MAX_CATEGORIES` | 20 | list category values only at/below this distinct count |
| `ID_LIKE_UNIQUE_RATIO` | 0.9 | distinct/non-missing above this ⇒ identifier |

A lab MAY raise (more conservative) any threshold; lowering a threshold below the
default SHOULD require explicit local justification.

---

## 6. Conformance

An implementation is conforming if, for any input dataset, its codebook:

1. contains every section required by §2;
2. classifies each variable per §3;
3. satisfies every rule in §4 for every reported quantity;
4. records the applied thresholds per §4.8.

The reference implementation is `profile_dataset.py`. The test suite in
`tests/test_profile.py` includes adversarial checks that exact extremes, rare
category values, and identifier/string values do not appear in the rendered
codebook; these tests form part of the conformance criteria.

---

## 7. Versioning

This is version **1.0 (draft)**. Backward-incompatible changes to the required
structure (§2) or the SDC rules (§4) will increment the major version. Additive,
backward-compatible facts may be introduced in minor versions.
