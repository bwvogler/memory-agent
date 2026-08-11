# TigerFS Backing Table Schema

The file-first workspace (`/mnt/kb/memory/`) is backed by `tigerfs.memory` in
the KB Postgres database. The FUSE layer exposes files normally; this document
covers the raw SQL schema for code that bypasses FUSE for performance (e.g.
`app/kb.py`).

## Column Reference

| Column | Type | Notes |
|--------|------|-------|
| `id` | `uuid` | Primary key. **Not** `file_id`. |
| `parent_id` | `uuid` | Parent directory row; `NULL` = workspace root |
| `filename` | `text` | Bare name including extension (`CLAUDE.md`) |
| `filetype` | `text` | `'file'` for regular files, `'dir'` for directories |
| `title` | `text` | Frontmatter `title:` field |
| `author` | `text` | Frontmatter `author:` field |
| `headers` | `jsonb` | All other frontmatter keys |
| `body` | `text` | File body (everything after frontmatter) |
| `encoding` | `text` | Always `'markdown'` for `.md` workspaces |
| `created_at` | `timestamptz` | Row creation time |
| `modified_at` | `timestamptz` | Last write time |

## Common Pitfalls

- **Column is `id`, not `file_id`** — `file_id` does not exist; queries will fail with `UndefinedColumnError`.
- **`filetype` is always `'file'`** — filtering `WHERE filetype = 'markdown'` returns nothing. Filter by filename suffix instead (`path LIKE '%.md'`).

## Recursive File Listing

```sql
WITH RECURSIVE paths AS (
    SELECT id, body, filename AS path
    FROM   tigerfs.memory
    WHERE  parent_id IS NULL
    UNION ALL
    SELECT m.id, m.body, p.path || '/' || m.filename
    FROM   tigerfs.memory m
    JOIN   paths p ON m.parent_id = p.id
)
SELECT path, body
FROM   paths
WHERE  path LIKE '%.md'
ORDER  BY path
```

This is what `app/kb.py:sql_list_files()` uses. Reading `.info/columns` via the
FUSE layer (`mount/.tables/memory/.info/columns`) will show you the same columns
if the schema ever changes.
