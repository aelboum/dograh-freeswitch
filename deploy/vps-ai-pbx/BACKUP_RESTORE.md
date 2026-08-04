# Backup & Restore

## What's backed up today (`scripts/backup.sh`)

| What | How | Why |
| --- | --- | --- |
| Postgres | `pg_dump -Fc` (custom format) inside the running `postgres` container | Includes every org's data — `organizations`, `workflows`, `workflow_runs`, `workflow_recordings` metadata, `telephony_configurations`, everything (see `ARCHITECTURE.md` §2's Tenant Model table for what lives here) |
| MinIO data | Full volume tar (`docker run` + `tar czf` against the named volume) | Call recordings and any other object storage content — the actual audio files `workflow_recordings` rows point at |
| FreeSWITCH config volume | Full volume tar | Captures anything beyond the bootstrap-rendered set — e.g. if you hand-added a second gateway file for testing (see `docker-compose.vps.yml`'s `freeswitch-config` volume comment) |
| `.env` | Plain copy (`env.snapshot`) | Lets a restore recreate an identical stack — contains secrets, protect the backup directory accordingly |

Run it:

```bash
cd deploy/vps-ai-pbx
./scripts/backup.sh                # writes to ./backups/<timestamp>/
./scripts/backup.sh /path/elsewhere  # or a custom output directory
```

Nothing here is automatically scheduled — wire it into cron yourself, e.g.:

```
0 3 * * * cd /path/to/dograh/deploy/vps-ai-pbx && ./scripts/backup.sh /var/backups/ai-pbx >> /var/log/ai-pbx-backup.log 2>&1
```

## Restoring (`scripts/restore.sh`)

**Destructive** — overwrites the live Postgres database, MinIO volume, and
FreeSWITCH config volume. Requires typing `restore` to confirm before
touching anything.

```bash
cd deploy/vps-ai-pbx
./scripts/restore.sh ./backups/20260804T120000Z
```

Stops the stack, restores Postgres first (bringing only `postgres` back up,
dropping/recreating the `public` schema, then `pg_restore`), restores the
MinIO and FreeSWITCH volumes, then brings the full stack back up.
`.env.snapshot` from the backup is **not** applied automatically — compare
it against the live `.env` yourself first, in case the live one changed for
unrelated reasons since that backup was taken.

## Disaster recovery (fresh VPS)

1. Provision a new VPS, run `install.sh` (see `VPS_DEPLOYMENT_GUIDE.md`) to
   get the stack shape running with a fresh `.env`.
2. Copy your most recent backup directory onto the new VPS.
3. Run `scripts/restore.sh <backup-dir>`.
4. Re-point your SIP trunk's registration at the new VPS's IP (same as
   `MIGRATION_PLAN.md` step 5).

## Future: per-tenant data (documentation only — not implemented this pass)

As real tenants and call volume accumulate, the all-or-nothing volume-level
approach above stops being the right granularity for some operations. Future
capabilities, once the Configuration Service and multi-tenant provisioning
(`SAAS_ROADMAP.md` Phase 2/3) are real:

- **Call recordings** — `workflow_recordings` rows are already
  `organization_id`-scoped (`ARCHITECTURE.md` §2). A future per-tenant
  export would use `mc mirror` against a tenant-prefixed MinIO path, rather
  than the whole-bucket tar `backup.sh` does today.
- **AI conversation logs/transcripts** — `workflow_runs`, also already
  org-scoped. Same idea: a `pg_dump` filtered to
  `WHERE organization_id = <id>` (via `pg_dump --table` + a row-filtering
  view, or a dedicated export query) instead of the whole-database dump.
- **Full tenant export** — the above two, bundled: a filtered `pg_dump` plus
  the tenant's MinIO prefix, packaged together as one exportable unit (e.g.
  for a customer offboarding or a "download my data" feature).
- **Workflow export** — `workflow_definitions` is already a portable JSON
  representation of a workflow; a future export feature is mostly a matter
  of an API endpoint around what already exists, not new data modeling.

### Recommended retention policy (a recommendation for the roadmap, not enforced today)

- **Call audio** (MinIO recordings): a bounded retention window (e.g. 30-90
  days) unless a tenant's plan or compliance need says otherwise — audio is
  the largest storage cost by far.
- **Transcripts/call logs** (`workflow_runs`): can reasonably outlive the
  audio itself (much smaller, often useful for analytics/billing long after
  the recording is no longer needed).
- **Full backups** (`scripts/backup.sh` output): keep a short rolling window
  (e.g. 7 daily + 4 weekly) rather than indefinitely, given they duplicate
  the same recordings/transcripts above.

None of the above is automated by anything in this deployment today — it's
a direction for whoever builds the tenant-facing data-management features in
`SAAS_ROADMAP.md` Phase 2/3.
