# Dograh Current Docker Architecture — Audit

Audit of the existing, already-working Dograh Docker stack, as it stands in
this repository today, before any VPS/FreeSWITCH-container work is added.
This is the baseline the new `deploy/vps-ai-pbx/docker-compose.vps.yml`
extends — nothing described here changes as part of this migration.

## Existing deployment paths (all unchanged by this work)

| Path | File(s) | Purpose |
| --- | --- | --- |
| Local dev | `docker-compose.yaml` (default, no profiles) | `postgres`, `redis`, `minio`, `api`, `ui` only |
| Local + FreeSWITCH provider | `docker-compose.yaml` + `docker-compose.freeswitch.yml` (`--profile freeswitch`) | Adds `freeswitch-manager` (ESL listener), connects out to an **operator-run** FreeSWITCH box — does not run FreeSWITCH itself |
| Remote/self-hosted (own reverse proxy) | `docker-compose.yaml` (`--profile remote`) + `scripts/setup_remote.sh` + `remote_up.sh` | Adds `nginx`, `coturn`, `dograh-init` (renders nginx/coturn config from env) |
| Hostinger (managed Traefik) | `deploy/hostinger/docker-compose.yaml` + `.env.example` | Self-contained app stack (`ui`, `api`, `minio` behind Traefik labels), assumes the platform already runs Traefik + Let's Encrypt |
| Tunnel (no public IP) | `docker-compose.yaml` (`--profile tunnel`) | Adds `cloudflared`, either a named tunnel (stable hostname) or an ephemeral quick-tunnel |

**None of these define or run FreeSWITCH itself.** `docker-compose.freeswitch.yml`'s
own header comment is explicit: it "does not run FreeSWITCH itself" —
operators bring their own box. This is exactly the gap
`deploy/vps-ai-pbx/` closes for the VPS case (Phase 4/5 of the migration).

## Containers (base `docker-compose.yaml`)

| Service | Image | Notes |
| --- | --- | --- |
| `postgres` | `pgvector/pgvector:pg17` | `postgres_data` volume, healthcheck via `pg_isready`, `json-file` logging capped `10m`×3 |
| `redis` | `redis:7` | password-protected (`REDIS_PASSWORD`), `redis_data` volume |
| `minio` | `minio/minio` | bound to `127.0.0.1` only by default (not exposed beyond the host), `minio-data` volume, console on `9001` |
| `dograh-init` | `bash:5.2` | profile `remote`/`local-turn` only — renders nginx/coturn config from env vars into named volumes via `scripts/run_dograh_init.sh`, then exits (`service_completed_successfully`) |
| `nginx` | `nginx:alpine` | profile `remote` only — TLS termination + reverse proxy, depends on `dograh-init` |
| `coturn` | `coturn/coturn:4.8.0` | profile `remote`/`local-turn` — WebRTC TURN relay, ports `3478`/`5349` tcp+udp, `49152-49200/udp` |
| `api` | `${REGISTRY:-dograhai}/dograh-api:latest` | FastAPI backend, port `8000`, healthcheck hits `/api/v1/health` |
| `ui` | `${REGISTRY:-dograhai}/dograh-ui:latest` | Next.js standalone server, port `3010` |
| `cloudflared` | `cloudflare/cloudflared:latest` | profile `tunnel` — named or quick tunnel, metrics on `2000` |

`freeswitch-manager` (from `docker-compose.freeswitch.yml`, profile
`freeswitch`) reuses the **same** `dograh-api` image with a different
`command` (`./scripts/run_freeswitch_manager.sh`) — no separate image, and
this is exactly the pattern the new `deploy/vps-ai-pbx/docker-compose.vps.yml`
keeps for that service.

## Environment variable contract (already established)

The variables the new VPS compose file must not rename, since they're the
existing convention every deployment path already shares:

- **App/DB**: `POSTGRES_PASSWORD`, `REDIS_PASSWORD`, `MINIO_ROOT_USER`,
  `MINIO_ROOT_PASSWORD`, `OSS_JWT_SECRET` (required, no default).
- **Public addressing**: `PUBLIC_BASE_URL`, `PUBLIC_HOST` — the API derives
  `BACKEND_API_ENDPOINT`/`MINIO_PUBLIC_ENDPOINT`/`TURN_HOST` from these when
  unset (`api/constants.py`, `api/utils/common.py`); when the resolved value
  is non-public (localhost/private IP), the API falls back to discovering a
  running Cloudflare tunnel's URL at runtime.
- **S3/MinIO**: `ENABLE_AWS_S3`, `S3_*` (only used if not using bundled
  MinIO).
- **TURN**: `TURN_HOST`, `TURN_SECRET`, `FORCE_TURN_RELAY`.
- **Cloudflare**: `CLOUDFLARE_TUNNEL_TOKEN` (unset → quick tunnel).
- **Workers**: `FASTAPI_WORKERS` (drives nginx upstream rendering in the
  `remote` profile).
- **freeswitch-manager-specific** (from `docker-compose.freeswitch.yml`):
  same `PUBLIC_BASE_URL`/`PUBLIC_HOST`/`BACKEND_API_ENDPOINT`,
  `DATABASE_URL`, `REDIS_URL`, `OSS_JWT_SECRET` — no FreeSWITCH-box
  connection details here, since those live per-org in
  `telephony_configurations` (ESL host/port/password), not in this
  container's env.

## Networking

Single bridge network `app-network` in every existing compose file. No
service reaches another via a fixed IP anywhere — everything is Docker
service-name DNS (`postgres`, `redis`, `minio`, `api`). This convention is
carried into `ai-pbx-network` for the VPS deployment unchanged.

## Volumes

All named volumes, `driver: local`, no host bind-mounts in any of the
existing compose files (`deploy/hostinger/README.md` calls this out
explicitly as a design requirement: "named volumes only, no host
bind-mounts"). The VPS deployment's `freeswitch-config` volume follows the
same rule.

## What the new VPS stack adds (not present in any existing path)

1. A **`freeswitch`** service that actually runs FreeSWITCH + `mod_audio_stream`
   in a container (`deploy/vps-ai-pbx/freeswitch/Dockerfile`) — closing the
   gap called out above.
2. `freeswitch-manager` pointed at that in-stack `freeswitch` service over
   the internal network (`freeswitch:8021`), instead of at an
   externally-run box.
3. Bootstrap FreeSWITCH config env vars (`SIP_EXTERNAL_IP`, `RTP_PUBLIC_IP`,
   `ESL_PASSWORD`, `DOGRAH_ESL_ALLOWED_CIDRS`, `SIP_GATEWAY_*`) — new,
   FreeSWITCH-specific, additive to the existing contract above, not a
   replacement of it.

Everything else (`api`, `ui`, `redis`, `postgres`, `minio`, `cloudflared`,
their images, healthchecks, and env vars) is reused as-is from the existing
`docker-compose.yaml` — see `docker-compose.vps.yml` itself for the exact
merge.
