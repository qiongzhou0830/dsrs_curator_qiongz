from pathlib import Path
from datetime import datetime
import csv
import json
import xml.etree.ElementTree as ET

import pyarrow as pa
import pyarrow.parquet as pq


ROOT = Path(__file__).resolve().parent

FILERS_FILE = ROOT / "output" / "filers.csv"
PRIMARY_DIR = ROOT / ".cache" / "primary"
SUBMISSIONS_DIR = ROOT / ".cache" / "submissions"
OUTPUT_FILE = ROOT / "output" / "filings.parquet"
REPORT_PERIODS = {"2026-03-31", "2026-06-30"}
FILING_DATE_CUTOFF = "2026-08-18"
VALID_FORMS = {
    "13F-HR",
    "13F-HR/A",
    "13F-NT",
    "13F-NT/A",
}
HOLDINGS_OUTPUT_FILE = ROOT / "output" / "holdings.parquet"
FILINGS_DIR = ROOT / "output" / "filings"


def local_name(tag: str) -> str:
    """Remove the XML namespace from a tag."""
    return tag.split("}")[-1]


def get_text(element, tag_name):
    """Find the first matching XML tag and return its text."""
    for child in element.iter():
        if local_name(child.tag) == tag_name:
            return child.text

    return None


def to_int(value):
    """Convert XML text to int, keeping missing values as None."""
    if value is None or value.strip() == "":
        return None

    return int(value)


def parse_date(value):
    """Convert YYYY-MM-DD text to a Python date."""
    return datetime.strptime(value, "%Y-%m-%d").date()


def parse_cover_date(value):
    """Convert SEC cover-page MM-DD-YYYY date to a Python date."""
    return datetime.strptime(value, "%m-%d-%Y").date()


def make_report_quarter(report_period):
    """Convert a report date such as 2026-03-31 to 2026Q1."""
    quarter = (report_period.month - 1) // 3 + 1

    return f"{report_period.year}Q{quarter}"


def load_filers():
    """Load the verified manager roster."""
    rows = []

    with FILERS_FILE.open(newline="") as f:
        reader = csv.DictReader(f)

        for row in reader:
            rows.append(row)

    return rows


def load_submission_filings(cik):
    """
    Read the cached SEC submissions JSON and find
    the Q1/Q2 filings that are in scope.
    """
    padded_cik = cik.zfill(10)
    path = SUBMISSIONS_DIR / f"CIK{padded_cik}.json"

    data = json.loads(path.read_text())
    recent = data["filings"]["recent"]

    filings = []

    for i in range(len(recent["accessionNumber"])):
        form = recent["form"][i]
        report_date = recent["reportDate"][i]
        filing_date = recent["filingDate"][i]

        # Same scope rules used in Chapter 1.
        if form not in VALID_FORMS:
            continue

        if report_date not in REPORT_PERIODS:
            continue

        if filing_date > FILING_DATE_CUTOFF:
            continue

        filings.append(
            {
                "accession_number": recent["accessionNumber"][i],
                "form_type": form,
                "filing_date": filing_date,
            }
        )

    return filings


def parse_primary(accession_number):
    """Read filing-level fields from primary_doc.xml."""
    accession_no_dashes = accession_number.replace("-", "")
    path = PRIMARY_DIR / f"{accession_no_dashes}.xml"

    root = ET.parse(path).getroot()

    # Filing manager name from the cover page.
    filing_manager = None

    for element in root.iter():
        if local_name(element.tag) == "filingManager":
            filing_manager = get_text(element, "name")
            break

    report_period_text = get_text(root, "periodOfReport")
    report_period = parse_cover_date(report_period_text)

    form_type = get_text(root, "submissionType")

    # Amendment information is only present for amended filings.
    amendment_no = get_text(root, "amendmentNumber")
    amendment_type = get_text(root, "amendmentType")

    return {
        "filing_manager": filing_manager,
        "report_period": report_period,
        "form_type": form_type,
        "is_amendment": form_type.endswith("/A"),
        "amendment_no": to_int(amendment_no),
        "amendment_type": amendment_type,
        "report_type": get_text(root, "reportType"),
        "form_13f_file_number": get_text(root, "form13FFileNumber"),
        "crd_number": get_text(root, "crdNumber"),
        "sec_file_number": get_text(root, "secFileNumber"),
        "other_included_managers_count": to_int(
            get_text(root, "otherIncludedManagersCount")
        ),
        "table_entry_total": to_int(
            get_text(root, "tableEntryTotal")
        ),
        "table_value_total": to_int(
            get_text(root, "tableValueTotal")
        ),
    }


def build_filing_rows():
    """Build one Python dictionary for each filing."""
    rows = []

    for filer in load_filers():
        cik = filer["cik"]
        fund_name = filer["fund_name"]

        submissions = load_submission_filings(cik)

        for submission in submissions:
            accession = submission["accession_number"]
            primary = parse_primary(accession)

            rows.append(
                {
                    # From SEC submissions API
                    "accession_number": accession,

                    # Required as a zero-padded 10-character string
                    "cik": cik.zfill(10),

                    # Exact name from the supplied roster
                    "fund_name": fund_name,

                    # Exact legal manager name on the cover page
                    "filing_manager": primary["filing_manager"],

                    "form_type": submission["form_type"],

                    # Report period comes from the filing cover page
                    "report_period": primary["report_period"],

                    # Derived from report_period
                    "report_quarter": make_report_quarter(
                        primary["report_period"]
                    ),

                    # Filing date comes from submissions API
                    "filing_date": parse_date(
                        submission["filing_date"]
                    ),

                    "is_amendment": primary["is_amendment"],
                    "amendment_no": primary["amendment_no"],
                    "amendment_type": primary["amendment_type"],
                    "report_type": primary["report_type"],
                    "form_13f_file_number": (
                        primary["form_13f_file_number"]
                    ),
                    "crd_number": primary["crd_number"],
                    "sec_file_number": primary["sec_file_number"],
                    "other_included_managers_count": (
                        primary["other_included_managers_count"]
                    ),
                    "table_entry_total": primary["table_entry_total"],
                    "table_value_total": primary["table_value_total"],
                }
            )

    return rows


def write_filings_parquet(rows):
    """Write filings.parquet using the exact required Arrow schema."""
    schema = pa.schema(
        [
            pa.field("accession_number", pa.string(), nullable=False),
            pa.field("cik", pa.string(), nullable=False),
            pa.field("fund_name", pa.string(), nullable=False),
            pa.field("filing_manager", pa.string(), nullable=False),
            pa.field("form_type", pa.string(), nullable=False),
            pa.field("report_period", pa.date32(), nullable=False),
            pa.field("report_quarter", pa.string(), nullable=False),
            pa.field("filing_date", pa.date32(), nullable=False),
            pa.field("is_amendment", pa.bool_(), nullable=False),
            pa.field("amendment_no", pa.int32(), nullable=True),
            pa.field("amendment_type", pa.string(), nullable=True),
            pa.field("report_type", pa.string(), nullable=False),
            pa.field("form_13f_file_number", pa.string(), nullable=True),
            pa.field("crd_number", pa.string(), nullable=True),
            pa.field("sec_file_number", pa.string(), nullable=True),
            pa.field(
                "other_included_managers_count",
                pa.int32(),
                nullable=True,
            ),
            pa.field("table_entry_total", pa.int64(), nullable=True),
            pa.field("table_value_total", pa.int64(), nullable=True),
        ]
    )

    table = pa.Table.from_pylist(rows, schema=schema)

    pq.write_table(
        table,
        OUTPUT_FILE,
        compression="snappy",
    )

def parse_holding_rows(xml_file, filing):
    """
    Parse every infoTable row from one holdings XML file.

    One infoTable element becomes one row in holdings.parquet.
    Row order stays the same as the source XML.
    """
    root = ET.parse(xml_file).getroot()

    rows = []

    for element in root.iter():
        if local_name(element.tag) != "infoTable":
            continue

        # These are required numeric fields in the output schema.
        value = int(get_text(element, "value"))
        shares = int(get_text(element, "sshPrnamt"))
        voting_sole = int(get_text(element, "Sole"))
        voting_shared = int(get_text(element, "Shared"))
        voting_none = int(get_text(element, "None"))

        # These fields are allowed to be missing.
        figi = get_text(element, "figi")
        put_call = get_text(element, "putCall")
        other_manager = get_text(element, "otherManager")
        # Some filings use "0" to mean no other manager.
        if other_manager == "0":
            other_manager = None

        rows.append(
            {
                # Filing-level identifiers
                "accession_number": filing["accession_number"],
                "cik": filing["cik"],
                "report_quarter": filing["report_quarter"],

                # Position-level fields from infoTable
                "name_of_issuer": get_text(
                    element,
                    "nameOfIssuer",
                ),
                "title_of_class": get_text(
                    element,
                    "titleOfClass",
                ),
                "cusip": get_text(
                    element,
                    "cusip",
                ),
                "figi": figi,
                "value": value,
                "ssh_prnamt": shares,
                "ssh_prnamt_type": get_text(
                    element,
                    "sshPrnamtType",
                ),
                "put_call": put_call,
                "investment_discretion": get_text(
                    element,
                    "investmentDiscretion",
                ),
                "other_manager": other_manager,
                "voting_sole": voting_sole,
                "voting_shared": voting_shared,
                "voting_none": voting_none,
            }
        )

    return rows

def build_holding_rows(filing_rows):
    """Build all holdings rows from the 39 holdings filings."""
    rows = []

    for filing in filing_rows:

        # A notice filing has no information table,
        # so it produces no holdings rows.
        if filing["form_type"] in {"13F-NT", "13F-NT/A"}:
            continue

        accession_no_dashes = filing[
            "accession_number"
        ].replace("-", "")

        # output/filings uses CIK without leading zeros.
        cik_folder = str(int(filing["cik"]))

        xml_file = (
            FILINGS_DIR
            / cik_folder
            / f"{accession_no_dashes}.xml"
        )

        if not xml_file.exists():
            raise FileNotFoundError(
                f"Missing holdings XML: {xml_file}"
            )

        filing_holdings = parse_holding_rows(
            xml_file,
            filing,
        )

        rows.extend(filing_holdings)

    return rows

def write_holdings_parquet(rows):
    """Write holdings.parquet using the required Arrow schema."""
    schema = pa.schema(
        [
            pa.field(
                "accession_number",
                pa.string(),
                nullable=False,
            ),
            pa.field(
                "cik",
                pa.string(),
                nullable=False,
            ),
            pa.field(
                "report_quarter",
                pa.string(),
                nullable=False,
            ),
            pa.field(
                "name_of_issuer",
                pa.string(),
                nullable=False,
            ),
            pa.field(
                "title_of_class",
                pa.string(),
                nullable=False,
            ),
            pa.field(
                "cusip",
                pa.string(),
                nullable=False,
            ),
            pa.field(
                "figi",
                pa.string(),
                nullable=True,
            ),
            pa.field(
                "value",
                pa.int64(),
                nullable=False,
            ),
            pa.field(
                "ssh_prnamt",
                pa.int64(),
                nullable=False,
            ),
            pa.field(
                "ssh_prnamt_type",
                pa.string(),
                nullable=False,
            ),
            pa.field(
                "put_call",
                pa.string(),
                nullable=True,
            ),
            pa.field(
                "investment_discretion",
                pa.string(),
                nullable=False,
            ),
            pa.field(
                "other_manager",
                pa.string(),
                nullable=True,
            ),
            pa.field(
                "voting_sole",
                pa.int64(),
                nullable=False,
            ),
            pa.field(
                "voting_shared",
                pa.int64(),
                nullable=False,
            ),
            pa.field(
                "voting_none",
                pa.int64(),
                nullable=False,
            ),
        ]
    )

    table = pa.Table.from_pylist(
        rows,
        schema=schema,
    )

    pq.write_table(
        table,
        HOLDINGS_OUTPUT_FILE,
        compression="snappy",
    )


def main():
    filing_rows = build_filing_rows()

    print(f"Built {len(filing_rows)} filing rows")

    write_filings_parquet(filing_rows)

    print(f"Wrote {OUTPUT_FILE}")

    holding_rows = build_holding_rows(
        filing_rows
    )

    print(
        f"Built {len(holding_rows)} holdings rows"
    )

    write_holdings_parquet(
        holding_rows
    )

    print(
        f"Wrote {HOLDINGS_OUTPUT_FILE}"
    )


if __name__ == "__main__":
    main()