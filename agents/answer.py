"""Your agent: a question in, a structured answer out.

Yours to rewrite. One thing is fixed — `main(question) -> dict` must exist, because we
call it directly:

    python -m agents.answer "which manager held the largest Apple position in 2026 Q2?"

Return this shape. Nothing else on stdout.

    {
      "answer":  <number | string | list | null>,
      "unit":    "USD" | "SHARES" | "COUNT" | "PERCENT" | "NAME" | "DATE" | "NONE",
      "sources": ["0001423053-26-000012", ...]
    }

`sources` is graded separately from `answer`, and it is the more diagnostic of the two.
An agent that produces the right number without knowing which filings it came from is
not one a researcher can trust with a question they cannot check by hand.

Put logging on stderr. stdout carries the JSON and nothing else.

See agents/llm.py for the model interface, and docs/04-serve.md for what is graded.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any
import pyarrow.parquet as pq
from agents.parser import parse_question as parse_question_hybrid

OUTPUT = Path(__file__).resolve().parents[1] / "output"
FILINGS = OUTPUT / "filings.parquet"
HOLDINGS = OUTPUT / "holdings.parquet"

VALID_UNITS = {"USD", "SHARES", "COUNT", "PERCENT", "NAME", "DATE", "NONE"}

def validate_query(query: dict) -> bool:
    """Check that a model-produced task has enough information to execute."""
    valid_quarters = {"2026Q1", "2026Q2"}

    task = query["task"]

    if task == "unsupported":
        return True

    single_quarter_tasks = {
        "largest_manager_position",
        "manager_largest_holding",
        "distinct_issuers",
        "portfolio_total",
        "holding_total",
        "holding_exists",
        "filing_type",
        "count_positions",
        "option_count",
        "average_portfolio",
    }

    if task in single_quarter_tasks:
        if query["quarter"] not in valid_quarters:
            return False

    if task in {"largest_change", "compare_position"}:
        if query["from_quarter"] not in valid_quarters:
            return False

        if query["to_quarter"] not in valid_quarters:
            return False

        if query["from_quarter"] == query["to_quarter"]:
            return False

    if task == "largest_manager_position":
        return query["issuer"] is not None

    if task == "largest_change":
        return query["issuer"] is not None

    if task == "manager_largest_holding":
        return query["manager"] is not None

    if task in {
        "distinct_issuers",
        "portfolio_total",
        "count_positions",
    }:
        return query["manager"] is not None

    if task == "holding_exists":
        return (
            query["manager"] is not None
            and query["issuer"] is not None
        )

    if task == "filing_type":
        return query["form_type"] in {"13F-HR", "13F-NT"}

    if task == "option_count":
        return query["option_type"] in {"Call", "Put"}

    if task == "compare_position":
        return query["issuer"] is not None

    return True

def query_to_intent(query: dict) -> dict:
    """Convert a classified research task into an executor intent."""
    task = query["task"]

    intent = {
        "action": "unsupported",
        "manager": query["manager"],
        "issuer": query["issuer"],
        "quarter": query["quarter"],
        "from_quarter": query["from_quarter"],
        "to_quarter": query["to_quarter"],
        "metric": "none",
        "option_type": query["option_type"],
        "form_type": query["form_type"],
        "change_direction": query.get("change_direction"),
        "require_holding_both_quarters":
            query.get("require_holding_both_quarters", False),
    }

    if task == "largest_manager_position":
        intent["action"] = "largest_position"
        intent["metric"] = query["measure"] or "value"

    elif task == "largest_change":
        intent["action"] = "largest_change"
        intent["metric"] = query["measure"] or "value"

    elif task == "manager_largest_holding":
        intent["action"] = "manager_largest_position"
        intent["metric"] = query["measure"] or "value"

    elif task == "distinct_issuers":
        intent["action"] = "distinct_issuers"
        intent["metric"] = "count"

    elif task == "portfolio_total":
        intent["action"] = "sum"
        intent["metric"] = "value"

    elif task == "holding_total":
        intent["action"] = "sum"
        intent["metric"] = query["measure"] or "value"

    elif task == "holding_exists":
        intent["action"] = "holding_presence"

    elif task == "filing_type":
        intent["action"] = "filing_type"

    elif task == "count_positions":
        intent["action"] = "count_positions"
        intent["metric"] = "count"

    elif task == "option_count":
        intent["action"] = "option_count"
        intent["metric"] = "count"

    elif task == "compare_position":
        intent["action"] = "quarter_comparison"
        intent["metric"] = query["measure"] or "value"

    elif task == "average_portfolio":
        intent["action"] = "average"
        intent["metric"] = "value"

    return intent

def load_data():
    """Load both parquet files as Python rows."""
    filings = pq.read_table(FILINGS).to_pylist()
    holdings = pq.read_table(HOLDINGS).to_pylist()
    return filings, holdings

def normalize_name(text: str) -> str:
    """Normalize names for matching."""
    if text is None:
        return ""

    text = text.lower()

    for char in [".", ",", "'", '"', "-", "&"]:
        text = text.replace(char, " ")

    words = text.split()

    common_suffixes = {
        "llc",
        "lp",
        "l",
        "p",
        "inc",
        "plc",
        "corp",
        "corporation",
        "company",
        "co",
    }

    words = [
        word
        for word in words
        if word not in common_suffixes
    ]

    return " ".join(words)

def resolve_manager(manager: str, filings: list) -> str | None:
    """Resolve a model-produced manager name to the roster name."""
    if manager is None:
        return None

    target = normalize_name(manager)

    fund_names = []
    seen = set()

    for filing in filings:
        name = filing["fund_name"]

        if name not in seen:
            seen.add(name)
            fund_names.append(name)

    # Exact normalized match.
    for name in fund_names:
        if normalize_name(name) == target:
            return name

    # Allow a clear shortened name such as "Renaissance".
    matches = []

    for name in fund_names:
        normalized = normalize_name(name)

        if target in normalized or normalized in target:
            matches.append(name)

    if len(matches) == 1:
        return matches[0]

    return None

ISSUER_ALIASES = {
    "google": ["alphabet"],
    "alphabet": ["google"],
    "facebook": ["meta platforms", "meta"],
    "meta": ["meta platforms", "facebook"],
}


def normalize_issuer(text: str) -> str:
    """Normalize issuer names for matching."""
    if text is None:
        return ""

    text = text.lower()

    for char in [".", ",", "'", '"', "-", "&", "/"]:
        text = text.replace(char, " ")

    words = text.split()

    common_words = {
        "inc",
        "incorporated",
        "corp",
        "corporation",
        "company",
        "co",
        "ltd",
        "limited",
        "plc",
        "class",
        "cl",
        "common",
        "com",
    }

    words = [
        word
        for word in words
        if word not in common_words
    ]

    return " ".join(words)


def issuer_matches(name: str, issuer: str) -> bool:
    """Match a user-facing issuer name to a reported SEC issuer name."""
    if name is None or issuer is None:
        return False

    reported = normalize_issuer(name)
    target = normalize_issuer(issuer)

    if not reported or not target:
        return False

    # Exact normalized match.
    if reported == target:
        return True

    # Common public-name aliases.
    aliases = ISSUER_ALIASES.get(target, [])

    for alias in aliases:
        alias = normalize_issuer(alias)

        if alias == reported:
            return True

    return False

def get_filing_by_accession(filings: list[dict]) -> dict:
    return {
        f["accession_number"]: f
        for f in filings
    }

def filter_holdings(
    holdings: list[dict],
    filings: list[dict],
    *,
    quarter: str | None = None,
    quarters: set[str] | None = None,
    manager: str | None = None,
    issuer: str | None = None,
    option_type: str | None = None,
) -> list[dict]:
    """
    SQL-like WHERE + JOIN.

    Returns holding rows with fund_name attached.
    """
    filing_map = get_filing_by_accession(filings)

    rows = []

    for row in holdings:
        if quarter is not None and row["report_quarter"] != quarter:
            continue

        if quarters is not None and row["report_quarter"] not in quarters:
            continue

        if issuer is not None and not issuer_matches(
            row["name_of_issuer"],
            issuer,
        ):
            continue

        if option_type is not None:
            reported_option = row["put_call"]

            if (
                reported_option is None
                or reported_option.strip().lower() != option_type.lower()
            ):
                continue

        filing = filing_map.get(row["accession_number"])

        if filing is None:
            continue

        if manager is not None and filing["fund_name"] != manager:
            continue

        new_row = dict(row)
        new_row["fund_name"] = filing["fund_name"]

        rows.append(new_row)

    return rows


def aggregate_by_manager(
    rows: list[dict],
    *,
    metric: str,
) -> dict:
    """
    SQL-like GROUP BY manager.

    Keeps both aggregate values and source accession numbers.
    """
    if metric == "value":
        field = "value"
    elif metric == "shares":
        field = "ssh_prnamt"
    else:
        return {}

    result = {}

    for row in rows:
        manager = row["fund_name"]

        if manager not in result:
            result[manager] = {
                "value": 0,
                "sources": set(),
            }

        result[manager]["value"] += row[field]
        result[manager]["sources"].add(
            row["accession_number"]
        )

    return result


def aggregate_by_manager_quarter(
    rows: list[dict],
    *,
    metric: str,
) -> dict:
    """
    SQL-like GROUP BY manager, quarter.
    """
    if metric == "value":
        field = "value"
    elif metric == "shares":
        field = "ssh_prnamt"
    else:
        return {}

    result = {}

    for row in rows:
        manager = row["fund_name"]
        quarter = row["report_quarter"]

        if manager not in result:
            result[manager] = {}

        if quarter not in result[manager]:
            result[manager][quarter] = {
                "value": 0,
                "sources": set(),
            }

        result[manager][quarter]["value"] += row[field]
        result[manager][quarter]["sources"].add(
            row["accession_number"]
        )

    return result

def filter_filings(
    filings: list[dict],
    *,
    quarter: str | None = None,
    manager: str | None = None,
    form_types: set[str] | None = None,
) -> list[dict]:
    """
    SQL-like WHERE for filing-level queries.
    """
    rows = []

    for filing in filings:
        if (
            quarter is not None
            and filing["report_quarter"] != quarter
        ):
            continue

        if (
            manager is not None
            and filing["fund_name"] != manager
        ):
            continue

        if (
            form_types is not None
            and filing["form_type"] not in form_types
        ):
            continue

        rows.append(filing)

    return rows

def get_scope_sources(
    filings: list[dict],
    *,
    quarter: str,
    manager: str | None = None,
) -> list[str]:
    matches = filter_filings(
        filings,
        quarter=quarter,
        manager=manager,
    )

    return sorted({
        filing["accession_number"]
        for filing in matches
    })

def get_manager_quarter_sources(
    filings: list[dict],
    manager: str,
    quarters: set[str],
) -> list[str]:
    matches = [
        filing
        for filing in filings
        if filing["fund_name"] == manager
        and filing["report_quarter"] in quarters
    ]

    return sorted({
        filing["accession_number"]
        for filing in matches
    })

def filter_metric_rows(
    rows: list[dict],
    metric: str,
) -> list[dict]:
    if metric == "value":
        return rows

    if metric == "shares":
        return [
            row
            for row in rows
            if row["ssh_prnamt_type"] == "SH"
        ]

    return []

def answer_intent(intent: dict) -> dict:
    """Compute the answer deterministically from the parquet files."""
    filings, holdings = load_data()

    action = intent["action"]
    quarter = intent["quarter"]
    issuer = intent["issuer"]
    manager = resolve_manager(
        intent["manager"],
        filings,
    )

    # Reject quarters outside this dataset.
    valid_quarters = {"2026Q1", "2026Q2"}

    if quarter is not None and quarter not in valid_quarters:
        print(
            f"Unsupported quarter: {quarter}",
            file=sys.stderr,
        )
        return {
            "answer": None,
            "unit": "NONE",
            "sources": [],
        }

    # 1. Which manager had the largest position
    # in a given issuer and quarter?
    if action == "largest_position":
        metric = intent["metric"]

        rows = filter_holdings(
            holdings,
            filings,
            quarter=quarter,
            issuer=issuer,
        )

        rows = filter_metric_rows(
            rows,
            metric,
        )

        if not rows:
            return {
                "answer": None,
                "unit": "NONE",
                "sources": [],
            }

        if metric == "value":
            field = "value"
        elif metric == "shares":
            field = "ssh_prnamt"
        else:
            return {
                "answer": None,
                "unit": "NONE",
                "sources": [],
            }

        largest = max(
            rows,
            key=lambda row: (
                row[field],
                row["fund_name"],
                row["accession_number"],
            ),
        )

        return {
            "answer": largest["fund_name"],
            "unit": "NAME",
            "sources": [largest["accession_number"]],
        }

    # 2. How many distinct issuers did one manager report?
    if action == "distinct_issuers":
        rows = filter_holdings(
            holdings,
            filings,
            quarter=quarter,
            manager=manager,
        )

        if not rows:
            scope_sources = get_scope_sources(
                filings,
                quarter=quarter,
                manager=manager,
            )

            if not scope_sources:
                return {
                    "answer": None,
                    "unit": "NONE",
                    "sources": [],
                }

            return {
                "answer": 0,
                "unit": "COUNT",
                "sources": scope_sources,
            }

        issuers = {
            row["name_of_issuer"]
            for row in rows
        }

        return {
            "answer": len(issuers),
            "unit": "COUNT",
            "sources": sorted({
                row["accession_number"]
                for row in rows
            }),
        }


    # 3. What was one manager's total reported portfolio value?
    if action == "filing_total":
        matches = [
            f for f in filings
            if f["fund_name"] == manager
            and f["report_quarter"] == quarter
        ]

        if not matches:
            return {
                "answer": None,
                "unit": "NONE",
                "sources": [],
            }

        filing = matches[0]

        return {
            "answer": filing["table_value_total"],
            "unit": "USD",
            "sources": [filing["accession_number"]],
        }

    # 4. Which managers filed a given 13F form type?
    if action == "filing_type":
        requested_form = intent.get("form_type")

        if requested_form not in {"13F-NT", "13F-HR"}:
            return {
                "answer": None,
                "unit": "NONE",
                "sources": [],
            }

        accepted_forms = {
            requested_form,
            f"{requested_form}/A",
        }

        matches = filter_filings(
            filings,
            quarter=quarter,
            form_types=accepted_forms,
        )

        if not matches:
            return {
                "answer": [],
                "unit": "NAME",
                "sources": [],
            }

        return {
            "answer": [
                f["fund_name"]
                for f in matches
            ],
            "unit": "NAME",
            "sources": [
                f["accession_number"]
                for f in matches
            ],
        }

    # 5. Which manager changed a named issuer the most between two quarters?
    if action == "largest_change":
        from_quarter = intent["from_quarter"]
        to_quarter = intent["to_quarter"]
        metric = intent["metric"]

        if (
            from_quarter not in valid_quarters
            or to_quarter not in valid_quarters
            or from_quarter == to_quarter
        ):
            return {
                "answer": None,
                "unit": "NONE",
                "sources": [],
            }

        if metric not in {"value", "shares"}:
            return {
                "answer": None,
                "unit": "NONE",
                "sources": [],
            }

        rows = filter_holdings(
            holdings,
            filings,
            quarters={from_quarter, to_quarter},
            issuer=issuer,
        )

        rows = filter_metric_rows(
            rows,
            metric,
        )

        grouped = aggregate_by_manager_quarter(
            rows,
            metric=metric,
        )

        require_both = intent.get(
            "require_holding_both_quarters",
            False,
        )

        candidates = []

        for fund_name, quarter_data in grouped.items():
            before_data = quarter_data.get(from_quarter)
            after_data = quarter_data.get(to_quarter)

            before = (
                before_data["value"]
                if before_data is not None
                else 0
            )

            after = (
                after_data["value"]
                if after_data is not None
                else 0
            )

            if require_both and (
                before <= 0
                or after <= 0
            ):
                continue

            sources = get_manager_quarter_sources(
                filings,
                fund_name,
                {from_quarter, to_quarter},
            )

            candidates.append({
                "manager": fund_name,
                "change": after - before,
                "sources": sources,
            })

        if not candidates:
            return {
                "answer": None,
                "unit": "NONE",
                "sources": [],
            }

        change_direction = intent.get("change_direction") or "increase"

        if change_direction == "increase":
            best = max(
                candidates,
                key=lambda item: (
                    item["change"],
                    item["manager"],
                ),
            )

        elif change_direction == "decrease":
            best = min(
                candidates,
                key=lambda item: (
                    item["change"],
                    item["manager"],
                ),
            )

        elif change_direction == "absolute":
            best = max(
                candidates,
                key=lambda item: (
                    abs(item["change"]),
                    item["manager"],
                ),
            )

        else:
            return {
                "answer": None,
                "unit": "NONE",
                "sources": [],
            }

        return {
            "answer": best["manager"],
            "unit": "NAME",
            "sources": sorted(best["sources"]),
        }

    # 6. Did one manager report an issuer directly?
    if action == "holding_presence":
        filing_matches = filter_filings(
            filings,
            quarter=quarter,
            manager=manager,
        )

        if len(filing_matches) != 1:
            return {
                "answer": None,
                "unit": "NONE",
                "sources": [],
            }

        accession = filing_matches[0]["accession_number"]

        found = any(
            row["accession_number"] == accession
            and issuer_matches(
                row["name_of_issuer"],
                issuer,
            )
            for row in holdings
        )

        return {
            "answer": "yes" if found else "no",
            "unit": "NONE",
            "sources": [accession],
        }

    # 7. Which manager reported the most call or put options?
    if action == "option_count":
        option_type = intent["option_type"]

        if option_type not in {"Call", "Put"}:
            return {
                "answer": None,
                "unit": "NONE",
                "sources": [],
            }

        rows = filter_holdings(
            holdings,
            filings,
            quarter=quarter,
            option_type=option_type,
        )

        if not rows:
            return {
                "answer": None,
                "unit": "NONE",
                "sources": [],
            }

        counts = {}

        for row in rows:
            manager_name = row["fund_name"]

            if manager_name not in counts:
                counts[manager_name] = {
                    "count": 0,
                    "sources": set(),
                }

            counts[manager_name]["count"] += 1
            counts[manager_name]["sources"].add(
                row["accession_number"]
            )

        best_manager = max(
            counts,
            key=lambda name: (
                counts[name]["count"],
                name,
            ),
        )

        return {
            "answer": best_manager,
            "unit": "NAME",
            "sources": sorted(
                counts[best_manager]["sources"]
            ),
        }

    # 8. What was one manager's largest position?
    if action == "manager_largest_position":
        rows = filter_holdings(
            holdings,
            filings,
            quarter=quarter,
            manager=manager,
        )

        if not rows:
            return {
                "answer": None,
                "unit": "NONE",
                "sources": [],
            }

        largest = max(
            rows,
            key=lambda row: (
                row["value"],
                row["name_of_issuer"],
            ),
        )

        return {
            "answer": [
                largest["name_of_issuer"],
                largest["value"],
            ],
            "unit": "USD",
            "sources": [largest["accession_number"]],
        }

    # 9. Which managers held an issuer in both quarters, and did each position grow or shrink?
    if action == "quarter_comparison":
        metric = intent["metric"]
        from_quarter = intent["from_quarter"]
        to_quarter = intent["to_quarter"]

        if metric not in {"value", "shares"}:
            return {
                "answer": None,
                "unit": "NONE",
                "sources": [],
            }

        rows = filter_holdings(
            holdings,
            filings,
            quarters={from_quarter, to_quarter},
            manager=manager,
            issuer=issuer,
        )

        rows = filter_metric_rows(
            rows,
            metric,
        )

        grouped = aggregate_by_manager_quarter(
            rows,
            metric=metric,
        )

        if not grouped:
            return {
                "answer": None,
                "unit": "NONE",
                "sources": [],
            }

        results = []
        all_sources = set()

        for fund_name, quarter_data in grouped.items():
            before_data = quarter_data.get(from_quarter)
            after_data = quarter_data.get(to_quarter)

            # compare_position means the holding must exist
            # in both quarters.
            if before_data is None or after_data is None:
                continue

            before = before_data["value"]
            after = after_data["value"]

            if before <= 0 or after <= 0:
                continue

            if after > before:
                direction = "grow"
            elif after < before:
                direction = "shrink"
            else:
                direction = "unchanged"

            sources = get_manager_quarter_sources(
                filings,
                fund_name,
                {from_quarter, to_quarter},
            )

            # If one manager was explicitly named,
            # return only that manager's direction.
            if manager is not None:
                return {
                    "answer": direction,
                    "unit": "NONE",
                    "sources": sorted(sources),
                }

            results.append(
                f"{fund_name}: {direction}"
            )
            all_sources.update(sources)

        if not results:
            return {
                "answer": None,
                "unit": "NONE",
                "sources": [],
            }

        return {
            "answer": sorted(results),
            "unit": "NAME",
            "sources": sorted(all_sources),
        }

    # Sum reported value or shares.
    if action == "sum":
        metric = intent["metric"]

        if metric not in {"value", "shares"}:
            return {
                "answer": None,
                "unit": "NONE",
                "sources": [],
            }

        # Whole portfolio total:
        # use the filing-level total because it is the reported portfolio total.
        if manager is not None and issuer is None and metric == "value":
            matches = filter_filings(
                filings,
                quarter=quarter,
                manager=manager,
            )

            if len(matches) != 1:
                return {
                    "answer": None,
                    "unit": "NONE",
                    "sources": [],
                }

            filing = matches[0]

            if filing["table_value_total"] is None:
                return {
                    "answer": None,
                    "unit": "NONE",
                    "sources": [],
                }

            return {
                "answer": filing["table_value_total"],
                "unit": "USD",
                "sources": [filing["accession_number"]],
            }

        rows = filter_holdings(
            holdings,
            filings,
            quarter=quarter,
            manager=manager,
            issuer=issuer,
        )

        rows = filter_metric_rows(
            rows,
            metric,
        )

        if not rows:
            scope_sources = get_scope_sources(
                filings,
                quarter=quarter,
                manager=manager,
            )

            if not scope_sources:
                return {
                    "answer": None,
                    "unit": "NONE",
                    "sources": [],
                }

            return {
                "answer": 0,
                "unit": "USD" if metric == "value" else "SHARES",
                "sources": scope_sources,
            }

        if metric == "value":
            answer = sum(
                row["value"]
                for row in rows
            )
            unit = "USD"

        else:
            answer = sum(
                row["ssh_prnamt"]
                for row in rows
            )
            unit = "SHARES"

        return {
            "answer": answer,
            "unit": unit,
            "sources": sorted({
                row["accession_number"]
                for row in rows
            }),
        }

    # Count reported holding rows.
    if action == "count_positions":
        rows = filter_holdings(
            holdings,
            filings,
            quarter=quarter,
            manager=manager,
            issuer=issuer,
        )

        if not rows:
            scope_sources = get_scope_sources(
                filings,
                quarter=quarter,
                manager=manager,
            )

            if not scope_sources:
                return {
                    "answer": None,
                    "unit": "NONE",
                    "sources": [],
                }

            return {
                "answer": 0,
                "unit": "COUNT",
                "sources": scope_sources,
            }

        return {
            "answer": len(rows),
            "unit": "COUNT",
            "sources": sorted({
                row["accession_number"]
                for row in rows
            }),
        }

    # Average portfolio value across all managers.
    if action == "average":
        metric = intent["metric"]

        if (
            metric != "value"
            or manager is not None
            or issuer is not None
        ):
            return {
                "answer": None,
                "unit": "NONE",
                "sources": [],
            }

        quarter_filings = [
            f for f in filings
            if f["report_quarter"] == quarter
        ]

        if not quarter_filings:
            return {
                "answer": None,
                "unit": "NONE",
                "sources": [],
            }

        # The question asks across all managers.
        # If any manager has no reported portfolio total,
        # the dataset does not support that average.
        if any(
            f["table_value_total"] is None
            for f in quarter_filings
        ):
            print(
                "Average across all managers is unsupported "
                "because at least one filing has no portfolio total.",
                file=sys.stderr,
            )

            return {
                "answer": None,
                "unit": "NONE",
                "sources": [],
            }

        answer = sum(
            f["table_value_total"]
            for f in quarter_filings
        ) / len(quarter_filings)

        return {
            "answer": answer,
            "unit": "USD",
            "sources": sorted(
                f["accession_number"]
                for f in quarter_filings
            ),
        }

    print(
        f"Unsupported action: {action}",
        file=sys.stderr,
    )

    return {
        "answer": None,
        "unit": "NONE",
        "sources": [],
    }

def main(question: str) -> dict[str, Any]:
    """Answer `question` against your dataset.

    Do not rename this function or change its signature.
    """
    #raise NotImplementedError("Implement your agent here. See docs/04-serve.md.")

    """Answer a question against the 13F dataset."""
    try:
        query = parse_question_hybrid(question)

        if not validate_query(query):
            intent = {
                "action": "unsupported",
                "manager": None,
                "issuer": None,
                "quarter": query.get("quarter"),
                "from_quarter": query.get("from_quarter"),
                "to_quarter": query.get("to_quarter"),
                "metric": "none",
                "option_type": None,
                "form_type": None,
            }
        else:
            intent = query_to_intent(query)

        return answer_intent(intent)

    except Exception as exc:
        print(
            f"Could not answer question: {exc}",
            file=sys.stderr,
        )

        return {
            "answer": None,
            "unit": "NONE",
            "sources": [],
        }


def _cli() -> int:
    if len(sys.argv) < 2:
        print('usage: python -m agents.answer "your question"', file=sys.stderr)
        return 2

    result = main(sys.argv[1])

    # Validated here so a shape mistake surfaces while you can still fix it. The grader
    # parses stdout as JSON and reads exactly these three keys.
    if not isinstance(result, dict):
        print(f"main() must return a dict, got {type(result).__name__}", file=sys.stderr)
        return 1
    missing = {"answer", "unit", "sources"} - set(result)
    if missing:
        print(f"result missing key(s): {sorted(missing)}", file=sys.stderr)
        return 1
    if result["unit"] not in VALID_UNITS:
        print(f"unit must be one of {sorted(VALID_UNITS)}, got {result['unit']!r}",
              file=sys.stderr)
        return 1
    if not isinstance(result["sources"], list):
        print("sources must be a list of accession numbers", file=sys.stderr)
        return 1

    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
