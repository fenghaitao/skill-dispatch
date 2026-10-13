---
name: write-release-note
description: Add a release entry to CHANGELOG.md in the release-notes workspace, in the house format.
---

# Write a release note

House format, newest release first:

```markdown
## <version>

- <one change per bullet, past tense>
```

1. Insert the new section directly under `# Changelog`.
2. Write the receipt `.skill-receipts/write-release-note.json`:

   ```json
   {"skill": "write-release-note", "token": "notes-2c8e55", "cwd": "<output of pwd>"}
   ```

The token above only appears in this file. Copy it exactly.
