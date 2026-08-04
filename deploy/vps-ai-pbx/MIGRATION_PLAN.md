# Migration Plan

This document sequences the actual cutover from the Raspberry Pi + local
Docker setup to this VPS deployment — **not executed as part of producing
these artifacts**, per the explicit instruction to not migrate production
yet. It's the runbook for when you're ready to do so.

## Backward compatibility (read this first)

Nothing about producing `deploy/vps-ai-pbx/` changed the Raspberry Pi or any
existing repo file. The Pi remains the production reference until you
explicitly decide, separately, to cut over. Every step below is written so
you can stop at any point and keep running on the Pi indefinitely — there is
no point of no return until you actually repoint your SIP trunk's
registration at the new VPS (step 5).

## Sequence

### 1. Provision a VPS

Any provider works — nothing in this deployment assumes a specific one (see
`VPS_FIREWALL.md`'s generic ufw/iptables rules, not cloud-specific
security-group calls). Minimum recommended: 2 vCPU / 4GB RAM for a
single-tenant bootstrap deployment (FreeSWITCH + Postgres + Redis + MinIO +
api + ui all on one box). Debian 12 or Ubuntu 22.04+ recommended (matches
what `install.sh`/the FreeSWITCH Dockerfile assume for the host's package
manager).

### 2. Run `install.sh`

```bash
curl -fsSL https://raw.githubusercontent.com/dograh-hq/dograh/main/deploy/vps-ai-pbx/install.sh | bash
# or, if you already have the repo cloned:
cd deploy/vps-ai-pbx && ./install.sh
```

Installs Docker, builds `.env` interactively (see `VPS_DEPLOYMENT_GUIDE.md`
for what it asks), builds the `freeswitch` image from source, and brings the
full stack up. See `VPS_FIREWALL.md` and open the required ports before or
immediately after this step.

### 3. Validate the new stack in isolation — no trunk repointed yet

Follow `VPS_DEPLOYMENT_GUIDE.md`'s verification checklist and
`TROUBLESHOOTING.md` if anything doesn't come up healthy. At this point the
new VPS stack is fully running but **not receiving any real calls** — the
Pi keeps handling production traffic throughout.

### 4. Back up the Pi's current SIP trunk credentials

You already have these (they're what's in `RASPBERRY_FREESWITCH_BACKUP.md`'s
referenced-but-not-reproduced gateway config) — enter them into the new
VPS's `.env` (`SIP_GATEWAY_*`) if you haven't already during `install.sh`.

### 5. Cut over — repoint the SIP trunk

This is the only step that actually affects production traffic. With your
SIP trunk provider, change the registration target from the Pi's address to
the new VPS's public IP (`SIP_EXTERNAL_IP`). Most providers apply this
within seconds to a few minutes (matches the same `expire-seconds=1800`
re-registration cadence audited on the Pi).

**Rollback**: repoint the trunk back to the Pi's address. Since the Pi was
never touched, it's still fully configured and ready to take over
immediately — this is the entire rollback procedure, no data restore
needed.

### 6. Monitor both directions for a burn-in period

Place test calls (inbound and outbound, per `TROUBLESHOOTING.md`'s
checklist) against the VPS. Keep the Pi powered on and untouched during this
period specifically so rollback (step 5, reversed) stays a one-step
operation.

### 7. Decommission the Pi (only once confident)

Not part of this migration's scope to schedule or execute — a separate,
later, explicit decision once the VPS has proven itself in production. Until
then, `RASPBERRY_FREESWITCH_BACKUP.md` remains the record of exactly what
was running there, in case it's ever needed for reference again.

## Database / MinIO migration (if there's existing data to carry over)

This bootstrap deployment starts with fresh Postgres/MinIO volumes — there
is no existing Dograh call-history data on the Pi to migrate (the Pi only
ever ran FreeSWITCH itself; Dograh's own database has been running on the
separate Windows dev machine per `DOGRAH_CURRENT_ARCHITECTURE.md`). If
you're instead consolidating an existing Dograh database onto this VPS:

1. **Backup**: `pg_dump -Fc` against the source database (same approach as
   `scripts/backup.sh` step 1).
2. **Restore**: `pg_restore` into the new VPS's `postgres` container (same
   approach as `scripts/restore.sh` step 1) — do this *before* starting the
   `api`/`ui` services against it, so no writes race the restore.
3. **Validate**: log into the migrated dashboard, confirm organizations,
   workflows, and phone number bindings all look correct before relying on
   it.

Same pattern for MinIO (`mc mirror` from the old endpoint to the new one, or
a volume-level tar transfer — see `BACKUP_RESTORE.md`).
