*! =============================================================================
*! AD4TJ disclosure-safe dataset profiler  (Stata implementation)
*! =============================================================================
*!
*! Run this INSIDE the secure data-lab environment, on the REAL confidential
*! data. It writes a *codebook log file* describing the dataset so researchers
*! can write correct, runnable analysis code WITHOUT ever seeing the data.
*!
*! The log NEVER releases an individual data point. It applies conservative
*! statistical-disclosure-control (SDC) rules:
*!   * counts below MIN_CELL_COUNT are suppressed,
*!   * exact minimum/maximum values are never released (summarize is not shown),
*!   * numeric summaries are rounded to a few significant figures,
*!   * string/identifier values are not printed.
*!
*! The lab remains the final authority: review the log and your local SDC
*! policy before releasing it.
*!
*! -----------------------------------------------------------------------------
*! HOW TO USE
*! -----------------------------------------------------------------------------
*! 1. Edit the CONFIG block below: set DATA_PATH to your data file.
*! 2. Run:   do profile_dataset.do      (or: stata -b do profile_dataset.do)
*! 3. A "<dataset>.codebook.log" file is written next to your data.
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

* Disclosure-control settings (defaults are conservative; adjust to lab policy)
global MIN_CELL_COUNT       10    // suppress any count strictly below this
global ROUND_SIGNIFICANT    3     // round numeric summaries to this many sig figs
global MAX_CATEGORIES       20    // list category values only if distinct <= this
global ID_LIKE_UNIQUE_RATIO 0.9   // distinct/non-missing above this => identifier
* ===========================================================================

* Optional: pass the data path as an argument instead of editing the block above
*   do profile_dataset.do "C:/path/to/mydata.dta"
if (`"`1'"' != "") global DATA_PATH `"`1'"'


* ---------------------------------------------------------------------------
* Helper programs  (must be defined before they are used)
* ---------------------------------------------------------------------------
capture program drop sigfig
program define sigfig, rclass
    * round to ROUND_SIGNIFICANT significant figures; return string in r(v)
    args x
    if (`x' == 0 | "`x'" == "." | "`x'" == "") {
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

capture program drop write_categories
program define write_categories
    * value-count table with small-count suppression
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
            * strip any double quotes in the value so file write cannot break (r198)
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
* Import data based on file extension
* ---------------------------------------------------------------------------
local path "$DATA_PATH"
local ext = lower(substr("`path'", strrpos("`path'", ".") + 1, .))

if ("`ext'" == "dta") {
    use "`path'", clear
}
else if ("`ext'" == "csv") {
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
* Open output file and write the header
* ---------------------------------------------------------------------------
tempname fh
file open `fh' using "`out'", write replace text

local bar  = "============================================================================"
local dash = "----------------------------------------------------------------------------"

file write `fh' "`bar'" _n
file write `fh' "AD4TJ DISCLOSURE-SAFE CODEBOOK" _n
file write `fh' "`bar'" _n
file write `fh' "Dataset      : `label'" _n
file write `fh' "Source file  : `path'" _n
file write `fh' "Rows         : `n_rows'" _n
file write `fh' "Columns      : `n_cols'" _n
file write `fh' "" _n
file write `fh' "Disclosure-control rules applied to this log:" _n
file write `fh' "  - counts below $MIN_CELL_COUNT are suppressed" _n
file write `fh' "  - numeric summaries rounded to $ROUND_SIGNIFICANT significant figures" _n
file write `fh' "  - exact minimum and maximum values are NEVER released" _n
file write `fh' "  - category values listed only when distinct count <= $MAX_CATEGORIES" _n
file write `fh' "  - identifier / high-cardinality column values are not listed" _n
file write `fh' "" _n
file write `fh' "This file contains NO individual-level data points. The data lab must" _n
file write `fh' "still review it against local disclosure policy before release." _n
file write `fh' "`bar'" _n
file write `fh' "" _n


* ---------------------------------------------------------------------------
* Per-variable profiling
* ---------------------------------------------------------------------------
foreach var of local allvars {

    quietly count if !missing(`var')
    local n_obs = r(N)
    local n_missing = `n_rows' - `n_obs'
    if (`n_rows' > 0) local pct = round(100 * `n_missing' / `n_rows', 0.1)
    else local pct = 0

    local vlab : variable label `var'
    * strip any double quotes so file write cannot break (r198) on labels
    local vlab : subinstr local vlab `"""' "'", all

    * distinct count among non-missing (works for numeric and string)
    tempvar tg
    quietly egen `tg' = tag(`var') if !missing(`var')
    quietly count if `tg' == 1
    local nd = r(N)
    drop `tg'

    capture confirm numeric variable `var'
    local is_numeric = (_rc == 0)

    * ---- classify ----
    local kind ""
    local is_int 0
    if (`n_obs' == 0) {
        local kind "empty"
    }
    else if (`is_numeric') {
        quietly count if `var' != floor(`var') & !missing(`var')
        local is_int = (r(N) == 0)
        if (`is_int' & (`nd' == `n_obs' | `nd' / `n_obs' > $ID_LIKE_UNIQUE_RATIO)) {
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

    * ---- common header ----
    file write `fh' "`dash'" _n
    file write `fh' "VARIABLE: `var'" _n
    if ("`vlab'" != "") file write `fh' "  label           : `vlab'" _n
    file write `fh' "  kind            : `kind'" _n
    file write `fh' "  non-missing     : `n_obs'" _n
    file write `fh' "  missing         : `n_missing' (`pct'%)" _n

    * ---- per-kind facts ----
    if ("`kind'" == "empty") {
        file write `fh' "  note            : Column is entirely missing." _n
    }
    else if ("`kind'" == "numeric") {
        file write `fh' "  integer-valued  : `is_int'" _n
        quietly count if `var' == 0 & !missing(`var')
        safecount `r(N)'
        file write `fh' "  # zero          : `r(v)'" _n
        quietly count if `var' < 0 & !missing(`var')
        safecount `r(N)'
        file write `fh' "  # negative      : `r(v)'" _n
        quietly count if `var' > 0 & !missing(`var')
        safecount `r(N)'
        file write `fh' "  # positive      : `r(v)'" _n

        if (`is_int' & `nd' <= $MAX_CATEGORIES) {
            file write `fh' "  looks coded     : yes (few distinct integer values - likely a code list)" _n
            file write `fh' "  distinct values : `nd'" _n
            write_categories `var' `fh'
        }

        if (`n_obs' < $MIN_CELL_COUNT) {
            file write `fh' "  note            : Too few observations to release summary statistics; suppressed." _n
        }
        else {
            * capture all needed statistics first, then round (sigfig resets r())
            quietly summarize `var', detail
            local s_mean = r(mean)
            local s_sd   = r(sd)
            foreach p in 1 5 10 25 50 75 90 95 99 {
                local s_p`p' = r(p`p')
            }
            sigfig `s_mean'
            file write `fh' "  mean (rounded)  : `r(v)'" _n
            sigfig `s_sd'
            file write `fh' "  sd (rounded)    : `r(v)'" _n
            sigfig `s_p1'
            local lo "`r(v)'"
            sigfig `s_p99'
            local hi "`r(v)'"
            file write `fh' "  approx range    : `lo' .. `hi'  (rounded p1-p99)" _n
            local qline ""
            foreach p in 1 5 10 25 50 75 90 95 99 {
                sigfig `s_p`p''
                local qline "`qline'  p`p'=`r(v)'"
            }
            file write `fh' "  quantiles       :`qline'" _n
            file write `fh' "  note            : Exact min/max withheld; range shown is rounded p1-p99. All numeric summaries rounded to $ROUND_SIGNIFICANT significant figures." _n
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
        file write `fh' "  distinct values : `nd'" _n
        local uniq = (`nd' == `n_obs')
        file write `fh' "  unique per row  : `uniq'" _n
        file write `fh' "  note            : High-cardinality / identifier-like column: individual values NOT listed." _n
    }
}

file write `fh' "`dash'" _n
file write `fh' "" _n
file write `fh' "END OF CODEBOOK" _n
file close `fh'

display as result "Codebook written to: `out'"
