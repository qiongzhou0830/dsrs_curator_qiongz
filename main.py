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
import json
import time

ROOT = Path(__file__).resolve().parent
FILERS = ROOT / "filers.csv"
OUTPUT = ROOT / "output"
CACHE = ROOT / ".cache"
SUBMISSIONS_CACHE = CACHE / "submissions"
LAST_REQUEST_TIME = 0.0
MIN_REQUEST_INTERVAL = 0.25  # about 4 requests/second

def sec_get(url: str, user_agent: str) -> httpx.Response:
    """Send a rate-limited SEC request with simple retry/backoff."""
    global LAST_REQUEST_TIME

    headers = {"User-Agent": user_agent}

    for attempt in range(3):
        elapsed = time.monotonic() - LAST_REQUEST_TIME

        if elapsed < MIN_REQUEST_INTERVAL:
            time.sleep(MIN_REQUEST_INTERVAL - elapsed)
        
        response = httpx.get(url, headers=headers, timeout=60)
        LAST_REQUEST_TIME = time.monotonic()

        if response.status_code not in {429, 500, 502, 503, 504}:
            response.raise_for_status()
            return response

        wait_time = 2 ** attempt
        print(
            f"SEC request failed with {response.status_code}; "
            f"retrying in {wait_time}s"
        )
        time.sleep(wait_time)

    response.raise_for_status()
    return response

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

#1.1
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
    
    response = sec_get(CIK_LOOKUP_URL, user_agent)

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

    manual_cik_overrides = {
        "Tudor Investment Corp": "0000923093",
    }

    for filer in filers:
        fund_name = filer["fund_name"]
        given_cik = filer["cik"]

        key = normalize_name(fund_name)
        matches = lookup.get(key, [])

        if fund_name in manual_cik_overrides:
            final_cik = manual_cik_overrides[fund_name]
            cik_source = "corrected"

        elif given_cik in matches:
            final_cik = given_cik
            cik_source = "given"

        elif len(matches) == 1:
            final_cik = matches[0]
            cik_source = "corrected"

        else:
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
                "cik": str(int(final_cik)),
                "cik_source": cik_source,
            })
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

#1.2
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"

def get_submissions(cik: str, user_agent: str) -> dict:
    """Get one manager's SEC submissions JSON, using a local cache."""
    SUBMISSIONS_CACHE.mkdir(parents=True, exist_ok=True)

    padded_cik = cik.zfill(10)
    cache_file = SUBMISSIONS_CACHE / f"CIK{padded_cik}.json"

    if cache_file.exists():
        print(f"Using cached submissions for {cik}")
        return json.loads(cache_file.read_text())

    url = SUBMISSIONS_URL.format(cik=padded_cik)
    response = sec_get(url, user_agent)

    cache_file.write_bytes(response.content)

    print(f"Downloaded submissions for {cik}")
    return response.json()

def find_filings(cik: str, user_agent: str) -> list[dict[str, str]]:
    """Find the in-scope 13F filings for one manager."""
    data = get_submissions(cik, user_agent)
    recent = data["filings"]["recent"]

    filings = []

    for i in range(len(recent["accessionNumber"])):
        form = recent["form"][i]
        report_date = recent["reportDate"][i]
        filing_date = recent["filingDate"][i]

        if form not in {"13F-HR", "13F-HR/A", "13F-NT", "13F-NT/A"}:
            continue

        if report_date not in REPORT_PERIODS:
            continue

        if filing_date > FILING_DATE_CUTOFF:
            continue

        filings.append(
            {
                "accession_number": recent["accessionNumber"][i],
                "form_type": form,
                "report_date": report_date,
                "filing_date": filing_date,
            }
        )

    return filings

#1.3
ARCHIVE_BASE_URL = "https://www.sec.gov/Archives/edgar/data"

def get_filing_index(cik: str, accession_number: str, user_agent: str) -> dict:
    """Get index.json for one filing from EDGAR archive."""
    accession_no_dashes = accession_number.replace("-", "")

    url = (
        f"{ARCHIVE_BASE_URL}/"
        f"{int(cik)}/"
        f"{accession_no_dashes}/"
        f"index.json"
    )

    response = sec_get(url, user_agent)

    return response.json()

def find_information_table(
    cik: str,
    accession_number: str,
    user_agent: str,
) -> tuple[str | None, bytes | None]:
    """Find the holdings information-table XML for one filing."""
    index_data = get_filing_index(cik, accession_number, user_agent)

    accession_no_dashes = accession_number.replace("-", "")
    base_url = (
        f"{ARCHIVE_BASE_URL}/"
        f"{int(cik)}/"
        f"{accession_no_dashes}"
    )

    for item in index_data["directory"]["item"]:
        name = item["name"]

        if not name.lower().endswith(".xml"):
            continue

        if name == "primary_doc.xml":
            continue

        url = f"{base_url}/{name}"
        response = sec_get(url, user_agent)

        text = response.text

        if "infoTable" in text:
            return name, response.content

    return None, None

def download_filing_xml(
    cik: str,
    accession_number: str,
    form_type: str,
    user_agent: str,
    output: Path,
) -> None:
    """Download one filing's information-table XML."""
    accession_no_dashes = accession_number.replace("-", "")

    cik_dir = output / "filings" / str(int(cik))
    cik_dir.mkdir(parents=True, exist_ok=True)

    output_file = cik_dir / f"{accession_no_dashes}.xml"

    if output_file.exists():
        print(f"Using cached filing {accession_number}")
        return

    name, content = find_information_table(
        cik,
        accession_number,
        user_agent,
    )
    #13F-NT->primary_doc.xml
    if content is None:
        if form_type in {"13F-NT", "13F-NT/A"}:
            index_data = get_filing_index(cik, accession_number, user_agent)

            accession_no_dashes = accession_number.replace("-", "")
            base_url = (
                f"{ARCHIVE_BASE_URL}/"
                f"{int(cik)}/"
                f"{accession_no_dashes}"
            )

            primary_name = None

            for item in index_data["directory"]["item"]:
                if item["name"] == "primary_doc.xml":
                    primary_name = item["name"]
                    break

            if primary_name is None:
                raise ValueError(
                    f"Could not find primary_doc.xml for notice {accession_number}"
                )

            url = f"{base_url}/{primary_name}"
            response = sec_get(url, user_agent)

            output_file.write_bytes(response.content)

            print(f"Downloaded notice {accession_number} from {primary_name}")
            return

        raise ValueError(
            f"Could not find information table for {accession_number}"
        )

    output_file.write_bytes(content)

    print(
        f"Downloaded {accession_number} "
        f"from {name}"
    )

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
    
    #retrieval
    all_filings = []

    for filer in verified:
        filings = find_filings(filer["cik"], user_agent)

        print(
            f"{filer['fund_name']}: "
            f"{len(filings)} in-scope filings"
        )

        for filing in filings:
            print(
                f"  {filing['report_date']} "
                f"{filing['form_type']} "
                f"{filing['accession_number']}"
            )

            download_filing_xml(
                filer["cik"],
                filing["accession_number"],
                filing["form_type"],
                user_agent,
                output,
            )

        all_filings.extend(filings)

    print()
    print(f"Total in-scope filings: {len(all_filings)}")


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
