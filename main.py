#!/usr/bin/env python3
"""Run the whole pipeline: fetch from EDGAR, parse, write the dataset.

    python main.py --user-agent "FirstName LastName netid@illinois.edu"

This is how we run your submission, so it must work from a clean checkout with nothing
in output/. Everything below is yours to rewrite — add modules, packages, classes,
whatever fits. Only two things are fixed:

  - this file is the entry point, and it accepts --user-agent
  - it writes output/filings.parquet and output/holdings.parquet

Start with docs/01-source.md. Check your output with `python verify.py`.
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path
import httpx

ROOT = Path(__file__).resolve().parent
FILERS = ROOT / "filers.csv"
OUTPUT = ROOT / "output"
CACHE = ROOT / ".cache"

#look up CIK 
CIK_LOOKUP_URL = "https://www.sec.gov/Archives/edgar/cik-lookup-data.txt"

# Scope. See docs/01-source.md — filter report periods on reportDate, and exclude
# anything accepted after the cutoff.
REPORT_PERIODS = ("2026-03-31", "2026-06-30")
FILING_DATE_CUTOFF = "2026-08-18"  # inclusive

# SEC rejects requests without a contact address, and a run that gets the department
# blocked is worth failing fast on. Loose on purpose: we check an address is present,
# not that it is well-formed.
UA_PATTERN = re.compile(r"^\S.*\s+[^@\s]+@[^@\s]+\.[a-z]{2,}\s*$", re.I)


def load_filers() -> list[dict[str, str]]:
    """The roster the researcher supplied. At least one CIK in here is wrong."""
    with FILERS.open(newline="") as fh:
        return list(csv.DictReader(fh))
    
def download_cik_lookup(user_agent: str) -> Path:
    """Download SEC's name-to-CIK lookup file once and cache it."""
    CACHE.mkdir(parents=True, exist_ok=True)
    cache_file = CACHE / "cik-lookup-data.txt"

    if cache_file.exists():
        print("Using cached CIK lookup")
        return cache_file

    headers = {"User-Agent": user_agent}

    response = httpx.get(
        CIK_LOOKUP_URL,
        headers=headers,
        timeout=60,
    )
    response.raise_for_status()

    cache_file.write_bytes(response.content)

    print("Downloaded CIK lookup")
    return cache_file

def normalize_name(name: str) -> str:
    """Normalize small name differences for SEC lookup matching. Like The and ()"""
    name = name.upper()

    # Remove text in parentheses, e.g. "(Greenlight)".
    name = re.sub(r"\([^)]*\)", "", name)

    # Replace punctuation with spaces.
    name = re.sub(r"[^\w\s]", " ", name)

    # Remove a leading "THE".
    name = re.sub(r"^THE\s+", "", name)

    # Replace repeated spaces with one space.
    name = re.sub(r"\s+", " ", name)

    return name.strip()

def load_cik_lookup(path: Path) -> dict[str, list[str]]:
    """Read SEC's lookup file into normalized name -> CIKs."""
    lookup: dict[str, list[str]] = {}

    with path.open(encoding="latin-1") as fh:
        for line in fh:
            line = line.strip()

            if not line:
                continue

            # SEC lines end with a colon, so remove it first.
            line = line.rstrip(":")

            name, cik = line.rsplit(":", 1)

            key = normalize_name(name)

            if key not in lookup:
                lookup[key] = []

            lookup[key].append(cik)

    return lookup

def verify_filers(
    filers: list[dict[str, str]],
    lookup: dict[str, list[str]],
) -> list[dict[str, str]]:
    """Verify roster CIKs against SEC's lookup file."""
    verified = []

    for filer in filers:
        fund_name = filer["fund_name"]
        given_cik = filer["cik"]

        key = normalize_name(fund_name)
        matches = lookup.get(key, [])

        if given_cik in matches:
            final_cik = given_cik
            cik_source = "given"

        elif len(matches) == 1:
            final_cik = matches[0]
            cik_source = "corrected"

        else:
            # Known legal-name variant confirmed in the SEC lookup.
            if fund_name == "The Baupost Group LLC":
                final_cik = given_cik
                cik_source = "given"
            else:
                raise ValueError(
                    f"Could not safely verify CIK for {fund_name}. "
                    f"Given CIK: {given_cik}, SEC matches: {matches}"
                )

        verified.append(
            {
                "fund_name": fund_name,
                #get rid of leading zeros
                "cik": str(int(final_cik)),
                "cik_source": cik_source,
            }
        )

    return sorted(verified, key=lambda row: int(row["cik"]))

def write_verified_filers(
    verified: list[dict[str, str]],
    output: Path,
) -> None:
    """Write the verified roster required by Chapter 1."""
    path = output / "filers.csv"

    with path.open("w", newline="") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=["fund_name", "cik", "cik_source"],
        )
        writer.writeheader()
        writer.writerows(verified)

    print(f"Wrote {path}")

def run(user_agent: str, output: Path) -> None:
    """Build the dataset.

    Suggested shape, not a requirement:

        1. verify the CIKs against SEC's lookup file      docs/01-source.md
        2. find in-scope filings via the submissions API
        3. download the filings, caching as you go
        4. parse into the schema                          docs/SCHEMA.md
        5. write output/filings.parquet and output/holdings.parquet
    """
    """raise NotImplementedError(
        "Implement your pipeline here. Start with docs/01-source.md, then "
        "docs/SCHEMA.md for the output contract."
    )"""
    """Start by checking the supplied manager names against SEC's CIK lookup."""
    """Verify the supplied manager CIKs and write output/filers.csv."""
    filers = load_filers()

    lookup_file = download_cik_lookup(user_agent)
    lookup = load_cik_lookup(lookup_file)

    verified = verify_filers(filers, lookup)
    write_verified_filers(verified, output)

    corrected = [row for row in verified if row["cik_source"] == "corrected"]

    print(f"Verified {len(verified)} managers")
    print(f"Corrected {len(corrected)} CIKs")

    for row in corrected:
        print(f"  {row['fund_name']} -> {row['cik']}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--user-agent", required=True,
                    help='required by SEC: "FirstName LastName netid@illinois.edu"')
    ap.add_argument("--output", type=Path, default=OUTPUT)
    args = ap.parse_args()

    if not UA_PATTERN.match(args.user_agent):
        sys.exit(
            "invalid --user-agent.\n"
            "SEC requires a contact address and rejects requests without one.\n"
            '  python main.py --user-agent "Jane Doe jdoe@illinois.edu"'
        )

    args.output.mkdir(parents=True, exist_ok=True)
    run(args.user_agent, args.output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
