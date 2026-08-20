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
import os
import re
from agents.llm import complete_json, LLMError
import pyarrow.parquet as pq

OUTPUT = Path(__file__).resolve().parents[1] / "output"
FILINGS = OUTPUT / "filings.parquet"
HOLDINGS = OUTPUT / "holdings.parquet"

VALID_UNITS = {"USD", "SHARES", "COUNT", "PERCENT", "NAME", "DATE", "NONE"}

TASK_SCHEMA = {
    "type": "object",
    "properties": {
        "task": {
            "type": "string",
            "enum": [
                "largest_manager_position",
                "largest_change",
                "manager_largest_holding",
                "distinct_issuers",
                "portfolio_total",
                "holding_total",
                "holding_exists",
                "filing_type",
                "count_positions",
                "option_count",
                "compare_position",
                "average_portfolio",
                "unsupported",
            ],
        },
        "manager": {
            "type": ["string", "null"],
        },
        "issuer": {
            "type": ["string", "null"],
        },
        "quarter": {
            "type": ["string", "null"],
        },
        "from_quarter": {
            "type": ["string", "null"],
        },
        "to_quarter": {
            "type": ["string", "null"],
        },
        "measure": {
            "type": ["string", "null"],
            "enum": ["value", "shares", None],
        },
        "option_type": {
            "type": ["string", "null"],
            "enum": ["Call", "Put", None],
        },
        "form_type": {
            "type": ["string", "null"],
            "enum": ["13F-HR", "13F-NT", None],
        },
    },
    "required": [
        "task",
        "manager",
        "issuer",
        "quarter",
        "from_quarter",
        "to_quarter",
        "measure",
        "option_type",
        "form_type",
    ],
    "additionalProperties": False,
}

def parse_question_with_llm(question: str) -> dict:
    """Use the model only to classify the research task and extract fields."""
    messages = [
        {
            "role": "system",
            "content": (
                "You parse questions about SEC Form 13F data. "
                "Do not answer the question and do not calculate anything. "
                "Return only the structured task description. "

                "Choose exactly one task. "

                "largest_manager_position: find which manager reported the "
                "largest position in a named issuer for one quarter. "

                "largest_change: find which manager had the greatest increase "
                "or decrease in a named issuer between two quarters. "

                "manager_largest_holding: find the largest holding inside one "
                "named manager's portfolio for one quarter. "

                "distinct_issuers: count the distinct issuers reported by one "
                "manager in one quarter. "

                "portfolio_total: get one manager's total reported portfolio "
                "value for one quarter. "

                "holding_total: sum a named issuer's reported value or shares, "
                "optionally for one manager, in one quarter. "

                "holding_exists: determine whether one manager directly "
                "reported a named issuer in one quarter. "

                "filing_type: identify managers that filed a requested form "
                "such as 13F-NT or 13F-HR in one quarter. "

                "count_positions: count reported holding positions for one "
                "manager in one quarter. "

                "option_count: find which manager reported the most Call or "
                "Put positions in one quarter. "

                "compare_position: compare a named issuer between two quarters "
                "and determine whether the reported position grew, shrank, "
                "or stayed unchanged. A manager may be named or may be omitted. "

                "average_portfolio: average total portfolio values across "
                "managers for one quarter. "

                "unsupported: use this when the question requires information "
                "not contained in the 13F dataset, such as investment return, "
                "stock price, prediction, performance, or future information. "

                "Task distinction rules: "
                "If the question asks who, which manager, or which fund had the most, largest, "
                "highest, or greatest position in a named issuer, use "
                "largest_manager_position. "
                "manager_largest_holding is only for questions asking for the largest holding "
                "inside one specific named manager's portfolio. "

                "Manager extraction rules: "
                "If a specific fund or investment manager name appears anywhere in the question, "
                "put that name in manager. "
                "Possessive forms such as Citadel's, AQR's, Renaissance's, and Third Point's "
                "still refer to the manager. "
                "Do not omit a named manager just because the task also contains an issuer. "

                "Entity rules: manager is only a named investment manager or "
                "fund. Words like 'manager', 'fund', 'who', and 'all' are not "
                "manager names. issuer is only the company/security being held. "
                "Never copy an issuer into manager or a manager into issuer. "

                "Use quarter format exactly YYYYQ1 or YYYYQ2, for example "
                "2026Q1. Convert wording like 'Q1 2026' to '2026Q1'. "
                "For a two-quarter question fill from_quarter and to_quarter. "

                "Use measure='shares' only when the question explicitly asks "
                "about shares. Use measure='value' for dollar value, exposure, "
                "portfolio value, or position value. Otherwise use null. "

                "For option_type use exactly 'Call' or 'Put'. "
                "For form_type use exactly '13F-HR' or '13F-NT'. "
                "For every field that does not apply, return JSON null."
            ),
        },
        {
            "role": "user",
            "content": question,
        },
    ]

    return complete_json(
        messages,
        TASK_SCHEMA,
        max_tokens=300,
    )

def clean_query(query: dict) -> dict:
    """Normalize harmless model-output variations."""
    empty_values = {
        "none",
        "null",
        "unknown",
        "n/a",
        "",
    }

    for field in [
        "manager",
        "issuer",
        "quarter",
        "from_quarter",
        "to_quarter",
        "option_type",
        "form_type",
    ]:
        value = query.get(field)

        if (
            isinstance(value, str)
            and value.lower().strip() in empty_values
        ):
            query[field] = None

    quarter_map = {
        "q1 2026": "2026Q1",
        "q2 2026": "2026Q2",
        "q3 2026": "2026Q3",
        "2026 q1": "2026Q1",
        "2026 q2": "2026Q2",
        "2026 q3": "2026Q3",
    }

    for field in [
        "quarter",
        "from_quarter",
        "to_quarter",
    ]:
        value = query.get(field)

        if isinstance(value, str):
            normalized = value.strip().lower()

            if normalized in quarter_map:
                query[field] = quarter_map[normalized]

    return query

def normalize_query_semantics(query: dict) -> dict:
    """Fix internally inconsistent model output."""

    # manager_largest_holding means:
    # find the largest holding inside one specific manager's portfolio.
    #
    # If there is no named manager but there is a named issuer,
    # the question is instead asking which manager has the
    # largest position in that issuer.
    if (
        query.get("task") == "manager_largest_holding"
        and query.get("manager") is None
        and query.get("issuer") is not None
    ):
        query["task"] = "largest_manager_position"

    return query

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

def parse_question_mock(question: str) -> dict:
    """Simple local parser used only when LLM_MODE=mock."""
    q = question.lower()

    intent = {
        "action": "unsupported",
        "manager": None,
        "issuer": None,
        "quarter": None,
        "from_quarter": None,
        "to_quarter": None,
        "metric": "none",
        "option_type": None,
    }

    intent["manager"] = find_manager_in_question(question)

    # Extract quarter.
    if "2026 q1" in q:
        intent["quarter"] = "2026Q1"

    if "2026 q2" in q:
        intent["quarter"] = "2026Q2"

    if "2026 q3" in q:
        intent["quarter"] = "2026Q3"

    # A few common issuer names from the example questions.
    for issuer in ["apple", "nvidia", "microsoft", "tesla"]:
        if issuer in q:
            intent["issuer"] = issuer

    if "distinct issuers" in q:
        intent["action"] = "distinct_issuers"
        intent["metric"] = "count"

    elif "total reported value" in q:
        intent["action"] = "filing_total"
        intent["metric"] = "value"

    elif "13f-nt" in q:
        intent["action"] = "filing_type"

    elif "call options" in q:
        intent["action"] = "option_count"
        intent["metric"] = "count"
        intent["option_type"] = "Call"

    elif "added the most" in q:
        intent["action"] = "largest_change"
        intent["metric"] = "shares"
        intent["from_quarter"] = "2026Q1"
        intent["to_quarter"] = "2026Q2"
    
    elif "report any" in q or "holdings directly" in q:
        intent["action"] = "holding_presence"

    elif "largest position" in q and intent["manager"] is not None:
        intent["action"] = "manager_largest_position"
        intent["metric"] = "value"
    
    elif "both 2026 q1 and 2026 q2" in q:
        intent["action"] = "quarter_comparison"
        intent["metric"] = "value"
        intent["from_quarter"] = "2026Q1"
        intent["to_quarter"] = "2026Q2"
    
    elif "largest" in q and intent["issuer"] is not None:
        intent["action"] = "largest_position"
        intent["metric"] = "value"

    return intent

#helper
def find_manager_in_question(question: str):
    """Match a roster manager name mentioned in the question."""
    filings = pq.read_table(FILINGS).to_pylist()
    q = question.lower()

    seen = set()

    for row in filings:
        name = row["fund_name"]

        if name in seen:
            continue

        seen.add(name)

        full_name = name.lower()

        # Try the full roster name first.
        if full_name in q:
            return name

        # Also try a version without common legal suffixes.
        short_name = full_name

        for suffix in [
            " llc",
            " l.p.",
            " lp",
            " inc.",
            " plc",
        ]:
            short_name = short_name.replace(suffix, "")

        short_name = short_name.strip()

        if short_name and short_name in q:
            return name

    return None

def parse_question(question: str) -> dict:
    """Parse a question in live or mock mode."""
    if os.environ.get("LLM_MODE", "live").lower() == "mock":
        return parse_question_mock(question)

    query = parse_question_with_llm(question)
    query = clean_query(query)
    query = normalize_query_semantics(query)

    # Prefer deterministic manager matching from the known roster.
    matched_manager = find_manager_in_question(question)

    if matched_manager is not None:
        query["manager"] = matched_manager

    else:
        # The model sometimes copies an issuer into the manager field.
        if (
            query.get("manager") is not None
            and query.get("issuer") is not None
            and query["manager"].lower() == query["issuer"].lower()
        ):
            query["manager"] = None

        # For cross-manager issuer questions, a lone value mistakenly
        # placed in manager is really the issuer.
        if (
            query.get("task") in {
                "largest_manager_position",
                "largest_change",
                "compare_position",
            }
            and query.get("issuer") is None
            and query.get("manager") is not None
        ):
            query["issuer"] = query["manager"]
            query["manager"] = None

    # If a real manager is named and there is no issuer, the question
    # is asking for that manager's largest holding.
    if (
        query.get("task") == "largest_manager_position"
        and query.get("manager") is not None
        and query.get("issuer") is None
    ):
        query["task"] = "manager_largest_holding"

    if not validate_query(query):
        return {
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

    return query_to_intent(query)

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

    # Clear substring match.
    if target in reported or reported in target:
        return True

    # Common public-name aliases.
    aliases = ISSUER_ALIASES.get(target, [])

    for alias in aliases:
        alias = normalize_issuer(alias)

        if alias == reported or alias in reported:
            return True

    return False

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

        if metric == "value":
            value_field = "value"
        elif metric == "shares":
            value_field = "ssh_prnamt"
        else:
            return {
                "answer": None,
                "unit": "NONE",
                "sources": [],
            }

        totals = {}
        sources = {}

        filing_by_accession = {
            f["accession_number"]: f
            for f in filings
        }

        for row in holdings:
            if row["report_quarter"] != quarter:
                continue

            if not issuer_matches(
                row["name_of_issuer"],
                issuer,
            ):
                continue

            filing = filing_by_accession.get(
                row["accession_number"]
            )

            if filing is None:
                continue

            fund_name = filing["fund_name"]

            totals[fund_name] = (
                totals.get(fund_name, 0)
                + row[value_field]
            )

            sources.setdefault(
                fund_name,
                set(),
            ).add(row["accession_number"])

        if not totals:
            return {
                "answer": None,
                "unit": "NONE",
                "sources": [],
            }

        winner = max(
            totals,
            key=lambda name: (
                totals[name],
                name,
            ),
        )

        return {
            "answer": winner,
            "unit": "NAME",
            "sources": sorted(sources[winner]),
        }

    # 2. How many distinct issuers did one manager report?
    if action == "distinct_issuers":
        filing_matches = [
            f for f in filings
            if f["fund_name"] == manager
            and f["report_quarter"] == quarter
        ]

        if not filing_matches:
            return {
                "answer": None,
                "unit": "NONE",
                "sources": [],
            }

        accession = filing_matches[0]["accession_number"]

        issuers = {
            row["name_of_issuer"]
            for row in holdings
            if row["accession_number"] == accession
        }

        return {
            "answer": len(issuers),
            "unit": "COUNT",
            "sources": [accession],
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

    # 4. Which managers filed a notice?
    if action == "filing_type":
        matches = [
            f for f in filings
            if f["report_quarter"] == quarter
            and f["form_type"] in {"13F-NT", "13F-NT/A"}
        ]

        if not matches:
            return {
                "answer": [],
                "unit": "NAME",
                "sources": [],
            }

        return {
            "answer": [f["fund_name"] for f in matches],
            "unit": "NAME",
            "sources": [f["accession_number"] for f in matches],
        }
        
    # 5. Which manager added the most shares of an issuer between two quarters?
    if action == "largest_change":
        from_quarter = intent["from_quarter"]
        to_quarter = intent["to_quarter"]
        metric = intent["metric"]

        if (
            from_quarter not in valid_quarters
            or to_quarter not in valid_quarters
        ):
            return {
                "answer": None,
                "unit": "NONE",
                "sources": [],
            }

        if metric == "value":
            value_field = "value"
        elif metric == "shares":
            value_field = "ssh_prnamt"
        else:
            return {
                "answer": None,
                "unit": "NONE",
                "sources": [],
            }

        filing_by_accession = {
            f["accession_number"]: f
            for f in filings
        }

        # Start with every manager that has filings in both quarters.
        manager_values = {}

        for f in filings:
            if f["report_quarter"] not in {
                from_quarter,
                to_quarter,
            }:
                continue

            name = f["fund_name"]

            if name not in manager_values:
                manager_values[name] = {
                    from_quarter: 0,
                    to_quarter: 0,
                    "sources": {},
                }

            manager_values[name]["sources"][
                f["report_quarter"]
            ] = f["accession_number"]

        # Add the issuer's reported value/shares.
        for row in holdings:
            if row["report_quarter"] not in {
                from_quarter,
                to_quarter,
            }:
                continue

            if not issuer_matches(
                row["name_of_issuer"],
                issuer,
            ):
                continue

            filing = filing_by_accession.get(
                row["accession_number"]
            )

            if filing is None:
                continue

            name = filing["fund_name"]

            if name not in manager_values:
                continue

            manager_values[name][
                row["report_quarter"]
            ] += row[value_field]

        if not manager_values:
            return {
                "answer": None,
                "unit": "NONE",
                "sources": [],
            }

        best_manager = None
        best_change = None

        for name, values in manager_values.items():
            change = (
                values[to_quarter]
                - values[from_quarter]
            )

            if (
                best_change is None
                or change > best_change
                or (
                    change == best_change
                    and name < best_manager
                )
            ):
                best_change = change
                best_manager = name

        if best_manager is None:
            return {
                "answer": None,
                "unit": "NONE",
                "sources": [],
            }

        best_sources = sorted(
            manager_values[
                best_manager
            ]["sources"].values()
        )

        return {
            "answer": best_manager,
            "unit": "NAME",
            "sources": best_sources,
        }

    # 6. Did one manager report an issuer directly?
    if action == "holding_presence":
        filing_matches = [
            f for f in filings
            if f["fund_name"] == manager
            and f["report_quarter"] == quarter
        ]

        if not filing_matches:
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

        counts = {}

        for row in holdings:
            if row["report_quarter"] != quarter:
                continue

            if row["put_call"] != option_type:
                continue

            filing = next(
                f for f in filings
                if f["accession_number"] == row["accession_number"]
            )

            name = filing["fund_name"]

            if name not in counts:
                counts[name] = {
                    "count": 0,
                    "sources": set(),
                }

            counts[name]["count"] += 1
            counts[name]["sources"].add(
                row["accession_number"]
            )

        if not counts:
            return {
                "answer": None,
                "unit": "NONE",
                "sources": [],
            }

        best_manager = max(
            counts,
            key=lambda name: counts[name]["count"],
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
        filing_matches = [
            f for f in filings
            if f["fund_name"] == manager
            and f["report_quarter"] == quarter
        ]

        if not filing_matches:
            return {
                "answer": None,
                "unit": "NONE",
                "sources": [],
            }

        accession = filing_matches[0]["accession_number"]

        rows = [
            row for row in holdings
            if row["accession_number"] == accession
        ]

        if not rows:
            return {
                "answer": None,
                "unit": "NONE",
                "sources": [],
            }

        largest = max(
            rows,
            key=lambda row: row["value"],
        )

        return {
            "answer": [
                largest["name_of_issuer"],
                largest["value"],
            ],
            "unit": "USD",
            "sources": [accession],
        }
    
    # 9. Which managers held an issuer in both quarters, and did each position grow or shrink?
    if action == "quarter_comparison":
        metric = intent["metric"]
        from_quarter = intent["from_quarter"]
        to_quarter = intent["to_quarter"]

        if metric == "value":
            value_field = "value"
        elif metric == "shares":
            value_field = "ssh_prnamt"
        else:
            return {
                "answer": None,
                "unit": "NONE",
                "sources": [],
            }

        # If a manager was named, compare only that manager.
        if manager is not None:
            managers = [manager]
        else:
             managers = sorted({
                f["fund_name"]
                for f in filings
            })

        results = []
        all_sources = []

        for fund_name in managers:
            values = {}
            sources = []

            for q in [from_quarter, to_quarter]:
                filing_matches = [
                    f for f in filings
                    if f["fund_name"] == fund_name
                    and f["report_quarter"] == q
                ]

                if len(filing_matches) != 1:
                    values[q] = None
                    continue

                filing = filing_matches[0]
                sources.append(filing["accession_number"])

                rows = [
                    row for row in holdings
                    if row["accession_number"] == filing["accession_number"]
                    and issuer_matches(
                        row["name_of_issuer"],
                        issuer,
                    )
                ]

                values[q] = sum(
                    row[value_field]
                    for row in rows
                )

            if (
                values.get(from_quarter) is None
                or values.get(to_quarter) is None
            ):
                continue

            before = values[from_quarter]
            after = values[to_quarter]

            if after > before:
                direction = "grow"
            elif after < before:
                direction = "shrink"
            else:
                direction = "unchanged"

            # One named manager -> answer only the direction.
            if manager is not None:
                return {
                    "answer": direction,
                    "unit": "NONE",
                    "sources": sorted(set(sources)),
                }

            # No manager named -> return all matching managers Only include managers that reported the issuer in both quarters.
            if before > 0 and after > 0:
                results.append(
                    f"{fund_name}: {direction}"
                )
                all_sources.extend(sources)

        if not results:
            return {
                "answer": None,
                "unit": "NONE",
                "sources": [],
            }

        return {
            "answer": results,
            "unit": "NAME",
            "sources": sorted(set(all_sources)),
        }
    
    # Sum reported value or shares.
    if action == "sum":
        metric = intent["metric"]

        # Portfolio total for one manager.
        if manager is not None and issuer is None and metric == "value":
            matches = [
                f for f in filings
                if f["fund_name"] == manager
                and f["report_quarter"] == quarter
            ]

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

        # Otherwise sum matching holdings rows.
        matches = []

        for row in holdings:
            if row["report_quarter"] != quarter:
                continue

            filing = next(
                f for f in filings
                if f["accession_number"] == row["accession_number"]
            )

            if manager is not None and filing["fund_name"] != manager:
                continue

            if issuer is not None and not issuer_matches(
                row["name_of_issuer"],
                issuer,
            ):
                continue

            matches.append(row)

        if not matches:
            return {
                "answer": None,
                "unit": "NONE",
                "sources": [],
            }

        if metric == "value":
            answer = sum(row["value"] for row in matches)
            unit = "USD"

        elif metric == "shares":
            answer = sum(row["ssh_prnamt"] for row in matches)
            unit = "SHARES"

        else:
            return {
                "answer": None,
                "unit": "NONE",
                "sources": [],
            }

        return {
            "answer": answer,
            "unit": unit,
            "sources": sorted({
                row["accession_number"]
                for row in matches
            }),
        }
    
    # Count reported holding rows.
    if action == "count_positions":
        matches = []

        for row in holdings:
            if row["report_quarter"] != quarter:
                continue

            filing = next(
                f for f in filings
                if f["accession_number"] == row["accession_number"]
            )

            if manager is not None and filing["fund_name"] != manager:
                continue

            if issuer is not None and not issuer_matches(
                row["name_of_issuer"],
                issuer,
            ):
                continue

            matches.append(row)

        if not matches:
            return {
                "answer": 0,
                "unit": "COUNT",
                "sources": [],
            }

        return {
            "answer": len(matches),
            "unit": "COUNT",
            "sources": sorted({
                row["accession_number"]
                for row in matches
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
        intent = parse_question(question)
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
