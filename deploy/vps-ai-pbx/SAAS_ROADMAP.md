# SaaS Roadmap — from single-VPS bootstrap to multi-tenant AI Call Center platform

A direction document, not a commitment — no dates, no estimates. Each phase
builds on the previous one without requiring a redesign of what came before,
because `ARCHITECTURE.md` documents the service boundaries, tenant model,
and interfaces this roadmap is built against.

## Phase 1 — this deployment

- Single VPS.
- Single FreeSWITCH instance (`deploy/vps-ai-pbx/freeswitch/`).
- Docker Compose (`docker-compose.vps.yml`).
- Single bootstrap SIP trunk, configured directly in `.env`
  (`SIP_GATEWAY_*` — explicitly labeled as a stopgap, see
  `ARCHITECTURE.md` §4a).
- Everything above FreeSWITCH already multi-tenant-capable
  (`organization_id` throughout — `ARCHITECTURE.md` §2), even though only
  one tenant realistically uses this single FreeSWITCH box today.

## Phase 2 — tenant management maturation

- Make explicit, dashboard-facing use of the multi-tenant database model
  that already exists (`organizations`/`organization_id` — nothing new to
  build in the schema, just in the UI/API surface around it).
- Customer provisioning workflow: an operator (not the customer) can create
  a new tenant, invite users, and set up their first telephony
  configuration through the existing UI/API — no new database concepts,
  just a guided flow over what's already there.
- Audit logging for telephony config changes — closes the gap flagged in
  `ARCHITECTURE.md` §2/§9 (doesn't exist yet specifically for telephony
  config).

## Phase 3 — dashboard-driven provisioning

- The **Configuration Service** (`ARCHITECTURE.md` §4b) becomes real:
  automatic SIP gateway creation per tenant via `mod_xml_curl`, replacing
  this deployment's static bootstrap template.
- Automatic extension provisioning — closes the "Extensions" gap in the
  Tenant Model table (`ARCHITECTURE.md` §2).
- Automatic workflow deployment — a tenant's AI agent/workflow becomes
  live on their DID without any manual FreeSWITCH or Dograh-config step.

## Phase 4 — multiple FreeSWITCH instances

- More than one FreeSWITCH instance, for capacity and isolation.
- The SIP Proxy layer (`ARCHITECTURE.md` §12, Kamailio/OpenSIPS-shaped)
  becomes real — it's what routes a trunk's traffic to the right
  FreeSWITCH instance for that tenant.
- Load balancing and high availability across instances.

## Phase 5 — Kubernetes, horizontal scaling

- Move from Docker Compose to Kubernetes, building directly on the
  Kubernetes-friendliness checklist already satisfied in Phase 1
  (`ARCHITECTURE.md` §8: no bind mounts, no hardcoded IPs, env-var
  configuration, stateless-where-possible containers).
- Horizontal scaling of every service that's already replicable today
  (`api`, `ui`, `freeswitch-manager`) plus the FreeSWITCH cluster from
  Phase 4.

## Cross-cutting concerns (apply across phases, not owned by a single one)

- **CI/CD pipeline maturation**: `GitHub → GitHub Actions → build images →
  push to registry → deploy to VPS → health checks → automatic rollback if
  unhealthy`. Modeled on the pattern already proven in
  `aelboum/mod_audio_stream`'s own GitHub Actions build — a real, working
  CI, not a hypothetical technology choice (see `ARCHITECTURE.md` §11 for
  detail). Health checks reuse the same Docker healthcheck definitions
  already in `docker-compose.vps.yml`, extended to whatever orchestrator is
  in use by the time this matters (Compose today, Kubernetes at Phase 5).
- **Monitoring**: grows from "Docker healthchecks + logs" (this
  deployment) toward the full FreeSWITCH/AI/infrastructure metrics stack
  described in `ARCHITECTURE.md` §7, as tenant count and call volume justify
  the operational investment.
- **Security posture**: grows from "firewall + internal-only ESL + secrets
  in `.env`" (this deployment, `VPS_FIREWALL.md`) toward the fuller set
  described in `ARCHITECTURE.md` §9 (secrets manager, automated TLS,
  `fail2ban`, rate limiting, SIP fraud detection, audit logs) as real
  customer traffic and tenant count grow.
