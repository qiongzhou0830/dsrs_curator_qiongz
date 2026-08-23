import os
import re
from functools import lru_cache
from pathlib import Path

import pyarrow.parquet as pq

from agents.llm import complete_json


FILINGS = Path("output/filings.parquet")


TASKS = [
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
]


CLASSIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "task": {
            "type": "string",
            "enum": TASKS,
        },
        "issuer": {
            "type": ["string", "null"],
        },
    },
    "required": ["task", "issuer"],
    "additionalProperties": False,
}


ISSUER_SCHEMA = {
    "type": "object",
    "properties": {
        "issuer": {
            "type": ["string", "null"],
        }
    },
    "required": ["issuer"],
    "additionalProperties": False,
}


# Tasks where an issuer/security is normally required.
ISSUER_TASKS = {
    "largest_manager_position",
    "largest_change",
    "holding_total",
    "holding_exists",
    "compare_position",
}


def normalize_text(text: str) -> str:
    text = text.lower()
    text = text.replace("’", "'")
    text = text.replace("–", "-")
    text = text.replace("—", "-")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


@lru_cache(maxsize=1)
def manager_names() -> list[str]:
    """
    Read the actual manager roster from filings.parquet.

    Using the dataset itself avoids guessing whether a company mentioned
    in the question is a manager or an issuer.
    """
    if not FILINGS.exists():
        return []

    table = pq.read_table(FILINGS, columns=["fund_name"])
    names = []

    for name in table.column("fund_name").to_pylist():
        if name and name not in names:
            names.append(name)

    return names


def manager_variants(name: str) -> list[str]:
    """
    Produce conservative variants of a manager name.

    Example:
        "Third Point LLC"
        -> "third point llc"
        -> "third point"

    We only strip common legal suffixes.
    """
    base = normalize_text(name)

    variants = {base}

    suffix_patterns = [
        r"\s+l\.?l\.?c\.?$",
        r"\s+l\.?p\.?$",
        r"\s+llc$",
        r"\s+lp$",
        r"\s+inc\.?$",
        r"\s+corp\.?$",
        r"\s+corporation$",
        r"\s+company$",
        r"\s+co\.?$",
        r"\s+ltd\.?$",
        r"\s+limited$",
    ]

    changed = True

    while changed:
        changed = False

        for value in list(variants):
            for pattern in suffix_patterns:
                shorter = re.sub(pattern, "", value).strip()

                if shorter and shorter != value and shorter not in variants:
                    variants.add(shorter)
                    changed = True

    return sorted(variants, key=len, reverse=True)

def manager_aliases() -> dict[str, str]:
    """
    Build conservative short aliases from the actual manager roster.

    Only keep an alias when it identifies exactly one manager.
    """
    candidates = {}

    for manager in manager_names():
        variants = manager_variants(manager)

        if not variants:
            continue

        shortest = min(variants, key=len)

        cleaned = re.sub(
            r"[^a-z0-9]+",
            " ",
            shortest,
        ).strip()

        words = cleaned.split()

        # Leading "the" is an article, not a meaningful manager alias.
        if words and words[0] == "the":
            words = words[1:]

        if not words:
            continue

        for end in range(1, len(words) + 1):
            alias = " ".join(words[:end])

            # Avoid extremely short/noisy aliases.
            if len(alias) < 3:
                continue

            candidates.setdefault(
                alias,
                set(),
            ).add(manager)

    aliases = {}

    for alias, managers in candidates.items():
        if len(managers) == 1:
            aliases[alias] = next(iter(managers))

    return aliases

def extract_require_holding_both_quarters(
    question: str,
) -> bool:
    text = normalize_text(question)

    patterns = [
        r"\bheld\b.*\bin both quarters\b",
        r"\bholding\b.*\bin both quarters\b",
        r"\bowned\b.*\bin both quarters\b",
        r"\bheld\b.*\bacross both quarters\b",
        r"\bholding\b.*\bacross both quarters\b",
        r"\bowned\b.*\bacross both quarters\b",
        r"\bcontinued holding\b",
        r"\bcontinued to hold\b",
    ]

    return any(
        re.search(pattern, text)
        for pattern in patterns
    )

def extract_manager(question: str) -> str | None:
    """
    Match managers against the actual manager roster.

    Prefer full/legal-suffix variants first, then fall back to a
    roster-derived short alias only when that alias is unique.
    """
    text = normalize_text(question)

    matches = []

    # First: conservative full-name / suffix-stripped matching.
    for manager in manager_names():
        for variant in manager_variants(manager):
            pattern = r"(?<!\w)" + re.escape(variant) + r"(?!\w)"

            if re.search(pattern, text):
                matches.append((len(variant), manager))
                break

    if matches:
        matches.sort(reverse=True)

        longest_length = matches[0][0]

        longest = {
            manager
            for length, manager in matches
            if length == longest_length
        }

        if len(longest) == 1:
            return next(iter(longest))

        return None

    # Second: unique short aliases derived from the same roster.
    cleaned_text = re.sub(
        r"[^a-z0-9]+",
        " ",
        text,
    ).strip()

    alias_matches = []

    for alias, manager in manager_aliases().items():
        pattern = r"(?<!\w)" + re.escape(alias) + r"(?!\w)"

        if re.search(pattern, cleaned_text):
            alias_matches.append(
                (len(alias), manager)
            )

    if not alias_matches:
        return None

    # Prefer the longest matching alias.
    alias_matches.sort(reverse=True)

    longest_length = alias_matches[0][0]

    longest_managers = {
        manager
        for length, manager in alias_matches
        if length == longest_length
    }

    if len(longest_managers) == 1:
        return next(iter(longest_managers))

    return None


def extract_quarters(question: str) -> list[str]:
    """
    Extract quarters as YYYYQn.

    Handles:
        2026 Q1
        Q1 2026
        2026Q1
        from Q1 to Q2 2026
        between Q1 and Q2 in 2026
    """
    text = normalize_text(question)

    found = []

    # Direct forms: 2026 Q1 / 2026Q1
    for match in re.finditer(
        r"\b(20\d{2})\s*[-/]?\s*q([1-4])\b",
        text,
        flags=re.IGNORECASE,
    ):
        found.append(
            (
                match.start(),
                f"{match.group(1)}Q{match.group(2)}",
            )
        )

    # Reverse forms: Q1 2026
    for match in re.finditer(
        r"\bq([1-4])\s*[-/]?\s*(20\d{2})\b",
        text,
        flags=re.IGNORECASE,
    ):
        found.append(
            (
                match.start(),
                f"{match.group(2)}Q{match.group(1)}",
            )
        )

    # If the question contains one clear year, allow shorthand such as:
    # "from Q1 to Q2 in 2026".
    years = re.findall(r"\b(20\d{2})\b", text)
    unique_years = list(dict.fromkeys(years))

    if len(unique_years) == 1:
        year = unique_years[0]

        for match in re.finditer(
            r"\bq([1-4])\b",
            text,
            flags=re.IGNORECASE,
        ):
            value = f"{year}Q{match.group(1)}"

            if not any(
                position == match.start() and quarter == value
                for position, quarter in found
            ):
                found.append((match.start(), value))

    found.sort(key=lambda item: item[0])

    quarters = []

    for _, quarter in found:
        if quarter not in quarters:
            quarters.append(quarter)

    return quarters


def extract_measure(question: str) -> str | None:
    text = normalize_text(question)

    if re.search(r"\bshares?\b", text):
        return "shares"

    value_terms = [
        "value",
        "dollar",
        "dollars",
        "portfolio value",
        "exposure",
        "$",
    ]

    if any(term in text for term in value_terms):
        return "value"

    return None

def extract_change_direction(question: str) -> str | None:
    """
    Determine what kind of cross-quarter change the question asks for.
    """
    text = normalize_text(question)

    paired_patterns = [
        r"\bincrease or decrease\b",
        r"\bincreased or decreased\b",
        r"\bgrow or shrink\b",
        r"\bgrew or shrank\b",
        r"\bgrow or shrank\b",
        r"\bgrew or shrink\b",
    ]

    if any(re.search(pattern, text) for pattern in paired_patterns):
        return None

    decrease_patterns = [
        r"\bdecrease\b",
        r"\bdecreased\b",
        r"\bdecreasing\b",
        r"\bdecline\b",
        r"\bdeclined\b",
        r"\bdrop\b",
        r"\bdropped\b",
        r"\breduce\b",
        r"\breduced\b",
        r"\bshrink\b",
        r"\bshrank\b",
        r"\bsold\b.*\bmost\b",
    ]

    if any(re.search(pattern, text) for pattern in decrease_patterns):
        return "decrease"

    increase_patterns = [
        r"\bincrease\b",
        r"\bincreased\b",
        r"\bincreasing\b",
        r"\bgrow\b",
        r"\bgrew\b",
        r"\badd\b",
        r"\badded\b",
        r"\badding\b",
        r"\bbought\b.*\bmost\b",
        r"\baccumulate\b",
        r"\baccumulated\b",
    ]

    if any(re.search(pattern, text) for pattern in increase_patterns):
        return "increase"

    absolute_patterns = [
        r"\bchanged\b.*\bmost\b",
        r"\bchange\b.*\bmost\b",
        r"\blargest change\b",
        r"\bbiggest change\b",
        r"\bgreatest change\b",
    ]

    if any(re.search(pattern, text) for pattern in absolute_patterns):
        return "absolute"

    return None


def extract_option_type(question: str) -> str | None:
    text = normalize_text(question)

    if re.search(r"\bcalls?\b", text):
        return "Call"

    if re.search(r"\bputs?\b", text):
        return "Put"

    return None


def extract_form_type(question: str) -> str | None:
    text = normalize_text(question)

    if re.search(r"\b13f[\s-]*nt\b", text):
        return "13F-NT"

    if re.search(r"\b13f[\s-]*hr\b", text):
        return "13F-HR"

    return None


def classification_messages(
    question: str,
    *,
    manager: str | None,
    quarters: list[str],
    measure: str | None,
    option_type: str | None,
    form_type: str | None,
) -> list[dict[str, str]]:
    """
    Prompt used by the real semantic classifier.

    Keep this function separate so local tests can use exactly the same
    semantic prompt as grading.
    """
    return [
        {
            "role": "system",
            "content": (
                "You classify natural-language questions about SEC Form 13F data. "
                "Do not calculate the answer. "
                "The program has already extracted manager names, quarters, "
                "measure, option type, and filing type deterministically. "
                "Treat those extracted fields as reliable context. "
                "Your job is to identify the semantic task and the issuer/security "
                "being held when the task requires one. "

                "Task meanings: "

                "largest_manager_position = compare managers in one quarter for "
                "one named issuer/security and identify the manager with the "
                "largest position. "

                "largest_change = compare managers by the amount one named "
                "issuer/security changed between two quarters and identify the "
                "manager with the greatest increase or decrease. "

                "manager_largest_holding = search inside one specific manager's "
                "portfolio and return that manager's largest holding. "

                "distinct_issuers = count distinct or unique issuers reported by "
                "one specific manager. "

                "portfolio_total = calculate the total reported portfolio value "
                "for one specific manager in one quarter. "

                "holding_total = calculate the value or shares of one named "
                "issuer/security, possibly restricted to one specific manager. "

                "holding_exists = determine whether one specific manager directly "
                "reported a named issuer/security. "

                "filing_type = identify manager or managers that filed a specified "
                "13F form type. "

                "count_positions = count reported positions for a manager. "

                "option_count = compare managers by the number of reported Call "
                "or Put option positions. "

                "compare_position = compare the same named issuer/security across "
                "two quarters and report which managers held it and whether each "
                "position grew, shrank, or stayed unchanged. This task is about "
                "the direction or status of positions, not ranking managers by "
                "who changed the most. "

                "average_portfolio = calculate average portfolio value across "
                "managers. "

                "unsupported = the requested answer cannot be obtained from the "
                "available 13F filings, such as market price, investment return, "
                "prediction, future holdings, performance, or another quarter "
                "outside the available dataset. "

                "Important semantic boundaries: "

                "If the question asks WHICH MANAGER had the largest holding in "
                "one named company, use largest_manager_position. "

                "If ONE MANAGER is named and the question asks what that manager's "
                "largest holding was, use manager_largest_holding. "

                "If the question ranks managers by how much a named holding "
                "increased or decreased between quarters, use largest_change. "

                "If the question asks which managers held a security across "
                "quarters and whether their positions grew, shrank, or stayed "
                "unchanged, use compare_position. "

                "portfolio_total means the whole manager portfolio. "
                "holding_total means one named security. "

                "holding_exists asks yes/no existence. "
                "holding_total asks for an amount. "

                "Issuer means the company, security, or investment being held. "
                "A fund or investment manager is never the issuer merely because "
                "its name appears in the question. "
                "If no issuer is needed for the selected task, return issuer=null."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Question: {question}\n"
                f"Detected manager: {manager or 'NONE'}\n"
                f"Detected quarters: "
                f"{', '.join(quarters) if quarters else 'NONE'}\n"
                f"Detected measure: {measure or 'NONE'}\n"
                f"Detected option type: {option_type or 'NONE'}\n"
                f"Detected form type: {form_type or 'NONE'}"
            ),
        },
    ]


def classify_with_llm(
    question: str,
    *,
    manager: str | None,
    quarters: list[str],
    measure: str | None,
    option_type: str | None,
    form_type: str | None,
) -> dict:
    messages = classification_messages(
        question,
        manager=manager,
        quarters=quarters,
        measure=measure,
        option_type=option_type,
        form_type=form_type,
    )

    return complete_json(
        messages,
        CLASSIFY_SCHEMA,
        max_tokens=160,
    )


def issuer_messages(
    question: str,
    *,
    task: str,
    manager: str | None,
    quarters: list[str],
) -> list[dict[str, str]]:
    """
    Used when Python knows the task with high confidence but the issuer
    still needs semantic extraction.
    """
    return [
        {
            "role": "system",
            "content": (
                "Extract the issuer or security being held from an SEC Form 13F "
                "question. Do not answer the question or calculate anything. "
                "The semantic task has already been determined by the program. "
                "A fund or investment manager is not the issuer. "
                "Return issuer=null only when the question does not name a "
                "specific issuer/security."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Task: {task}\n"
                f"Question: {question}\n"
                f"Detected manager: {manager or 'NONE'}\n"
                f"Detected quarters: "
                f"{', '.join(quarters) if quarters else 'NONE'}"
            ),
        },
    ]


def extract_issuer_with_llm(
    question: str,
    *,
    task: str,
    manager: str | None,
    quarters: list[str],
) -> str | None:
    result = complete_json(
        issuer_messages(
            question,
            task=task,
            manager=manager,
            quarters=quarters,
        ),
        ISSUER_SCHEMA,
        max_tokens=80,
    )

    issuer = result.get("issuer")

    if not isinstance(issuer, str):
        return None

    issuer = issuer.strip()

    if not issuer:
        return None

    # Defensive check: never allow the known manager name to come back
    # unchanged as the issuer.
    if manager is not None:
        normalized_issuer = normalize_text(issuer)

        if normalized_issuer in set(manager_variants(manager)):
            return None

    return issuer


def obvious_task(
    question: str,
    *,
    manager: str | None,
    quarters: list[str],
    option_type: str | None,
    form_type: str | None,
) -> str | None:
    """
    Only return a task when the wording makes it very high-confidence.

    Do NOT try to solve every semantic variation with regex.
    Ambiguous or unusual wording should go to the LLM classifier.
    """
    text = normalize_text(question)

    # Explicit filing type is highly reliable.
    if form_type is not None:
        return "filing_type"

    # Explicit distinct/unique issuer count.
    if (
        manager is not None
        and (
            "distinct issuer" in text
            or "distinct issuers" in text
            or "unique issuer" in text
            or "unique issuers" in text
        )
    ):
        return "distinct_issuers"

    # Explicit average portfolio request.
    if "average" in text and "portfolio" in text:
        return "average_portfolio"

    # Explicit Call/Put ranking.
    if (
        option_type is not None
        and any(
            phrase in text
            for phrase in [
                "most call",
                "most put",
                "largest number of call",
                "largest number of put",
                "highest number of call",
                "highest number of put",
            ]
        )
    ):
        return "option_count"

    # Explicit count of positions inside one known manager.
    if (
        manager is not None
        and any(
            phrase in text
            for phrase in [
                "how many positions",
                "number of positions",
                "count of positions",
            ]
        )
    ):
        return "count_positions"

    # Explicit whole-portfolio total.
    if (
        manager is not None
        and any(
            phrase in text
            for phrase in [
                "total reported value",
                "total portfolio value",
                "portfolio total",
            ]
        )
    ):
        return "portfolio_total"

    # Explicit largest holding inside one named manager.
    if (
        manager is not None
        and len(quarters) == 1
        and any(
            phrase in text
            for phrase in [
                "largest holding",
                "largest position",
                "biggest holding",
                "biggest position",
            ]
        )
    ):
        return "manager_largest_holding"

    # Very explicit cross-quarter direction question.
    if (
        len(quarters) >= 2
        and any(
            phrase in text
            for phrase in [
                "grow or shrink",
                "grew or shrank",
                "grew or shrink",
                "increase or decrease",
                "increased or decreased",
                "grew or decreased",
            ]
        )
    ):
        return "compare_position"

    # Compare all managers/funds for one issuer in one quarter.
    if (
        manager is None
        and len(quarters) == 1
        and (
            "among all managers" in text
            or "among managers" in text
            or "among all funds" in text
            or "among funds" in text
            or "which manager" in text
            or "which fund" in text
        )
        and any(
            word in text
            for word in [
                "largest",
                "biggest",
                "greatest",
                "most",
            ]
        )
        and any(
            word in text
            for word in [
                "held",
                "holding",
                "position",
                "owned",
                "exposure",
                "stake",
            ]
        )
    ):
        return "largest_manager_position"

    return None


def clean_issuer(
    issuer,
    manager: str | None,
) -> str | None:
    if not isinstance(issuer, str):
        return None

    issuer = issuer.strip()

    if not issuer:
        return None

    if manager is not None:
        normalized_issuer = normalize_text(issuer)

        if normalized_issuer in set(manager_variants(manager)):
            return None

    return issuer


def build_query(
    *,
    task: str,
    manager: str | None,
    issuer: str | None,
    quarters: list[str],
    measure: str | None,
    option_type: str | None,
    form_type: str | None,
    change_direction: str | None = None,
    require_holding_both_quarters: bool = False,
) -> dict:
    quarter = quarters[0] if quarters else None

    from_quarter = None
    to_quarter = None

    if len(quarters) >= 2:
        from_quarter = quarters[0]
        to_quarter = quarters[1]

    return {
        "task": task,
        "manager": manager,
        "issuer": issuer,
        "quarter": quarter,
        "from_quarter": from_quarter,
        "to_quarter": to_quarter,
        "measure": measure,
        "option_type": option_type,
        "form_type": form_type,
        "change_direction": change_direction,
        "require_holding_both_quarters":
            require_holding_both_quarters,
    }


def parse_question(question: str) -> dict:
    """
    Hybrid parser.

    1. Deterministically extract fields Python can identify reliably.
    2. Route only very obvious semantic tasks deterministically.
    3. Let the LLM handle genuinely semantic / paraphrased questions.
    4. Never let the LLM calculate the final answer.
    """
    require_holding_both_quarters = (
        extract_require_holding_both_quarters(question)
    )
    manager = extract_manager(question)
    quarters = extract_quarters(question)
    measure = extract_measure(question)
    change_direction = extract_change_direction(question)
    option_type = extract_option_type(question)
    form_type = extract_form_type(question)

    task = obvious_task(
        question,
        manager=manager,
        quarters=quarters,
        option_type=option_type,
        form_type=form_type,
    )

    # High-confidence deterministic semantic route.
    if task is not None:
        issuer = None

        if task in ISSUER_TASKS:
            if os.environ.get("LLM_MODE", "live").lower() != "mock":
                issuer = extract_issuer_with_llm(
                    question,
                    task=task,
                    manager=manager,
                    quarters=quarters,
                )

                # Recovery path:
                # if issuer-only extraction fails, ask the full semantic
                # classifier and use only its issuer.
                if issuer is None:
                    recovery = classify_with_llm(
                        question,
                        manager=manager,
                        quarters=quarters,
                        measure=measure,
                        option_type=option_type,
                        form_type=form_type,
                    )

                    issuer = clean_issuer(
                        recovery.get("issuer"),
                        manager,
                    )

        return build_query(
            task=task,
            manager=manager,
            issuer=issuer,
            quarters=quarters,
            measure=measure,
            option_type=option_type,
            form_type=form_type,
            change_direction=change_direction,
            require_holding_both_quarters=require_holding_both_quarters,
        )

    # Do not contact a model in mock mode.
    if os.environ.get("LLM_MODE", "live").lower() == "mock":
        return build_query(
            task="unsupported",
            manager=manager,
            issuer=None,
            quarters=quarters,
            measure=measure,
            option_type=option_type,
            form_type=form_type,
            change_direction=change_direction,
            require_holding_both_quarters=require_holding_both_quarters,
        )

    # Genuinely semantic question: ask the LLM for task + issuer.
    result = classify_with_llm(
        question,
        manager=manager,
        quarters=quarters,
        measure=measure,
        option_type=option_type,
        form_type=form_type,
    )

    task = result.get("task")

    if task not in TASKS:
        task = "unsupported"

    issuer = clean_issuer(
        result.get("issuer"),
        manager,
    )

    return build_query(
        task=task,
        manager=manager,
        issuer=issuer,
        quarters=quarters,
        measure=measure,
        option_type=option_type,
        form_type=form_type,
        change_direction=change_direction,
        require_holding_both_quarters=require_holding_both_quarters,
    )
