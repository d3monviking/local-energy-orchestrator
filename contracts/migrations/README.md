# Schema migrations

`contracts/ddl.sql` is the base schema. Every schema change after it goes here as a numbered SQL file, applied by `contracts/migrate.py`:

```bash
POSTGRES_PORT=5433 python contracts/migrate.py           # apply pending migrations
POSTGRES_PORT=5433 python contracts/migrate.py --status  # what's applied
```

`python -m world.build` runs pending migrations too, so a fresh database always ends up on the latest schema.

## Rules

- **Name:** `NNN_<owner>_<what>.sql`, where `NNN` is the next free number and owner is `a` (Grid & DISCOM) or `b` (Household). Example: `003_b_dr_override.sql`.
- **Claim the number** in your PR title. If two branches pick the same number, whoever merges second renumbers; the runner refuses duplicates.
- **Never edit a merged migration.** Fix forward with a new one.
- Each file runs once, in its own transaction. Prefer `CREATE TABLE IF NOT EXISTS` / `ADD COLUMN IF NOT EXISTS`, so re-running against a hand-patched database is harmless.
- Don't edit `ddl.sql` for new changes. It stays the base, so the two work streams never conflict on it.
