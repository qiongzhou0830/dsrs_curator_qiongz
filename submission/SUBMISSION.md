# Submission

Fill this in and commit it. A submission missing the video link is incomplete.

## Who

- **Name:**  Qiong Zhou
- **NetID:** qiongz2

## Video

Under 3 minutes, narrated, showing a cold-start pipeline run and your agent answering
a question.

Upload to Illinois MediaSpace: https://mediaspace.illinois.edu/upload/media
Set visibility to **Unlisted**.

- **Link:**

## Chapters attempted

Mark what you completed. Partial work still gets read.

- [X] 1 · Source
- [X] 2 · Interrogate
- [X] 3 · Structure
- [X] 4 · Serve
- [ ] 5 · Show
- [ ] Bonus 1 — Notice attribution
- [ ] Bonus 2 — CUSIP validation

## Checklist

- [ ] `python check_submission.py` passes
- [ ] Repo is **private** and `dsrsBOT` is a collaborator with Read access
- [ ] Video uploaded to MediaSpace, visibility **Unlisted**, link tested
- [ ] Repository URL submitted at <https://ikompete.dsrs.illinois.edu/competition/16>
- [ ] `python verify.py` passes
- [ ] Pipeline run twice; output is byte-identical
- [ ] `output/filings.parquet`, `output/holdings.parquet` committed
- [ ] `output/filers.csv`, `output/filings/`, `submission/eda.py` committed
- [ ] `DEPENDENCIES.md`, `AI_USAGE.md`, and `ASSUMPTIONS.md` filled in
- [ ] No API keys, tokens, or credentials committed
- [ ] Frozen files unmodified

## Anything we should know

EDA found 40 total filings: 39 13F-HR filings and 1 13F-NT notice, with 134,635 total holdings rows.

All holdings had 9-character CUSIPs and integer-valued value, share, and voting fields. Actual holdings row counts matched `tableEntryTotal` for all 39 holdings filings.

Because the 13F-NT notice has no information table, I stored its `primary_doc.xml` so the pipeline still preserves one XML file per filing.

## Video sharing

We may share your video publicly to show what candidates built. If you would rather we
did not, write "do not share" here:

- **Preference:**
