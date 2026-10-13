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
3. Write the receipt `.skill-receipts/summarize-csv.json`:

   ```json
   {"skill": "summarize-csv", "token": "data-9d41b0", "cwd": "<output of pwd>", "dispatch_id": "<dispatch ID from request>"}
   ```

The token above only appears in this file. Copy it exactly.
Include the dispatch ID from the request. Write complete JSON to a temporary
file in `.skill-receipts/`, then atomically rename it to the receipt path.
