from pathlib import Path
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]
FILINGS_DIR = ROOT / "output" / "filings"
PRIMARY_DIR = ROOT / ".cache" / "primary"


def local_name(tag: str) -> str:
    """Remove XML namespace from a tag."""
    return tag.split("}")[-1]


def get_text(element, tag_name):
    """Find the text of one XML tag inside an element."""
    for child in element.iter():
        if local_name(child.tag) == tag_name:
            return child.text
    return None


def parse_holdings_file(xml_file: Path) -> dict:
    """Inspect one holdings XML file."""
    root = ET.parse(xml_file).getroot()

    holdings_count = 0
    bad_cusip = 0
    bad_value = 0
    bad_shares = 0
    bad_voting = 0

    missing_figi = 0
    missing_put_call = 0
    missing_other_manager = 0

    total_value = 0

    for row in root.iter():
        if local_name(row.tag) != "infoTable":
            continue

        holdings_count += 1

        cusip = get_text(row, "cusip")
        value = get_text(row, "value")
        shares = get_text(row, "sshPrnamt")

        voting_sole = get_text(row, "Sole")
        voting_shared = get_text(row, "Shared")
        voting_none = get_text(row, "None")

        figi = get_text(row, "figi")
        put_call = get_text(row, "putCall")
        other_manager = get_text(row, "otherManager")

        # Check 1:
        # Chapter 3 expects CUSIP to be a 9-character string.
        if cusip is None or len(cusip.strip()) != 9:
            bad_cusip += 1

        # Check 2:
        # Chapter 3 expects value to be an integer.
        try:
            value_int = int(value)
            total_value += value_int
        except (TypeError, ValueError):
            bad_value += 1

        # Check 3:
        # Chapter 3 expects share amount to be an integer.
        try:
            int(shares)
        except (TypeError, ValueError):
            bad_shares += 1

        # Check 4:
        # Chapter 3 expects all three voting fields to be integers.
        for voting_value in [voting_sole, voting_shared, voting_none]:
            try:
                int(voting_value)
            except (TypeError, ValueError):
                bad_voting += 1

        # Check 5:
        # These fields are nullable in the required schema,
        # so we count how often they are missing.
        if figi is None:
            missing_figi += 1

        if put_call is None:
            missing_put_call += 1

        if other_manager is None:
            missing_other_manager += 1

    return {
        "file": xml_file,
        "holdings_count": holdings_count,
        "bad_cusip": bad_cusip,
        "bad_value": bad_value,
        "bad_shares": bad_shares,
        "bad_voting": bad_voting,
        "missing_figi": missing_figi,
        "missing_put_call": missing_put_call,
        "missing_other_manager": missing_other_manager,
        "total_value": total_value,
    }


def parse_primary_file(xml_file: Path) -> dict:
    """Read filing-level information from one primary document."""
    root = ET.parse(xml_file).getroot()

    return {
        "submission_type": get_text(root, "submissionType"),
        "period_of_report": get_text(root, "periodOfReport"),
        "table_entry_total": get_text(root, "tableEntryTotal"),
        "table_value_total": get_text(root, "tableValueTotal"),
        "filing_manager": get_text(root, "name"),
        "report_type": get_text(root, "reportType"),
    }


def main() -> None:
    xml_files = sorted(FILINGS_DIR.rglob("*.xml"))

    holdings_files = []
    notice_files = []
    unknown_files = []

    # Check 6:
    # Identify whether each saved XML is a holdings table or a notice.
    for xml_file in xml_files:
        root = ET.parse(xml_file).getroot()
        root_name = local_name(root.tag)

        if root_name == "informationTable":
            holdings_files.append(xml_file)

        elif root_name == "edgarSubmission":
            notice_files.append(xml_file)

        else:
            unknown_files.append(xml_file)

    print("=== Filing structure ===")
    print(f"Total XML files: {len(xml_files)}")
    print(f"Holdings filings: {len(holdings_files)}")
    print(f"Notice filings: {len(notice_files)}")
    print(f"Unknown files: {len(unknown_files)}")

    holdings_results = []

    for xml_file in holdings_files:
        holdings_results.append(parse_holdings_file(xml_file))

    total_holdings = sum(
        row["holdings_count"]
        for row in holdings_results
    )

    print()
    print("=== Holdings rows ===")
    print(f"Total holdings rows: {total_holdings}")

    # Check 7:
    # Validate fields that will later become integer/string columns.
    print()
    print("=== Schema-related checks ===")

    print(
        "Bad CUSIP rows:",
        sum(row["bad_cusip"] for row in holdings_results),
    )

    print(
        "Bad value rows:",
        sum(row["bad_value"] for row in holdings_results),
    )

    print(
        "Bad share rows:",
        sum(row["bad_shares"] for row in holdings_results),
    )

    print(
        "Bad voting values:",
        sum(row["bad_voting"] for row in holdings_results),
    )

    # Check 8:
    # See how often nullable fields are actually absent.
    print()
    print("=== Nullable fields ===")

    print(
        "Missing FIGI:",
        sum(row["missing_figi"] for row in holdings_results),
    )

    print(
        "Missing put/call:",
        sum(row["missing_put_call"] for row in holdings_results),
    )

    print(
        "Missing other manager:",
        sum(row["missing_other_manager"] for row in holdings_results),
    )

    # Check 9:
    # Show the largest filings by number of holdings.
    print()
    print("=== Largest filings ===")

    largest = sorted(
        holdings_results,
        key=lambda row: row["holdings_count"],
        reverse=True,
    )[:10]

    for row in largest:
        print(
            row["file"].name,
            row["holdings_count"],
        )

    # Check 10:
    # Compare the real number of infoTable rows against
    # tableEntryTotal reported in the primary document.
    print()
    print("=== Primary document reconciliation ===")

    mismatch_count = 0
    checked_count = 0

    for row in holdings_results:
        accession = row["file"].stem
        primary_file = PRIMARY_DIR / f"{accession}.xml"

        if not primary_file.exists():
            continue

        primary = parse_primary_file(primary_file)

        table_entry_total = primary["table_entry_total"]

        if table_entry_total is not None:
            checked_count += 1

            if int(table_entry_total) != row["holdings_count"]:
                mismatch_count += 1
                print(
                    "Entry count mismatch:",
                    accession,
                    table_entry_total,
                    row["holdings_count"],
                )

        # Also compare total reported value.
        table_value_total = primary["table_value_total"]

        if table_value_total is not None:
            if int(table_value_total) != row["total_value"]:
                print(
                    "Value total mismatch:",
                    accession,
                    table_value_total,
                    row["total_value"],
                )

    print(f"Primary files checked: {checked_count}")
    print(f"Entry count mismatches: {mismatch_count}")

    # Check 11:
    # Confirm the expected report periods and form types
    # from the primary documents that are already cached.
    print()
    print("=== Report periods and forms ===")

    periods = set()
    forms = set()

    for primary_file in PRIMARY_DIR.glob("*.xml"):
        primary = parse_primary_file(primary_file)

        if primary["period_of_report"] is not None:
            periods.add(primary["period_of_report"])

        if primary["submission_type"] is not None:
            forms.add(primary["submission_type"])

    print("Report periods:", sorted(periods))
    print("Form types:", sorted(forms))


if __name__ == "__main__":
    main()