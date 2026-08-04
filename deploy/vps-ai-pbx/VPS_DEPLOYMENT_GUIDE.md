# VPS Deployment Guide

How to actually run this deployment. For the design rationale, see
`ARCHITECTURE.md`; for the sequencing around an actual production cutover,
see `MIGRATION_PLAN.md`. This document is the "how do I run `install.sh` and
know it worked" companion to both.

## Prerequisites

- A VPS with a public IPv4 address, Debian 12 or Ubuntu 22.04+ (or any
  systemd Linux with apt/dnf/yum — `install.sh` detects it).
- Root or sudo access (to install Docker and open firewall ports).
- Your SIP trunk provider's credentials (username, password, realm, proxy)
  ready — you'll be asked for them.
- A domain (or `PUBLIC_HOST=<vps-ip>.sslip.io` if you don't have one yet —
  same no-cost trick documented in `deploy/hostinger/README.md`) for the
  Dograh dashboard.

## Running `install.sh`

```bash
cd deploy/vps-ai-pbx
./install.sh
```

What it does, in order (see the script itself for the exact commands):

1. Confirms it's running inside a real dograh git checkout (clones one to
   `~/dograh` if not).
2. Installs Docker + the Compose plugin if not already present.
3. If `.env` doesn't exist yet, prompts you for every bootstrap value:
   - **VPS's public IP** (`SIP_EXTERNAL_IP`/`RTP_PUBLIC_IP`).
   - **Bootstrap SIP trunk** (`SIP_GATEWAY_*`) — this is your one trunk for
     this initial deployment; see `.env.example`'s header comment and
     `ARCHITECTURE.md` §4a for why this isn't meant to scale per-customer.
   - **Public addressing** (`PUBLIC_BASE_URL`, `PUBLIC_HOST`,
     `AI_BACKEND_URL`).
   - **MinIO root username**, defaulting to `dograh`.
   - **Cloudflare tunnel token** (optional — leave blank for an ephemeral
     quick tunnel, fine for initial testing).
   - Passwords/secrets (`ESL_PASSWORD`, `POSTGRES_PASSWORD`,
     `REDIS_PASSWORD`, `MINIO_ROOT_PASSWORD`, `OSS_JWT_SECRET`) are
     generated for you automatically — you don't need to supply these.
4. Builds the `freeswitch` image from source (several minutes the first
   time — it's compiling FreeSWITCH itself, not pulling a pre-built image;
   see `freeswitch/Dockerfile`) and runs `docker compose up -d`.

If `.env` already exists, `install.sh` skips straight to step 4 — safe to
re-run any time (e.g. after a `git pull` that changed the Dockerfile).

## Open the firewall

Before (or immediately after) `install.sh` finishes, apply the rules in
`VPS_FIREWALL.md` — the SIP trunk won't reach FreeSWITCH otherwise.

## Verifying the deployment came up correctly

```bash
docker compose -f deploy/vps-ai-pbx/docker-compose.vps.yml ps
```

Every service should show `healthy` (or `running` for `cloudflared`, which
has no healthcheck). If `freeswitch` isn't healthy:

```bash
docker compose -f deploy/vps-ai-pbx/docker-compose.vps.yml logs freeswitch
docker compose -f deploy/vps-ai-pbx/docker-compose.vps.yml exec freeswitch fs_cli -x "sofia status"
```

`sofia status` should show your gateway's profile as `RUNNING` and, once the
trunk has had a chance to register (up to `expire-seconds`, typically
within a few seconds in practice), `fs_cli -x "sofia status gateway
<SIP_GATEWAY_NAME>"` should show `REGED`/`UP`. See `TROUBLESHOOTING.md` if
it doesn't.

## Configuring Dograh to use this FreeSWITCH

Same as any FreeSWITCH deployment, per
`api/services/telephony/providers/freeswitch/OPERATOR_GUIDE.md`: in the
Dograh UI, add a telephony configuration with provider **FreeSWITCH**, ESL
host `freeswitch` (the in-stack service reaches it — but if you're
configuring this from the `api` container's perspective, use the service
name; the dashboard form itself talks to `dograh-api`, which is already on
`ai-pbx-network`), ESL port `8021`, and the `ESL_PASSWORD` from your `.env`.
Bind a phone number to a workflow, and you're ready for step 6 of
`MIGRATION_PLAN.md` (test calls).

## Redeploying after a code change

```bash
cd deploy/vps-ai-pbx
docker compose -f docker-compose.vps.yml up -d --build
```

Rebuilds only what changed (Docker layer caching) and restarts affected
services — see `ARCHITECTURE.md` §10 ("Operational excellence") for the
broader rolling-deployment/rollback story.
