# deploy/vps-ai-pbx — AI PBX SaaS-foundation VPS deployment

A reproducible, Docker-based VPS deployment that runs the complete Dograh
voice AI stack — **including FreeSWITCH itself with `mod_audio_stream`** —
in one Compose stack, designed as the first building block of a
multi-tenant AI Call Center SaaS platform rather than a one-off copy of the
current Raspberry Pi + local Docker setup it's migrated from.

**This is additive.** It doesn't change `docker-compose.yaml`,
`docker-compose.freeswitch.yml`, or `deploy/hostinger/` — those keep working
exactly as they do today. See `MIGRATION_PLAN.md`'s backward-compatibility
note.

## Start here

- **New to this deployment?** Read `ARCHITECTURE.md` first — the design
  rationale (tenant model, service boundaries, why FreeSWITCH is containerized
  this way) that everything else builds on.
- **Ready to deploy?** `VPS_DEPLOYMENT_GUIDE.md` — how to run `install.sh`
  and verify it worked.
- **Testing on a local PC before a VPS exists?** `LOCAL_STAGING_GUIDE.md` —
  running this same stack on a Windows/Docker Desktop dev machine as a
  staging stand-in.
- **Planning an actual cutover from the Pi?** `MIGRATION_PLAN.md`.
- **Something broken?** `TROUBLESHOOTING.md`.

## Files

| File | Role |
| --- | --- |
| `docker-compose.vps.yml` | The full stack: `freeswitch`, `freeswitch-manager`, `api`, `ui`, `redis`, `postgres`, `minio`, `cloudflared` |
| `install.sh` | Installs Docker, builds `.env` interactively, brings the stack up |
| `.env.example` | Bootstrap env var contract, placeholders only — copy to `.env` and fill in |
| `freeswitch/` | `Dockerfile` (builds FreeSWITCH 1.10.12 + `mod_audio_stream` from source), `docker-entrypoint.sh` (renders bootstrap config from env vars at container start), `conf/` (config templates) |
| `scripts/backup.sh` / `scripts/restore.sh` | Timestamped backup / destructive restore of Postgres, MinIO, and FreeSWITCH config |
| `RASPBERRY_FREESWITCH_BACKUP.md` | Read-only audit of the source Raspberry Pi FreeSWITCH install this deployment is derived from |
| `DOGRAH_CURRENT_ARCHITECTURE.md` | Audit of the existing Dograh Docker stack (unaffected by this work) |
| `ARCHITECTURE.md` | Current + target-state architecture: tenant model, service boundaries, FreeSWITCH-as-infrastructure principle, future Configuration Service spec, event model, internal API contracts, future monitoring, Kubernetes-friendliness, security model, operational excellence |
| `VPS_ARCHITECTURE.md` | Concrete network/container diagram for this specific deployment |
| `VPS_FIREWALL.md` | Firewall rules (ufw + iptables) and rationale |
| `SAAS_ROADMAP.md` | Phase 1-5 path from this single-VPS bootstrap deployment to a horizontally-scaled multi-tenant platform |
| `MIGRATION_PLAN.md` | Sequencing for an actual production cutover from the Pi (not executed yet) |
| `VPS_DEPLOYMENT_GUIDE.md` | How to run `install.sh` and verify the result |
| `LOCAL_STAGING_GUIDE.md` | Running this same stack locally on a Windows/Docker Desktop dev PC as a staging stand-in before a VPS exists |
| `docker-compose.local.override.yml` | Local-only: publishes `api`/`ui` host ports for LAN browser access. Never use on a real VPS — see `LOCAL_STAGING_GUIDE.md` |
| `BACKUP_RESTORE.md` | How to use `scripts/backup.sh`/`restore.sh`, plus future per-tenant data handling |
| `TROUBLESHOOTING.md` | SIP registration, no-audio, ESL ACL, TLS/wss, and module-load failure modes |

## Quick start

```bash
cd deploy/vps-ai-pbx
./install.sh
```

See `VPS_DEPLOYMENT_GUIDE.md` for what this prompts for and how to verify it
worked.
