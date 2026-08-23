from pathlib import Path
from collections import Counter
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]
FILINGS_DIR = ROOT / "output" / "filings"
PRIMARY_DIR = ROOT / ".cache" / "primary"


def tag_name(tag):
    return tag.split("}")[-1]


def find_text(element, name):
    for child in element.iter():
        if tag_name(child.tag) == name:
            if child.text is None:
                return None
            return child.text.strip()
    return None


def main():
    xml_files = sorted(FILINGS_DIR.rglob("*.xml"))

    holdings_files = []
    notice_files = []

    total_rows = 0

    bad_cusip = 0
    bad_value = 0
    bad_amount = 0
    bad_voting = 0

    missing_figi = 0
    missing_put_call = 0
    missing_other_manager = 0

    amount_types = Counter()
    put_calls = Counter()
    discretion_values = Counter()

    prn_example = None
    letter_cusip_example = None
    other_manager_example = None
    spacing_example = None

    repeated_cusip_example = None
    repeated_cusip_rows = None

    names_by_cusip = {}
    name_change_example = None  


    # -----------------------------
    # Read all 40 saved XML files
    # -----------------------------

    for xml_file in xml_files:
        root = ET.parse(xml_file).getroot()

        root_type = tag_name(root.tag)

        if root_type == "edgarSubmission":
            notice_files.append(xml_file)
            continue

        if root_type != "informationTable":
            continue

        holdings_files.append(xml_file)

        cusip_rows = {}

        for row in root.iter():
            if tag_name(row.tag) != "infoTable":
                continue

            total_rows += 1

            issuer = find_text(row, "nameOfIssuer")
            title = find_text(row, "titleOfClass")
            cusip = find_text(row, "cusip")

            if cusip is not None and issuer is not None:

                if cusip not in names_by_cusip:
                    names_by_cusip[cusip] = set()

                names_by_cusip[cusip].add(issuer)

            value = find_text(row, "value")

            amount = find_text(row, "sshPrnamt")
            amount_type = find_text(row, "sshPrnamtType")

            figi = find_text(row, "figi")
            put_call = find_text(row, "putCall")
            other_manager = find_text(row, "otherManager")

            investment_discretion = find_text(
                row,
                "investmentDiscretion",
            )

            voting_sole = find_text(row, "Sole")
            voting_shared = find_text(row, "Shared")
            voting_none = find_text(row, "None")


            # -----------------------------
            # Basic schema checks
            # -----------------------------

            if cusip is None or len(cusip) != 9:
                bad_cusip += 1

            try:
                int(value)
            except (TypeError, ValueError):
                bad_value += 1

            try:
                int(amount)
            except (TypeError, ValueError):
                bad_amount += 1

            for voting_value in [
                voting_sole,
                voting_shared,
                voting_none,
            ]:
                try:
                    int(voting_value)
                except (TypeError, ValueError):
                    bad_voting += 1


            # -----------------------------
            # Nullable fields
            # -----------------------------

            if figi is None:
                missing_figi += 1

            if put_call is None:
                missing_put_call += 1
            else:
                put_calls[put_call] += 1

            if other_manager is None:
                missing_other_manager += 1


            # -----------------------------
            # Observed categorical values
            # -----------------------------

            if amount_type is not None:
                amount_types[amount_type] += 1

            if investment_discretion is not None:
                discretion_values[
                    investment_discretion
                ] += 1


            # -----------------------------
            # Keep some real examples
            # -----------------------------

            if (
                amount_type == "PRN"
                and prn_example is None
            ):
                prn_example = (
                    xml_file.stem,
                    issuer,
                    title,
                    cusip,
                    amount,
                )

            if (
                cusip is not None
                and cusip[0].isalpha()
                and letter_cusip_example is None
            ):
                letter_cusip_example = (
                    xml_file.stem,
                    issuer,
                    title,
                    cusip,
                )

            if (
                other_manager is not None
                and "," in other_manager
                and other_manager_example is None
            ):
                other_manager_example = (
                    xml_file.stem,
                    issuer,
                    cusip,
                    other_manager,
                )

            if (
                issuer is not None
                and "  " in issuer
                and spacing_example is None
            ):
                spacing_example = (
                    xml_file.stem,
                    issuer,
                    cusip,
                )


            # -----------------------------
            # Save rows by CUSIP
            # -----------------------------

            if cusip is not None:

                if cusip not in cusip_rows:
                    cusip_rows[cusip] = []

                cusip_rows[cusip].append({
                    "issuer": issuer,
                    "title": title,
                    "value": value,
                    "amount": amount,
                    "amount_type": amount_type,
                    "put_call": put_call,
                    "investment_discretion":
                        investment_discretion,
                    "other_manager": other_manager,
                })


        # Find one real example where the same CUSIP
        # occurs more than once in the same filing.
        if repeated_cusip_example is None:

            for cusip, rows in cusip_rows.items():

                if len(rows) > 1:
                    repeated_cusip_example = (
                        xml_file.stem,
                        cusip,
                        len(rows),
                    )

                    repeated_cusip_rows = rows[:3]
                    break


    # -----------------------------
    # Filing structure
    # -----------------------------

    print()
    print("=== Filing structure ===")

    print("Total XML files:", len(xml_files))
    print("Holdings filings:", len(holdings_files))
    print("Notice filings:", len(notice_files))
    print("Total holdings rows:", total_rows)

    if notice_files:
        print(
            "Notice filing:",
            notice_files[0].stem,
        )


    # -----------------------------
    # Basic checks
    # -----------------------------

    print()
    print("=== Basic checks ===")

    print("Bad CUSIP rows:", bad_cusip)
    print("Bad value rows:", bad_value)
    print("Bad sshPrnamt rows:", bad_amount)
    print("Bad voting values:", bad_voting)


    # -----------------------------
    # Nullable fields
    # -----------------------------

    print()
    print("=== Nullable fields ===")

    print("Missing FIGI:", missing_figi)
    print("Missing putCall:", missing_put_call)
    print(
        "Missing otherManager:",
        missing_other_manager,
    )


    # -----------------------------
    # Actual values observed
    # -----------------------------

    print()
    print("=== Observed values ===")

    print(
        "sshPrnamtType:",
        dict(amount_types),
    )

    print(
        "putCall:",
        dict(put_calls),
    )

    print(
        "investmentDiscretion:",
        dict(discretion_values),
    )


    # -----------------------------
    # Real examples from the XML
    # -----------------------------
    for cusip, names in names_by_cusip.items():

        if len(names) > 1:
            name_change_example = (
                cusip,
                sorted(names)[:3],
            )
            break

    print()
    print("=== Concrete examples ===")

    print(
        "PRN example:",
        prn_example,
    )

    print(
        "Letter-leading CUSIP example:",
        letter_cusip_example,
    )

    print(
        "Complex otherManager example:",
        other_manager_example,
    )

    print(
        "Issuer spacing example:",
        spacing_example,
    )

    print(
        "Same CUSIP with different issuer names:",
        name_change_example,
    )

    print(
        "Repeated CUSIP example:",
        repeated_cusip_example,
    )

    if repeated_cusip_rows is not None:
        print("Example rows with that CUSIP:")

        for row in repeated_cusip_rows:
            print(row)


    # -----------------------------
    # Primary document reconciliation
    # -----------------------------

    print()
    print("=== Primary document reconciliation ===")

    checked_files = 0
    entry_mismatches = 0
    value_mismatches = 0

    for xml_file in holdings_files:

        accession = xml_file.stem

        primary_file = (
            PRIMARY_DIR / f"{accession}.xml"
        )

        if not primary_file.exists():
            continue

        primary_root = ET.parse(
            primary_file
        ).getroot()

        declared_entries = find_text(
            primary_root,
            "tableEntryTotal",
        )

        declared_value = find_text(
            primary_root,
            "tableValueTotal",
        )

        holdings_root = ET.parse(
            xml_file
        ).getroot()

        actual_entries = 0
        actual_value = 0

        for row in holdings_root.iter():

            if tag_name(row.tag) != "infoTable":
                continue

            actual_entries += 1

            value = find_text(row, "value")

            if value is not None:
                actual_value += int(value)

        if declared_entries is not None:

            checked_files += 1

            if int(declared_entries) != actual_entries:

                entry_mismatches += 1

                print(
                    "Entry mismatch:",
                    accession,
                    "declared=",
                    declared_entries,
                    "actual=",
                    actual_entries,
                )

        if declared_value is not None:

            if int(declared_value) != actual_value:

                value_mismatches += 1

                print(
                    "Value mismatch:",
                    accession,
                    "declared=",
                    declared_value,
                    "actual=",
                    actual_value,
                )

    print(
        "Primary files checked:",
        checked_files,
    )

    print(
        "Entry count mismatches:",
        entry_mismatches,
    )

    print(
        "Value total mismatches:",
        value_mismatches,
    )


    # -----------------------------
    # Report periods and forms
    # -----------------------------

    print()
    print("=== Report periods and forms ===")

    periods = set()
    forms = set()

    if PRIMARY_DIR.exists():

        for primary_file in PRIMARY_DIR.glob("*.xml"):

            root = ET.parse(
                primary_file
            ).getroot()

            period = find_text(
                root,
                "periodOfReport",
            )

            form = find_text(
                root,
                "submissionType",
            )

            if period is not None:
                periods.add(period)

            if form is not None:
                forms.add(form)

    print(
        "Report periods:",
        sorted(periods),
    )

    print(
        "Form types:",
        sorted(forms),
    )


    # -----------------------------
    # Main findings
    # -----------------------------

    print()
    print("=== Findings ===")

    print(
        "1. There are two different filing structures. "
        "Most XML files are information tables with holdings, "
        "but the 13F-NT notice has filing information and no "
        "infoTable rows."
    )

    print(
        "2. FIGI, putCall, and otherManager are not present "
        "on every holding. These fields need to stay nullable."
    )

    print(
        "3. sshPrnamt does not always mean number of shares. "
        "The data contains both SH and PRN. PRN is used for "
        "principal amounts such as notes and debt securities."
    )

    print(
        "4. The same CUSIP can appear multiple times in one "
        "filing with different amounts, values, discretion, or "
        "otherManager references. These are separate reported "
        "positions and should not be deduplicated by CUSIP."
    )

    print(
        "5. otherManager is not always one simple number. "
        "The XML contains values with multiple references, "
        "such as comma-separated values. It should be "
        "preserved as a string."
    )

    print(
        "6. Issuer names and titleOfClass are free text. "
        "The same security can have different formatting or "
        "spacing between filings, so the original text should "
        "be preserved."
    )

    print(
        "7. Some valid CUSIPs begin with letters. "
        "CUSIP therefore needs to stay a 9-character string."
    )

    unusual_put_calls = [
        x for x in put_calls
        if x not in {"Call", "Put"}
    ]

    if len(unusual_put_calls) == 0:

        print(
            "8. In these 40 XML files, putCall used only "
            "'Call' and 'Put'. No other capitalization was "
            "observed."
        )

    else:

        print(
            "8. Other putCall values were found:",
            unusual_put_calls,
        )


if __name__ == "__main__":
    main()