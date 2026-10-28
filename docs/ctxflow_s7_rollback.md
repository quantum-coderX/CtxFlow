# S7 backup/restore rehearsal and rollback plan

This is the operational rollback plan for the CtxFlow memory schema and
run-event persistence. It is intentionally separate from request handling and
does not change [migration 005](../migrations/005_memory_scope_and_run_events.sql).

## Rehearse before a freeze

With PostgreSQL client tools installed and a source `.env` configured:

```bash
python scripts/rehearse_restore.py \
  --source-dotenv .env \
  --archive .artifacts/ctxflow-memory.dump
```

The command:

1. creates a custom-format `pg_dump` archive;
2. validates the archive with `pg_restore --list`;
3. makes no database changes during validation; and
4. prints JSON including the archive size and validation result.

To rehearse an actual restore, use a separate staging database and a separate
dotenv file:

```bash
python scripts/rehearse_restore.py \
  --source-dotenv .env \
  --restore-dotenv .env.rehearsal \
  --archive .artifacts/ctxflow-memory.dump \
  --execute-restore
```

The command refuses to restore to the source database. `--execute-restore`
must never be used with production credentials unless the database owner has
approved the destructive `pg_restore --clean --if-exists` operation.

## Rollback procedure

1. Stop writers and prevent new ingestion jobs from starting.
2. Capture a fresh backup of the current database before changing anything.
3. Identify the last known-good archive and validate it with the rehearsal
   command above.
4. Restore that archive into the approved target using
   `--execute-restore`.
5. Apply migrations only in numbered order; do not edit an existing migration.
6. Verify project isolation, memory counts, run-event chain checks, and a
   representative retrieval before reopening writers.
7. Record the archive path, migration version, validation output, and operator
   approval in the release record.

If archive validation fails, do not attempt a restore from that archive. If a
restore fails, preserve the database logs and current backup, then retry only
against a separately verified target.

## Freeze checklist

- [ ] Rehearsal archive exists outside the repository and is readable by
      `pg_restore --list`.
- [ ] A staging restore completed against a different database.
- [ ] The rollback archive and migration version are recorded.
- [ ] Isolation and run-event verification tests passed.
- [ ] No production credentials or backup archives are committed.
