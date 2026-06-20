## `edit` and `write`

Use `edit` for exact replacements and `write` for whole-file writes.

- Use `edit` with `oldString` and `newString` for one exact replacement.
- Set `replaceAll` on `edit` only when every exact match should be replaced.
- Use `write` with `path` and `content` to create or overwrite an entire file.
- Re-read the file and retry with exact current text if a replacement fails.
