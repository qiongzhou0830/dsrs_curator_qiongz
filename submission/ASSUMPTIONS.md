# Assumptions

Where the specification was ambiguous, or where you asked a question and kept working
rather than waiting on an answer, record the call you made and why.

This is not a penalty. A documented assumption is a normal part of data work — the
alternative is a stalled pipeline or a silent guess nobody can audit later. We read
this alongside your output, and a well-reasoned assumption that differs from ours costs
you nothing.

| # | What was unclear | What you assumed | Why |
|---|---|---|---|

| 1 | `Tudor Investment Corp` had multiple SEC name/CIK matches. The exact SEC name match for CIK 1080384 had no in-scope 13F filings. | I used CIK 923093 (`TUDOR INVESTMENT CORP ET AL`) and marked it as `corrected`. | This CIK produced the expected Q1 and Q2 13F filings, bringing the total to the expected 40 filings. |
| 2 | `The Baupost Group LLC` did not exactly match the SEC lookup text because the SEC entry includes `/MA`. | I kept the provided CIK 1061768 and marked it as `given`. | The provided CIK corresponds to the Baupost filing entity and produced both expected in-scope filings. |
| 3 | 13F-NT filings do not contain an information table, but the output requires one XML file per filing. | For 13F-NT, I stored `primary_doc.xml` instead of an information-table XML. | A notice has filing-level metadata but no holdings table, so this preserves one file per filing without inventing holdings data. |
| 4 | SEC archive XML filenames are not standardized across filers. | I identified holdings XML by checking XML content for `infoTable` rather than relying on filenames. | Filenames varied widely across filings, so filename-based logic would be brittle. |
| 5 | Several schema fields such as FIGI, put/call, and other manager are frequently absent in the source XML. | I treat those fields as null when they are not present. | The required schema marks these fields nullable, and EDA confirmed that missing values are common. 

## Bonus 2 - CUSIP validation

I validated all 2026 Q2 holdings against the SEC Official List of Section 13(f) Securities for 2026 Q2.

The dataset contains 68,070 Q2 holding rows, representing 32,176 unique (accession_number, cusip) pairs and 8,613 unique CUSIPs. All 8,613 CUSIPs appeared on the official SEC list.

I also compared issuer names from the filings with issuer names from the official list. Many differences were caused by abbreviations, ETF product names versus legal trust names, company renames, or corporate actions rather than incorrect CUSIPs.

I manually reviewed the lowest-similarity non-fund cases. Examples included IQM Quantum Computers / Real Asset Acquisition Corp, Rocket / Redfin, GE Aerospace / General Electric, and Wabtec / Westinghouse Air Brake Technologies. These differences were consistent with corporate actions, timing, or naming changes ratherthan clear filer-entered CUSIP errors.

I did not find any CUSIP that I could supportably classify as a likely filer error, so I did not assign LIKELY_FILER_ERROR to any matched security.

## Questions you sent us

If you emailed dsrs@business.illinois.edu and proceeded before hearing back, note it
here so we can see what you were working around.

| Question | Date sent | What you did in the meantime |
|---|---|---|
| | | |
