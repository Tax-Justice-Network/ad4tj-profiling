#!/usr/bin/env Rscript
# =============================================================================
# AD4TJ disclosure-safe dataset profiler  (R implementation)
# =============================================================================
#
# Run this INSIDE the secure data-lab environment, on the REAL confidential
# data. It writes a *codebook log file* describing the dataset so researchers
# can write correct, runnable analysis code WITHOUT ever seeing the data.
#
# The log NEVER releases an individual data point. It applies conservative
# statistical-disclosure-control (SDC) rules:
#   * counts below MIN_CELL_COUNT are suppressed,
#   * exact minimum/maximum values are never released,
#   * numeric summaries are rounded to a few significant figures,
#   * string/identifier values are not printed.
#
# The lab remains the final authority: review the log and your local SDC
# policy before releasing it.
#
# -----------------------------------------------------------------------------
# HOW TO USE
# -----------------------------------------------------------------------------
# 1. Edit the CONFIG block below: set DATA_PATH to your data file.
# 2. Run:   Rscript profile_dataset.R
# 3. A "<dataset>.codebook.log" file is written next to your data.
#
# Supported input formats: .csv, .tsv, .xlsx/.xls (needs 'readxl'),
#                          .dta (needs 'haven')
# =============================================================================

# ===========================================================================
# CONFIG  --  edit this block, nothing else is required
# ===========================================================================
DATA_PATH            <- "examples/income_tax/returns.csv"  # path to your data file
OUTPUT_PATH          <- ""        # output log path; "" = derive from DATA_PATH
DATASET_LABEL        <- ""        # optional title; "" = use the file name

# Disclosure-control settings (defaults are conservative; adjust to lab policy)
MIN_CELL_COUNT       <- 10        # suppress any count strictly below this
ROUND_SIGNIFICANT    <- 3         # round numeric summaries to this many sig figs
MAX_CATEGORIES       <- 20        # list category values only if distinct <= this
ID_LIKE_UNIQUE_RATIO <- 0.9       # distinct/non-missing above this => identifier
# ===========================================================================

SUPPRESSED <- "<suppressed (below min cell count)>"


# ---------------------------------------------------------------------------
# Safe helpers
# ---------------------------------------------------------------------------
round_sig <- function(x, sig) {
  if (is.null(x) || length(x) == 0 || is.na(x) || is.infinite(x)) return(NA)
  if (x == 0) return(0)
  signif(x, sig)
}

safe_count <- function(c) {
  if (c == 0) return("0")
  if (c < MIN_CELL_COUNT) return(SUPPRESSED)
  as.character(as.integer(c))
}

fmt <- function(v) {
  if (is.null(v) || length(v) == 0 || (length(v) == 1 && is.na(v))) return("n/a")
  as.character(v)
}


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------
read_data <- function(path) {
  ext <- tolower(tools::file_ext(path))
  if (ext == "csv") {
    return(read.csv(path, stringsAsFactors = FALSE, check.names = FALSE))
  } else if (ext == "tsv") {
    return(read.delim(path, stringsAsFactors = FALSE, check.names = FALSE))
  } else if (ext %in% c("xlsx", "xls")) {
    if (!requireNamespace("readxl", quietly = TRUE))
      stop("Reading Excel needs the 'readxl' package: install.packages('readxl')")
    return(as.data.frame(readxl::read_excel(path)))
  } else if (ext == "dta") {
    if (!requireNamespace("haven", quietly = TRUE))
      stop("Reading Stata files needs the 'haven' package: install.packages('haven')")
    return(as.data.frame(haven::read_dta(path)))
  }
  stop(sprintf("Unsupported file type '%s'. Supported: csv tsv xlsx xls dta", ext))
}


# ---------------------------------------------------------------------------
# Per-column profiling -> returns a list describing the column
# ---------------------------------------------------------------------------
classify <- function(x, n_obs) {
  if (n_obs == 0) return("empty")
  if (is.logical(x)) return("boolean")
  if (inherits(x, "Date") || inherits(x, "POSIXt")) return("datetime")
  if (is.numeric(x)) {
    nn <- x[!is.na(x)]
    n_distinct <- length(unique(nn))
    is_int_like <- all(nn == floor(nn))
    if (is_int_like && (n_distinct == n_obs ||
                        n_distinct / n_obs > ID_LIKE_UNIQUE_RATIO)) {
      return("identifier")
    }
    return("numeric")
  }
  # character / factor
  nn <- x[!is.na(x)]
  n_distinct <- length(unique(nn))
  if (n_distinct >= max(MAX_CATEGORIES + 1, ID_LIKE_UNIQUE_RATIO * n_obs)) {
    return("identifier")
  }
  "categorical"
}

add_category_counts <- function(x, rep) {
  nn <- x[!is.na(x)]
  tab <- sort(table(nn), decreasing = TRUE)
  rep$n_distinct <- length(tab)
  shown <- list()
  n_suppressed_cats <- 0
  suppressed_total <- 0
  for (val in names(tab)) {
    c <- tab[[val]]
    if (c < MIN_CELL_COUNT) {
      n_suppressed_cats <- n_suppressed_cats + 1
      suppressed_total <- suppressed_total + c
      next
    }
    shown[[as.character(val)]] <- as.integer(c)
  }
  rep$categories <- shown
  if (n_suppressed_cats > 0) {
    rep$suppressed_categories <- list(
      n_categories = n_suppressed_cats,
      combined_count = safe_count(suppressed_total)
    )
    rep$notes <- c(rep$notes, sprintf(
      "%d rare category value(s) suppressed (below min cell count).",
      n_suppressed_cats))
  }
  rep
}

profile_column <- function(x, name) {
  n_total <- length(x)
  nn <- x[!is.na(x)]
  n_obs <- length(nn)
  n_missing <- n_total - n_obs
  pct_missing <- if (n_total > 0) round(100 * n_missing / n_total, 1) else 0

  kind <- classify(x, n_obs)
  rep <- list(name = name, kind = kind, storage_dtype = class(x)[1],
              n_obs = n_obs, n_missing = n_missing, pct_missing = pct_missing,
              notes = character(0))

  if (kind == "empty") {
    rep$notes <- c(rep$notes, "Column is entirely missing.")
    return(rep)
  }

  if (kind == "numeric") {
    arr <- as.numeric(nn)
    is_int_like <- all(arr == floor(arr))
    rep$integer_valued <- is_int_like
    rep$n_zero     <- safe_count(sum(arr == 0))
    rep$n_negative <- safe_count(sum(arr < 0))
    rep$n_positive <- safe_count(sum(arr > 0))

    n_distinct <- length(unique(arr))
    if (is_int_like && n_distinct <= MAX_CATEGORIES) {
      rep$looks_coded_categorical <- TRUE
      rep <- add_category_counts(nn, rep)
    }

    if (n_obs < MIN_CELL_COUNT) {
      rep$notes <- c(rep$notes,
        "Too few observations to release summary statistics; suppressed.")
      return(rep)
    }

    rep$mean <- round_sig(mean(arr), ROUND_SIGNIFICANT)
    rep$sd   <- round_sig(if (n_obs > 1) sd(arr) else 0, ROUND_SIGNIFICANT)
    probs <- c(p1 = .01, p5 = .05, p10 = .10, p25 = .25, p50 = .50,
               p75 = .75, p90 = .90, p95 = .95, p99 = .99)
    q <- sapply(probs, function(p) round_sig(as.numeric(
      quantile(arr, p, type = 7, names = FALSE)), ROUND_SIGNIFICANT))
    rep$quantiles <- q
    rep$approx_range <- c(q[["p1"]], q[["p99"]])
    rep$notes <- c(rep$notes, sprintf(paste0(
      "Exact min/max withheld; range shown is rounded p1-p99. ",
      "All numeric summaries rounded to %d significant figures."),
      ROUND_SIGNIFICANT))

  } else if (kind == "boolean") {
    n_true <- sum(nn)
    rep$n_true  <- safe_count(n_true)
    rep$n_false <- safe_count(n_obs - n_true)

  } else if (kind == "datetime") {
    yrs <- as.integer(format(nn, "%Y"))
    rep$n_distinct_dates <- length(unique(nn))
    rep$year_min <- min(yrs)
    rep$year_max <- max(yrs)
    rep$notes <- c(rep$notes, "Date detail reduced to calendar-year span.")

  } else if (kind == "categorical") {
    rep <- add_category_counts(nn, rep)

  } else if (kind == "identifier") {
    n_distinct <- length(unique(nn))
    rep$n_distinct <- n_distinct
    rep$looks_unique <- (n_distinct == n_obs)
    rep$notes <- c(rep$notes,
      "High-cardinality / identifier-like column: individual values NOT listed.")
  }
  rep
}


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
render_log <- function(reports, label, source, n_rows, n_cols) {
  L <- character(0)
  add <- function(...) L[[length(L) + 1]] <<- paste0(...)
  bar <- paste(rep("=", 76), collapse = "")
  dash <- paste(rep("-", 76), collapse = "")

  add(bar)
  add("AD4TJ DISCLOSURE-SAFE CODEBOOK")
  add(bar)
  add("Dataset      : ", label)
  add("Source file  : ", source)
  add("Rows         : ", n_rows)
  add("Columns      : ", n_cols)
  add("")
  add("Disclosure-control rules applied to this log:")
  add("  - counts below ", MIN_CELL_COUNT, " are suppressed")
  add("  - numeric summaries rounded to ", ROUND_SIGNIFICANT, " significant figures")
  add("  - exact minimum and maximum values are NEVER released")
  add("  - category values listed only when distinct count <= ", MAX_CATEGORIES)
  add("  - identifier / high-cardinality column values are not listed")
  add("")
  add("This file contains NO individual-level data points. The data lab must")
  add("still review it against local disclosure policy before release.")
  add(bar)
  add("")

  for (c in reports) {
    add(dash)
    add("VARIABLE: ", c$name)
    add("  kind            : ", c$kind)
    add("  storage dtype   : ", c$storage_dtype)
    add("  non-missing     : ", c$n_obs)
    add("  missing         : ", c$n_missing, " (", c$pct_missing, "%)")
    render_facts(c, add)
    for (note in c$notes) add("  note            : ", note)
  }
  add(dash)
  add("")
  add("END OF CODEBOOK")
  paste0(paste(unlist(L), collapse = "\n"), "\n")
}

render_categories <- function(c, add) {
  add("  distinct values : ", fmt(c$n_distinct))
  if (length(c$categories) > 0) {
    add("  value counts    :")
    for (val in names(c$categories)) {
      add(sprintf("      %20s : %s", paste0("'", val, "'"), c$categories[[val]]))
    }
  }
  if (!is.null(c$suppressed_categories)) {
    add(sprintf("      <%d rare value(s)> : combined %s",
                c$suppressed_categories$n_categories,
                fmt(c$suppressed_categories$combined_count)))
  }
}

render_facts <- function(c, add) {
  if (c$kind == "numeric") {
    add("  integer-valued  : ", fmt(c$integer_valued))
    add("  # zero          : ", fmt(c$n_zero))
    add("  # negative      : ", fmt(c$n_negative))
    add("  # positive      : ", fmt(c$n_positive))
    if (isTRUE(c$looks_coded_categorical)) {
      add("  looks coded     : yes (few distinct integer values - likely a code list)")
      render_categories(c, add)
    }
    if (!is.null(c$mean)) {
      add("  mean (rounded)  : ", fmt(c$mean))
      add("  sd (rounded)    : ", fmt(c$sd))
      add("  approx range    : ", fmt(c$approx_range[1]), " .. ",
          fmt(c$approx_range[2]), "  (rounded p1-p99)")
      qline <- paste(sprintf("%s=%s", names(c$quantiles),
                             sapply(c$quantiles, fmt)), collapse = "  ")
      add("  quantiles       : ", qline)
    }
  } else if (c$kind == "boolean") {
    add("  # true          : ", fmt(c$n_true))
    add("  # false         : ", fmt(c$n_false))
  } else if (c$kind == "datetime") {
    add("  distinct dates  : ", fmt(c$n_distinct_dates))
    add("  year span       : ", fmt(c$year_min), " .. ", fmt(c$year_max))
  } else if (c$kind == "categorical") {
    render_categories(c, add)
  } else if (c$kind == "identifier") {
    add("  distinct values : ", fmt(c$n_distinct))
    add("  unique per row  : ", fmt(c$looks_unique))
  }
}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
main <- function() {
  if (!file.exists(DATA_PATH)) {
    stop(sprintf("data file not found: %s", DATA_PATH))
  }
  label <- if (nzchar(DATASET_LABEL)) DATASET_LABEL else basename(DATA_PATH)
  df <- read_data(DATA_PATH)

  reports <- lapply(names(df), function(nm) profile_column(df[[nm]], nm))
  text <- render_log(reports, label, DATA_PATH, nrow(df), ncol(df))

  out_path <- if (nzchar(OUTPUT_PATH)) OUTPUT_PATH else
    paste0(tools::file_path_sans_ext(DATA_PATH), ".codebook.log")
  writeLines(text, out_path, sep = "")
  message(sprintf("Codebook written to: %s", out_path))
}

if (sys.nframe() == 0) {
  main()
}
