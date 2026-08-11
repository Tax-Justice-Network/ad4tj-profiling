*! =============================================================================
*! AD4TJ disclosure-safe dataset profiler -- LIGHTWEIGHT / LARGE-FILE version
*! =============================================================================
*!
*! Same purpose and same disclosure guarantees as profile_dataset.do, but built
*! for very large files (multi-GB) where the full profiler is too slow.
*!
*! -----------------------------------------------------------------------------
*! WHAT IS TRADED AWAY (and why it costs you little)
*! -----------------------------------------------------------------------------
*!  1. ROW SAMPLING (the big one). Statistics are computed on a random sample of
*!     SAMPLE_N rows instead of every row. For a codebook -- types, missingness,
*!     share of zeros, rough distribution -- a million rows describes the shape
*!     of the data just as well as fifty million. Counts in the log are SAMPLE
*!     counts; percentages are unbiased estimates of the population.
*!     For .dta input the sample is drawn AS THE FILE IS READ, so a 7GB file
*!     never has to fit in memory. This is usually the whole ballgame.
*!  2. No "summarize, detail". It sorts the variable and computes skewness,
*!     kurtosis and top/bottom-5 lists we never print. Replaced by one
*!     "summarize" (no sort) plus one "_pctile" for the percentiles.
*!  3. No distinct-value count for continuous numerics. It required a sort per
*!     variable and is meaningless for a money variable. Distinct counts are
*!     still computed where they matter: strings, dates, and integer columns
*!     (needed to spot code lists and identifiers).
*!  4. Fewer passes over the data: "# positive" is derived by arithmetic rather
*!     than counted, and the integer check is skipped when the storage type
*!     (byte/int/long) already proves it.
*!
*! WHAT IS **NOT** TRADED AWAY: every disclosure-control rule is identical --
*! small-count suppression, no exact min/max, rounding, category-listing limit,
*! identifier detection (including by column name), date coarsening.
*! Sampling, if anything, adds protection.
*!
*! -----------------------------------------------------------------------------
*! HOW TO USE
*! -----------------------------------------------------------------------------
*! 1. Edit the CONFIG block below: set DATA_PATH to your data file.
*! 2. Run:   do profile_dataset_lightweight.do
*! 3. A "<dataset>.codebook.log" file is written next to your data.
*!
*! If it is still too slow, lower SAMPLE_N (e.g. 250000). If you want exact
*! whole-file figures and have the time, set SAMPLE_N to 0.
*!
*! Supported input formats: .csv, .tsv, .xlsx/.xls, .dta
*! =============================================================================

clear all
set more off

* ===========================================================================
* CONFIG  --  edit this block, nothing else is required
* ===========================================================================
global DATA_PATH            "examples/income_tax/returns.csv"  // path to data
global OUTPUT_PATH          ""    // output log path; "" = derive from DATA_PATH
global DATASET_LABEL        ""    // optional title; "" = use the file name

* Speed settings
global SAMPLE_N             1000000  // rows to profile; 0 = use every row
global SEED                 20240101 // fixed so the run is reproducible

* Disclosure-control settings (defaults are conservative; adjust to lab policy)
global MIN_CELL_COUNT       10    // suppress any count strictly below this
global ROUND_SIGNIFICANT    3     // round numeric summaries to this many sig figs
global MAX_CATEGORIES       20    // list category values only if distinct <= this
global ID_LIKE_UNIQUE_RATIO 0.9   // distinct/non-missing above this => identifier

* Column-name hints that mark a variable as a direct identifier (values and
* distribution NEVER released), even when they repeat across a panel.
global ID_NAME_HINTS "tpin taxpayer nrc ssn passport reference refno reg_no regno national_id"
global ID_LARGE_INT_THRESHOLD 1e9 // advisory: flag integer cols this large with no 0/neg
* ===========================================================================


* ---------------------------------------------------------------------------
* Helper programs (must be defined before use)
* ---------------------------------------------------------------------------
capture program drop sigfig
program define sigfig, rclass
    * round to ROUND_SIGNIFICANT significant figures; return string in r(v)
    args x
    if ("`x'" == "" | "`x'" == ".") {
        return local v = "n/a"
        exit
    }
    if (`x' >= .) {
        return local v = "n/a"
        exit
    }
    if (`x' == 0) {
        return local v = "0"
        exit
    }
    local d = $ROUND_SIGNIFICANT - 1 - floor(log10(abs(`x')))
    local r = round(`x', 10^(-`d'))
    return local v = strofreal(`r', "%14.0g")
end

capture program drop safecount
program define safecount, rclass
    * suppress small non-zero counts; return string in r(v)
    args c
    if (`c' == 0) {
        return local v = "0"
    }
    else if (`c' < $MIN_CELL_COUNT) {
        return local v = "<suppressed (below min cell count)>"
    }
    else {
        return local v = "`c'"
    }
end

capture program drop id_by_name
program define id_by_name, rclass
    * return r(hit)=1 if the column NAME looks like a direct identifier
    args name
    local n = lower("`name'")
    local hit 0
    foreach h of global ID_NAME_HINTS {
        if (strpos("`n'", "`h'") > 0) local hit 1
    }
    if (strpos("`n'", "_id")  > 0) local hit 1
    if (strpos("`n'", "id_")  > 0) local hit 1
    if (strpos("`n'", "_tin") > 0) local hit 1
    if (strpos("`n'", "tin_") > 0) local hit 1
    if (inlist("`n'", "id", "tin", "tpin", "uid", "nrc", "ssn", "pin", "brn", "uin")) local hit 1
    return local hit = `hit'
end

capture program drop write_categories
program define write_categories
    * value-count table with small-count suppression (only called when the
    * variable has at most MAX_CATEGORIES distinct values, so this loop is short)
    args var fh
    quietly levelsof `var', local(levels)
    local n_suppressed 0
    local sup_total 0
    file write `fh' "  value counts    :" _n
    local is_num = 0
    capture confirm numeric variable `var'
    if (_rc == 0) local is_num = 1
    foreach l of local levels {
        if (`is_num') quietly count if `var' == `l'
        else quietly count if `var' == `"`l'"'
        local c = r(N)
        if (`c' < $MIN_CELL_COUNT) {
            local n_suppressed = `n_suppressed' + 1
            local sup_total = `sup_total' + `c'
        }
        else {
            local lshow : subinstr local l `"""' "'", all
            file write `fh' `"      '`lshow'' : `c'"' _n
        }
    }
    if (`n_suppressed' > 0) {
        safecount `sup_total'
        file write `fh' "      <`n_suppressed' rare value(s)> : combined `r(v)'" _n
    }
end


* ---------------------------------------------------------------------------
* Load the data, sampling on the way in where possible
* ---------------------------------------------------------------------------
local path "$DATA_PATH"
local ext = lower(substr("`path'", strrpos("`path'", ".") + 1, .))

local sampled   0
local n_file    .
local frac      1

set seed $SEED

if ("`ext'" == "dta") {
    * read only the header first: instant even on a 7GB file
    quietly describe using "`path'"
    local n_file = r(N)
    if ($SAMPLE_N > 0 & `n_file' > $SAMPLE_N) {
        local frac = $SAMPLE_N / `n_file'
        * draw the sample AS THE FILE IS READ, so the whole file is never
        * held in memory
        use if runiform() <= `frac' using "`path'", clear
        local sampled 1
    }
    else {
        use "`path'", clear
    }
}
else {
    if ("`ext'" == "csv") {
        import delimited "`path'", varnames(1) case(preserve) clear
    }
    else if ("`ext'" == "tsv") {
        import delimited "`path'", varnames(1) case(preserve) delimiter(tab) clear
    }
    else if ("`ext'" == "xlsx" | "`ext'" == "xls") {
        import excel "`path'", firstrow clear
    }
    else {
        display as error "Unsupported file type '.`ext''. Supported: dta csv tsv xlsx xls"
        exit 198
    }
    local n_file = _N
    if ($SAMPLE_N > 0 & _N > $SAMPLE_N) {
        local frac = $SAMPLE_N / `n_file'
        quietly keep if runiform() <= `frac'
        local sampled 1
    }
}

ds
local allvars `r(varlist)'
local n_rows = _N
local n_cols : word count `allvars'

local label "$DATASET_LABEL"
if ("`label'" == "") {
    local label = substr("`path'", strrpos("`path'", "/") + 1, .)
}

local out "$OUTPUT_PATH"
if ("`out'" == "") {
    local dot = strrpos("`path'", ".")
    local out = substr("`path'", 1, `dot' - 1) + ".codebook.log"
}


* ---------------------------------------------------------------------------
* Header
* ---------------------------------------------------------------------------
tempname fh
file open `fh' using "`out'", write replace text

local bar  = "============================================================================"
local dash = "----------------------------------------------------------------------------"

file write `fh' "`bar'" _n
file write `fh' "AD4TJ DISCLOSURE-SAFE CODEBOOK  (lightweight / sampled profiler)" _n
file write `fh' "`bar'" _n
file write `fh' "Dataset      : `label'" _n
file write `fh' "Source file  : `path'" _n
file write `fh' "Rows in file : `n_file'" _n
file write `fh' "Columns      : `n_cols'" _n
if (`sampled') {
    local fracpct = round(100 * `frac', 0.01)
    file write `fh' "Rows profiled: `n_rows'  (random sample, ~`fracpct'% of the file, seed $SEED)" _n
    file write `fh' "" _n
    file write `fh' "NOTE: every statistic below is computed on that random sample." _n
    file write `fh' "      Counts are SAMPLE counts, not whole-file counts." _n
    file write `fh' "      Percentages and distributions are unbiased estimates of the file." _n
    file write `fh' "      Distinct-value counts are counts WITHIN THE SAMPLE (a rare category" _n
    file write `fh' "      may be absent from it). Set SAMPLE_N to 0 for exact whole-file figures." _n
}
else {
    file write `fh' "Rows profiled: `n_rows'  (all rows)" _n
}
file write `fh' "" _n
file write `fh' "Disclosure-control rules applied to this log:" _n
file write `fh' "  - counts below $MIN_CELL_COUNT are suppressed" _n
file write `fh' "  - numeric summaries rounded to $ROUND_SIGNIFICANT significant figures" _n
file write `fh' "  - exact minimum and maximum values are NEVER released" _n
file write `fh' "  - category values listed only when distinct count <= $MAX_CATEGORIES" _n
file write `fh' "  - identifier / high-cardinality column values are not listed" _n
file write `fh' "  - columns whose NAME matches an identifier pattern are withheld entirely" _n
file write `fh' "" _n
file write `fh' "This file contains NO individual-level data points. The data lab must" _n
file write `fh' "still review it against local disclosure policy before release." _n
file write `fh' "This tool applies frequency-style controls only; it does NOT implement the" _n
file write `fh' "dominance / p-percent rule for magnitude data -- see SPEC.md section 4.1." _n
file write `fh' "`bar'" _n
file write `fh' "" _n


* ---------------------------------------------------------------------------
* Per-variable profiling
* ---------------------------------------------------------------------------
local i 0
foreach var of local allvars {
    local i = `i' + 1
    display as text "  [`i'/`n_cols'] `var'"

    local vlab : variable label `var'
    local vlab : subinstr local vlab `"""' "'", all

    capture confirm numeric variable `var'
    local is_numeric = (_rc == 0)

    * ---- one pass: N, mean, sd, min, max (no sort) --------------------------
    local v_mean = .
    local v_sd   = .
    local v_min  = .
    if (`is_numeric') {
        quietly summarize `var'
        local n_obs  = r(N)
        local v_mean = r(mean)
        local v_sd   = r(sd)
        local v_min  = r(min)
        local v_max  = r(max)
    }
    else {
        quietly count if !missing(`var')
        local n_obs = r(N)
    }
    local n_missing = `n_rows' - `n_obs'
    if (`n_rows' > 0) local pct = round(100 * `n_missing' / `n_rows', 0.1)
    else local pct = 0

    * ---- date detection (from the display format; costs nothing) ------------
    local isdate 0
    local dtype ""
    local dfmt ""
    if (`is_numeric') {
        local dfmt : format `var'
        if (substr("`dfmt'", 1, 2) == "%t") {
            local dtype = substr("`dfmt'", 3, 1)
            if (inlist("`dtype'", "d", "c", "C", "w", "m", "q", "h", "y")) local isdate 1
        }
        else if (substr("`dfmt'", 1, 2) == "%d") {
            local isdate 1
            local dtype "d"
        }
    }

    * ---- integer check: free when the storage type already proves it --------
    local is_int 0
    if (`is_numeric' & `n_obs' > 0) {
        local vtype : type `var'
        if (inlist("`vtype'", "byte", "int", "long")) {
            local is_int 1
        }
        else {
            quietly count if `var' != floor(`var') & !missing(`var')
            local is_int = (r(N) == 0)
        }
    }

    * ---- does the column NAME look like a direct identifier? ----------------
    id_by_name "`var'"
    local id_hit = r(hit)

    * ---- distinct count, but ONLY where it changes the answer ---------------
    * (skipped for continuous numerics: it needed a sort per variable and tells
    *  a researcher nothing about a money variable)
    local nd = .
    local nd_known 0
    local need_nd = 0
    if (`n_obs' > 0) {
        if (!`is_numeric')                   local need_nd = 1
        else if (`isdate')                   local need_nd = 1
        else if (`is_int' & !`id_hit')       local need_nd = 1
        else if (`id_hit')                   local need_nd = 1
    }
    if (`need_nd') {
        * tabulate hashes in a single pass (no sort); it errors when there are
        * too many levels, which itself tells us the column is high-cardinality
        capture quietly tabulate `var', nofreq
        if (_rc == 0) {
            local nd = r(r)
            local nd_known 1
        }
        else {
            * rare branch: fall back to an exact count only when we truly need
            * the distinct/rows ratio to decide identifier vs high-cardinality
            tempvar tg
            quietly egen `tg' = tag(`var') if !missing(`var')
            quietly count if `tg' == 1
            local nd = r(N)
            local nd_known 1
            drop `tg'
        }
    }

    * ---- classify -----------------------------------------------------------
    local kind ""
    if (`n_obs' == 0) {
        local kind "empty"
    }
    else if (`is_numeric' & `isdate') {
        local kind "datetime"
    }
    else if (`id_hit') {
        local kind "identifier"
    }
    else if (`is_numeric') {
        if (`is_int' & `nd_known' & (`nd' == `n_obs' | `nd' / `n_obs' > $ID_LIKE_UNIQUE_RATIO)) {
            local kind "identifier"
        }
        else {
            local kind "numeric"
        }
    }
    else {
        if (`nd' <= $MAX_CATEGORIES) {
            local kind "categorical"
        }
        else if (`nd' >= $ID_LIKE_UNIQUE_RATIO * `n_obs') {
            local kind "identifier"
        }
        else {
            local kind "high_cardinality"
        }
    }

    * ---- common header ------------------------------------------------------
    file write `fh' "`dash'" _n
    file write `fh' "VARIABLE: `var'" _n
    if ("`vlab'" != "") file write `fh' "  label           : `vlab'" _n
    file write `fh' "  kind            : `kind'" _n
    file write `fh' "  non-missing     : `n_obs'" _n
    file write `fh' "  missing         : `n_missing' (`pct'%)" _n

    * ---- per-kind facts -----------------------------------------------------
    if ("`kind'" == "empty") {
        file write `fh' "  note            : Column is entirely missing." _n
    }
    else if ("`kind'" == "datetime") {
        file write `fh' "  date format     : `dfmt'" _n
        if (`nd_known') file write `fh' "  distinct values : `nd'" _n
        * coarsen to a calendar-year span; min/max already in hand from summarize
        if ("`dtype'" == "y") {
            local ymin = `v_min'
            local ymax = `v_max'
        }
        else if ("`dtype'" == "d") {
            local ymin = year(`v_min')
            local ymax = year(`v_max')
        }
        else if ("`dtype'" == "c") {
            local ymin = year(dofc(`v_min'))
            local ymax = year(dofc(`v_max'))
        }
        else if ("`dtype'" == "C") {
            local ymin = year(dofC(`v_min'))
            local ymax = year(dofC(`v_max'))
        }
        else if ("`dtype'" == "w") {
            local ymin = year(dofw(`v_min'))
            local ymax = year(dofw(`v_max'))
        }
        else if ("`dtype'" == "m") {
            local ymin = year(dofm(`v_min'))
            local ymax = year(dofm(`v_max'))
        }
        else if ("`dtype'" == "q") {
            local ymin = year(dofq(`v_min'))
            local ymax = year(dofq(`v_max'))
        }
        else if ("`dtype'" == "h") {
            local ymin = year(dofh(`v_min'))
            local ymax = year(dofh(`v_max'))
        }
        file write `fh' "  year span       : `ymin' .. `ymax'" _n
        file write `fh' "  note            : Date variable; detail reduced to calendar-year span, exact dates not listed." _n
    }
    else if ("`kind'" == "numeric") {
        file write `fh' "  integer-valued  : `is_int'" _n

        * two passes; "# positive" is derived rather than counted
        quietly count if `var' == 0 & !missing(`var')
        local n_zero = r(N)
        quietly count if `var' < 0 & !missing(`var')
        local n_neg = r(N)
        local n_pos = `n_obs' - `n_zero' - `n_neg'
        safecount `n_zero'
        file write `fh' "  # zero          : `r(v)'" _n
        safecount `n_neg'
        file write `fh' "  # negative      : `r(v)'" _n
        safecount `n_pos'
        file write `fh' "  # positive      : `r(v)'" _n

        if (`is_int' & `nd_known' & `nd' <= $MAX_CATEGORIES) {
            file write `fh' "  looks coded     : yes (few distinct integer values - likely a code list)" _n
            file write `fh' "  distinct values : `nd'" _n
            write_categories `var' `fh'
        }

        if (`n_obs' < $MIN_CELL_COUNT) {
            file write `fh' "  note            : Too few observations to release summary statistics; suppressed." _n
        }
        else {
            sigfig `v_mean'
            file write `fh' "  mean (rounded)  : `r(v)'" _n
            sigfig `v_sd'
            file write `fh' "  sd (rounded)    : `r(v)'" _n

            * one sort for all percentiles (replaces summarize, detail)
            _pctile `var', percentiles(1 5 25 50 75 95 99)
            local q1  = r(r1)
            local q5  = r(r2)
            local q25 = r(r3)
            local q50 = r(r4)
            local q75 = r(r5)
            local q95 = r(r6)
            local q99 = r(r7)

            sigfig `q1'
            local lo "`r(v)'"
            sigfig `q99'
            local hi "`r(v)'"
            file write `fh' "  approx range    : `lo' .. `hi'  (rounded p1-p99)" _n

            local qline ""
            foreach p in 1 5 25 50 75 95 99 {
                sigfig `q`p''
                local qline "`qline'  p`p'=`r(v)'"
            }
            file write `fh' "  quantiles       :`qline'" _n
            file write `fh' "  note            : Exact min/max withheld; range shown is rounded p1-p99. All numeric summaries rounded to $ROUND_SIGNIFICANT significant figures." _n

            * advisory: uniformly huge integers with no 0/neg look like an identifier
            if (`is_int' & `n_zero' == 0 & `n_neg' == 0 & `v_min' >= $ID_LARGE_INT_THRESHOLD) {
                file write `fh' "  note            : WARNING: integer column with uniformly large values and no zeros or negatives -- verify this is a genuine measure and not an identifier that should be withheld." _n
            }
        }
    }
    else if ("`kind'" == "categorical") {
        file write `fh' "  distinct values : `nd'" _n
        write_categories `var' `fh'
    }
    else if ("`kind'" == "high_cardinality") {
        file write `fh' "  distinct values : `nd'" _n
        file write `fh' "  note            : More than $MAX_CATEGORIES distinct values; individual values NOT listed. If the code list is needed, request it separately (subject to lab approval)." _n
    }
    else if ("`kind'" == "identifier") {
        if (`nd_known') {
            file write `fh' "  distinct values : `nd'" _n
            local uniq = (`nd' == `n_obs')
            file write `fh' "  unique per row  : `uniq'" _n
        }
        if (`id_hit') {
            file write `fh' "  note            : Column name matches an identifier pattern; treated as a direct identifier -- values and distribution (mean/quantiles) NOT released." _n
        }
        else {
            file write `fh' "  note            : High-cardinality / identifier-like column: individual values NOT listed." _n
        }
    }
}

file write `fh' "`dash'" _n
file write `fh' "" _n
file write `fh' "END OF CODEBOOK" _n
file close `fh'

display as result "Codebook written to: `out'"
