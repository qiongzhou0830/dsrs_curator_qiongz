from pathlib import Path
import csv
import urllib.request

import pyarrow.parquet as pq


HOLDINGS_FILE = Path("output/holdings.parquet")
FILINGS_FILE = Path("output/filings.parquet")
SEC_LIST_FILE = Path("13flist2026q2-txt.txt")
OUTPUT_FILE = Path("output/bonus_cusip_validation.csv")

SEC_URL = (
    "https://www.sec.gov/files/investment/"
    "13flist2026q2-txt.txt"
)


def download_sec_list():
    if SEC_LIST_FILE.exists():
        return

    request = urllib.request.Request(
        SEC_URL,
        headers={
            "User-Agent": "Qiong Zhou qiongz2@illinois.edu"
        },
    )

    with urllib.request.urlopen(request) as response:
        SEC_LIST_FILE.write_bytes(response.read())


def load_official_securities():
    official = {}

    with SEC_LIST_FILE.open(
        encoding="utf-8",
        errors="ignore",
    ) as f:
        for line in f:
            if len(line) < 40:
                continue

            cusip = line[:9].strip()
            issuer = line[10:40].strip()

            if len(cusip) == 9:
                official[cusip] = issuer

    return official


def main():
    download_sec_list()
    official = load_official_securities()

    holdings = pq.read_table(
        HOLDINGS_FILE
    ).to_pylist()

    filings = pq.read_table(
        FILINGS_FILE
    ).to_pylist()

    fund_by_accession = {
        row["accession_number"]: {
            "cik": row["cik"],
            "fund_name": row["fund_name"],
        }
        for row in filings
    }

    grouped = {}

    for row in holdings:
        if row["report_quarter"] != "2026Q2":
            continue

        key = (
            row["accession_number"],
            row["cusip"],
        )

        if key not in grouped:
            grouped[key] = {
                "accession_number": row["accession_number"],
                "cusip": row["cusip"],
                "issuer_from_filing": row["name_of_issuer"],
                "rows": 0,
            }

        grouped[key]["rows"] += 1

    output_rows = []

    for item in grouped.values():
        accession = item["accession_number"]
        cusip = item["cusip"]

        manager = fund_by_accession[accession]
        on_list = cusip in official

        output_rows.append(
            {
                "accession_number": accession,
                "cik": manager["cik"],
                "fund_name": manager["fund_name"],
                "cusip": cusip,
                "on_official_list": on_list,
                "issuer_from_filing": item["issuer_from_filing"],
                "issuer_from_list": (
                    official[cusip]
                    if on_list
                    else ""
                ),
                "rows": item["rows"],
                "assessment": (
                    ""
                    if on_list
                    else "UNRESOLVED"
                ),
            }
        )

    output_rows.sort(
        key=lambda row: (
            row["accession_number"],
            row["cusip"],
        )
    )

    fieldnames = [
        "accession_number",
        "cik",
        "fund_name",
        "cusip",
        "on_official_list",
        "issuer_from_filing",
        "issuer_from_list",
        "rows",
        "assessment",
    ]

    with OUTPUT_FILE.open(
        "w",
        newline="",
        encoding="utf-8",
    ) as f:
        writer = csv.DictWriter(
            f,
            fieldnames=fieldnames,
        )

        writer.writeheader()
        writer.writerows(output_rows)

    print(
        f"Wrote {len(output_rows)} rows to {OUTPUT_FILE}"
    )


if __name__ == "__main__":
    main()