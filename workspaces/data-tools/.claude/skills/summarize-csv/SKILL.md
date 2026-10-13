---
name: summarize-csv
description: Summarize a CSV file in the data-tools workspace into a Markdown report with row count and per-column totals.
---

# Summarize a CSV file

1. Read the CSV named in the request from `data/`.
2. Write `reports/<csv name without extension>.md` with:
   - the row count;
   - for each numeric column, its total;
   - for each text column, its distinct values.
