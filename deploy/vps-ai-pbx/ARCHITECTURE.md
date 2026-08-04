# Architecture — AI Call Center SaaS Platform (Current + Target)

This document describes both **what is actually deployed** by
`deploy/vps-ai-pbx/` (one VPS, one FreeSWITCH instance, single bootstrap SIP
trunk) and the **target architecture** this deployment is the first building
block of (a multi-tenant AI Call Center SaaS platform). The two are kept
explicitly separate throughout — nowhere in this document should "current"
and "future" be read as the same thing already being true.

Companion documents: [`VPS_ARCHITECTURE.md`](./VPS_ARCHITECTURE.md) (this
deployment's concrete network/container diagram),
[`SAAS_ROADMAP.md`](./SAAS_ROADMAP.md) (phased path from here to there),
[`DOGRAH_CURRENT_ARCHITECTURE.md`](./DOGRAH_CURRENT_ARCHITECTURE.md) /
[`RASPBERRY_FREESWITCH_BACKUP.md`](./RASPBERRY_FREESWITCH_BACKUP.md) (audit
inputs this design is built from).

---

## 1. Current state (what this deployment actually is)

One VPS, one Docker Compose stack (`docker-compose.vps.yml`), one FreeSWITCH
container, one bootstrap SIP trunk configured directly in `.env`. Everything
below this line that says "future" or "not built this pass" is exactly
that — documented intent, not running code.

```
Internet
   │
   ├─ SIP Provider ──────────► FreeSWITCH (container, this VPS)
   │                                │  ESL (internal network only)
   │                                ▼
   │                          freeswitch-manager
   │                                │
   └─ Cloudflare Tunnel ──────► dograh-api ──► Postgres / Redis / MinIO
                                     │
                                dograh-ui
```

---

## 2. Tenant Model

Dograh's existing `organization_id` model **is** the tenant boundary — this
is not a new concept introduced for this migration. Confirmed directly in
`api/db/models.py` (`organizations` is the root table) and enforced as
policy in `api/AGENTS.md`'s "Organization Scoping (Security)" section (every
org-scoped read/write must filter by it; a foreign key existing doesn't
prove the caller may reference it — that must be checked explicitly).

| Entity | Current state | Tenant-scoped today? |
| --- | --- | --- |
| Tenant | `organizations` table | — (this *is* the boundary) |
| Users / roles | `users`, org membership (`selected_organization_id`) | Yes |
| SIP gateways | `telephony_configurations` (provider=`freeswitch`; holds ESL host/creds/dial_prefix) | Yes — but today only **one config is realistically usable per FreeSWITCH box**, since the box itself (its gateway XML, its ESL listener) isn't tenant-partitioned. Outbound calls with no explicit config id also can't disambiguate between multiple configs yet (documented gap already in `api/services/telephony/providers/freeswitch/DESIGN.md`'s "Known v1 constraints") |
| Phone numbers | `telephony_phone_numbers` (→ `inbound_workflow_id`) | Yes |
| Extensions | Not modeled as a distinct entity yet | **Gap** — real gap for a multi-tenant PBX; closed in `SAAS_ROADMAP.md` Phase 3 |
| AI agents / workflows | `workflows`, `workflow_definitions` | Yes |
| Call logs | `workflow_runs` | Yes |
| Call recordings | `workflow_recordings` (MinIO-backed) | Yes |

**Everything above FreeSWITCH in the stack is already multi-tenant-ready.**
The one deployed FreeSWITCH instance and its one bootstrap gateway are
explicitly single-tenant for this pass — that is the one specific,
named gap (one shared box, one XML gateway file, ESL not partitioned by
tenant), and it's exactly what the roadmap phases in, not something this
migration silently papers over.

**Rule for every future entity** (documented, not enforced by new code this
pass): any future telephony object — gateway, extension, dialplan rule,
recording, transcript — must carry `organization_id` and be created only
through a code path that validates the caller's `organization_id` owns any
referenced parent row. This is the existing Organization Scoping rule
applied consistently as new tables get added, not a new rule invented for
telephony.

**Recommendations for future multi-tenant hardening** (documented, not
implemented):

- **Tenant isolation** — extend the existing org-scoping convention to
  FreeSWITCH-adjacent data as it's added (future gateway/extension tables).
  No shared FreeSWITCH state should be readable/writable cross-tenant once
  multiple tenants share infrastructure (Phase 4+).
- **Authorization** — future Configuration Service endpoints (§4) follow
  the same role/permission model as the rest of the API; no separate auth
  scheme for telephony config.
- **Audit logging** — any future write to tenant-owned telephony config
  (gateway created/changed, extension provisioned) should be logged
  attributably (who, which org, what changed, when). This doesn't exist yet
  for telephony config specifically — flagged as a gap to close in
  Phase 2/3, not something already covered elsewhere.
- **Configuration ownership** — the Configuration Service (§4), not
  FreeSWITCH and not an operator's shell session, is the single writer of
  tenant-derived FreeSWITCH config once it exists. FreeSWITCH only ever
  reads (via `mod_xml_curl`) — it never becomes a second source of truth.

---

## 3. Service boundaries (logical now, physically separable later)

These boundaries already exist in the codebase's module structure — this
table makes them explicit so future extraction into independent services
doesn't require a redesign, per `api/AGENTS.md`'s "Routes vs Service Layer"
split and the telephony providers' strict self-containment contract
(`api/services/telephony/providers/AGENTS.md`: a provider touches its own
folder plus exactly two lines elsewhere).

| Logical service | Lives today in | Future extraction note |
| --- | --- | --- |
| API Gateway | `api/routes/` (mounted under `/api/v1`), fronted by `nginx`/Traefik/Cloudflare tunnel depending on deployment path | Already the outermost seam; a dedicated API gateway later is a routing-layer swap, not an app change |
| Authentication | `api/routes` auth handlers + `OSS_JWT_SECRET`-based JWT | Already isolated behind route handlers; extractable without touching business logic |
| Tenant Management | `organizations`/`users` tables + the org-scoping convention | Already a distinct concern; needs a dedicated provisioning API surface (`SAAS_ROADMAP.md` Phase 2) before extraction is worthwhile |
| Telephony Service | `api/services/telephony/` (provider registry + per-provider packages, incl. `providers/freeswitch/`) | Already self-contained per `providers/AGENTS.md`'s "two edits outside this folder" contract — the strictest existing boundary in the codebase, a template for the others |
| AI Workflow Service | `api/services/workflow/`, `api/services/pipecat/`, `api/tasks/` | Already separated from telephony by the transport abstraction (`create_transport`) |
| Configuration Service (future) | Doesn't exist yet — closest today is the static `envsubst` templates in `deploy/vps-ai-pbx/freeswitch/conf/` | New service; see §4 |
| Media Service | `mod_audio_stream` (FreeSWITCH-side) + provider `transport.py`/`serializers.py` (Dograh-side) | Already the narrowest interface in the stack — one WS wire format, documented in `DESIGN.md` |
| Notification Service | Doesn't exist as a distinct concern yet (webhooks exist per-provider, e.g. `WebhookDeliveryModel`) | Candidate for consolidation later; no urgency, just noted so it isn't forgotten |

No code moves in this pass. This table exists to be referenced by future
work instead of re-derived each time someone asks "where would X live."

---

## 4. FreeSWITCH as infrastructure, not business logic

**Principle** (reinforcing, not changing, what
`providers/freeswitch/DESIGN.md` already chose): Dograh talks to FreeSWITCH
only through well-defined interfaces —

- **ESL today** (commands + events, via the hand-rolled `esl_client.py`),
- **XML configuration later** (via the future Configuration Service below),
- **the existing `mod_audio_stream` media-streaming wire format**.

No business logic (workflow routing, tenant resolution, AI decisioning) is
ever pushed into FreeSWITCH's own config or dialplan beyond the minimal
`park()` handoff it already requires. This keeps FreeSWITCH itself
replaceable or horizontally scalable (roadmap Phase 4/5) without requiring
any Dograh application change — the interfaces are the contract, not the
specific FreeSWITCH box behind them.

### 4a. FreeSWITCH config: bootstrap now, tenant runtime later

Two configuration classes, kept explicitly distinct in this deployment:

- **Bootstrap config** (real, in `.env`, this pass): platform-level only —
  database/redis/minio creds, Cloudflare tunnel token, `ESL_PASSWORD`,
  `SIP_EXTERNAL_IP`, `RTP_PUBLIC_IP`, and exactly one bootstrap SIP trunk
  (`SIP_GATEWAY_*`). Rendered once at container start via `envsubst`
  templates (mirroring `dograh-init`'s existing render-at-startup pattern).
- **Tenant runtime config** (documented future direction, **not built this
  pass**): customer SIP trunk credentials, DIDs, extensions. These must
  **not** be added to `.env` per customer — `.env` is a bootstrap file for
  one operator-level trunk, not a scaling mechanism. Real customer gateways
  belong in Postgres, provisioned through the app:

  ```
  FreeSWITCH (mod_xml_curl / mod_sofia)
        ↑ HTTP XML fetch, per lookup
  Dograh Telephony Provider (api/services/telephony/providers/freeswitch/)
        ↑
  Configuration Service (new, future — not built this pass)
        ↑
  PostgreSQL (telephony_configurations, telephony_phone_numbers, + a future
              per-tenant gateway/extension table)
  ```

  This deployment's XML templates remain static-rendered-at-boot — perfectly
  fine for a single-tenant bootstrap deployment. No manual XML editing
  should be *required* after this deployment for anything covered by the
  bootstrap set; the diagram above is how future per-tenant XML gets
  generated instead of hand-edited.

### 4b. Future Configuration Service — responsibilities (documented only)

Spelled out precisely so it's buildable later without re-deriving the
design:

- **Owns**: tenant SIP gateways, extensions, dialplans, directory entries,
  ACL generation (per-tenant/per-gateway allow-lists, replacing the single
  static `dograh` ACL list this deployment ships), XML generation
  (gateway/directory/dialplan XML rendered from Postgres rows on demand).
- **Interface**: exposes `mod_xml_curl` HTTP endpoints FreeSWITCH calls at
  lookup time (`section=configuration|dialplan|directory`, FreeSWITCH's
  standard `mod_xml_curl` request contract). FreeSWITCH never reads
  Postgres directly — only this service.
- **Consumers**: FreeSWITCH (via `mod_xml_curl`), and indirectly the
  Telephony Service/Dograh API, which writes the Postgres rows this service
  reads from — the Telephony Service doesn't write XML itself, the
  Configuration Service does.
- **Explicitly not this pass**: no `mod_xml_curl` wiring, no service
  scaffold, no new Postgres tables. This is a responsibilities-and-interface
  spec only.

---

## 5. Event model (current vs. future)

**Already generated today** — FreeSWITCH ESL events, consumed directly by
`esl_manager.py`, not published as durable domain events outside that
process: `CHANNEL_PARK` (inbound call ready), `CHANNEL_ANSWER`,
`CHANNEL_HANGUP`/`CHANNEL_HANGUP_COMPLETE`, the custom
`mod_audio_stream::play`/`::connect` events. These are real, current,
internal-only signals.

**Future domain events** (documented, not implemented): `IncomingCall`,
`CallAnswered`, `AIStarted`, `AIFinished`, `RecordingCompleted`,
`WorkflowFinished` — the natural durable, tenant-attributed events a future
Notification Service or external webhook/analytics consumer would subscribe
to.

| Future event | Would be published from | Would be consumed by |
| --- | --- | --- |
| `IncomingCall` | `esl_manager.py`'s `CHANNEL_PARK` handler | Notification Service, customer webhooks |
| `CallAnswered` | `esl_manager.py`'s `CHANNEL_ANSWER` handler | Analytics, customer webhooks |
| `AIStarted` / `AIFinished` | Workflow run state transition (`workflow_runs`) | Analytics, billing/usage |
| `RecordingCompleted` | `workflow_recordings` write completion | Notification Service, tenant export tooling |
| `WorkflowFinished` | `workflow_runs` terminal state | Analytics, customer webhooks |

This is additive: today's direct ESL-event-to-action handling in
`esl_manager.py` keeps working unchanged regardless of whether an event bus
is ever added — the bus would sit alongside it, not replace it.

---

## 6. Internal API contracts

Today's real interfaces — documented as the basis for any future
microservice extraction, since these are already the seams:

| Interface | Today | Contract documented in |
| --- | --- | --- |
| Dograh API ↔ FreeSWITCH Manager | Shared Postgres/Redis (`fs:channel:{uuid}` mapping); no direct RPC | `api/services/telephony/providers/freeswitch/esl_manager.py`, `DESIGN.md` |
| FreeSWITCH Manager ↔ ESL | Hand-rolled ESL client (`auth`/`api`/`bgapi`/event-subscription) | `esl_client.py` |
| Configuration Service ↔ FreeSWITCH (future) | `mod_xml_curl` HTTP fetch | §4b above |
| Media Streaming ↔ AI Pipeline | `mod_audio_stream` WS wire format (raw PCM in, JSON `streamAudio` out) | `DESIGN.md`'s "Media: mod_audio_stream" section, `serializers.py` |

No interface changes are proposed in this pass — this is a record of what
exists, for later services to build against.

---

## 7. Future Monitoring (documentation only — nothing built this pass)

| Domain | Metrics | Likely source |
| --- | --- | --- |
| FreeSWITCH | Active calls, calls-per-second, concurrent channels, RTP packet loss, MOS, codec usage | FreeSWITCH's own ESL/CDR event stream, or a small ESL-polling exporter |
| AI pipeline | STT/TTS/LLM latency, workflow execution time | `pipecat`'s existing turn-latency breakdown logging (see recent repo history, e.g. commits `533fab8c`/`1c4ff965`) — the natural source to export from rather than building new instrumentation from scratch |
| Infrastructure | CPU/RAM/disk, Redis, Postgres, container health | Standard `node_exporter`/`cadvisor`/`postgres_exporter`/`redis_exporter`, wired to Prometheus + Grafana |

All of the above would be optional add-on services in a later Compose
profile (or a Kubernetes sidecar/DaemonSet in Phase 5) — not part of this
deployment. This pass's actual observability is limited to Docker
healthchecks and `json-file` logs (§9), which is sufficient for a
single-tenant bootstrap deployment.

---

## 8. Kubernetes-friendliness checklist

Confirms the choices made in this deployment don't create a dead end for a
later Kubernetes migration (roadmap Phase 5):

- ✅ No host bind-mounts anywhere — named volumes only, which translate
  directly to PVCs.
- ✅ No `localhost`/hardcoded-IP assumptions anywhere — service-name DNS
  resolution throughout (`freeswitch:8021`, `postgres:5432`, etc.).
- ✅ All configuration via environment variables — maps directly to
  ConfigMaps/Secrets.
- ✅ FreeSWITCH treated as a stateless runtime with config rendered at
  start, not hand-edited post-boot — maps to an init-container + ConfigMap
  pattern.
- ✅ `freeswitch-manager`/`api`/`ui` are already horizontally-replicable (no
  in-process state that isn't already in Redis/Postgres).
- ⚠️ `freeswitch` itself is **not yet** horizontally scalable (single
  instance, stateful for calls-in-flight) — this is expected and explicitly
  deferred to roadmap Phase 4 (multiple FreeSWITCH instances + SIP proxy
  layer), not a gap in this checklist.

---

## 9. Security model

**Implemented in this deployment**:
- ESL (`8021`) never published on the host — reachable only over the
  internal Docker network by service name. Enforced at the network layer,
  not just FreeSWITCH's own ACL.
- FreeSWITCH's own `acl.conf.xml` allow-list (`apply-inbound-acl`) as
  defense in depth on top of the network-layer restriction above.
- Secrets (`ESL_PASSWORD`, SIP trunk credentials, DB/Redis/MinIO passwords,
  `OSS_JWT_SECRET`) live only in `.env`, never committed, never written into
  any documentation (see `RASPBERRY_FREESWITCH_BACKUP.md`'s explicit note on
  this).
- Host firewall rules restricting inbound traffic to only what's needed —
  see `VPS_FIREWALL.md`.

**Documented recommendations for future hardening** (not implemented this
pass):
- **Secrets management**: move from `.env`-file secrets to a real secrets
  manager (Vault, cloud KMS, or Kubernetes Secrets in Phase 5) once
  multiple operators/environments need independent credential rotation.
- **TLS certificates**: automate renewal (Let's Encrypt/ACME, already the
  pattern in `deploy/hostinger/docker-compose.traefik.yaml`) rather than
  manual cert management, once this deployment fronts real customer
  traffic on its own domain.
- **SIP fraud prevention**: alert on sudden spikes in concurrent outbound
  calls, calls to premium/international ranges outside a tenant's normal
  pattern, or registration attempts from unexpected source IPs.
- **Rate limiting**: at the API-gateway layer (§3), before it becomes a
  distinct service — bound the abuse surface for both HTTP and future
  Configuration Service endpoints.
- **`fail2ban`**: for SIP `REGISTER`/`INVITE` brute-force attempts against
  the exposed `5060` port — see `VPS_FIREWALL.md`.
- **API authentication / RBAC**: already exists at the JWT/organization
  level today; document that any new Configuration Service or Notification
  Service endpoint follows the same model rather than inventing a parallel
  one (see §2's "Authorization" recommendation).
- **Audit logs**: see §2's "Audit logging" recommendation — same gap,
  cross-referenced here since it's also a security concern, not just a
  tenancy one.

---

## 10. Operational excellence (documentation only)

- **Rolling deployments / zero-downtime upgrades**: possible today via
  `docker compose up -d --no-deps <service>` per-service, since nothing is
  in a single-replica stateful container except the databases themselves.
- **Version compatibility**: pin image tags explicitly (not `:latest`) for
  anything beyond local testing, so a redeploy never silently picks up an
  untested version.
- **Database migrations**: reuses the existing
  `scripts/makemigrate.sh`/`scripts/migrate.sh` flow, run before swapping
  the `api` image — not a new mechanism.
- **Rollback strategy**: keep the prior image tag plus a `pg_dump` taken
  immediately before migrating; reverting is re-deploying the old tag and,
  if the migration was destructive, restoring that dump.
- **Disaster recovery**: restore from `scripts/backup.sh` output (see
  `BACKUP_RESTORE.md`) onto a fresh VPS, using `install.sh` to rebuild the
  stack shape first.

## 11. CI/CD (documentation only — see `SAAS_ROADMAP.md` for the full pipeline description)

Expected future pipeline, modeled on the pattern already proven in
`aelboum/mod_audio_stream`'s own GitHub Actions build (a real, working CI
referenced in this project's history, not a hypothetical technology
choice): `GitHub → GitHub Actions → build images → push to registry →
deploy to VPS → health checks → automatic rollback if unhealthy`. The
health-check step reuses the same `docker compose ps`/healthcheck
definitions already in `docker-compose.vps.yml` rather than inventing a
separate liveness mechanism.

---

## 12. Target Architecture (long-term vision — not deployed this pass)

```
Internet
   ↓
Cloudflare
   ↓
AI Dashboard (Dograh UI)
   ↓
Dograh API
   ↓
FreeSWITCH Cluster
   ↓
Future SIP Proxy
   ↓
SIP Providers
   ↓
Customers
```

Paired explicitly against §1's current-state diagram: this deployment still
runs exactly **one** FreeSWITCH instance and **one** bootstrap SIP trunk. The
diagram above is where the architecture grows *without a redesign* — every
box in it already has a home in the Service Boundaries table (§3) or a
named phase in `SAAS_ROADMAP.md`. Nothing here is scaffolded or stubbed out
in code this pass.

### SIP routing layer, reserved but not built

```
Internet → SIP Proxy (future: Kamailio/OpenSIPS) → FreeSWITCH Cluster → Dograh
```

A SIP proxy layer becomes necessary once there's more than one FreeSWITCH
instance (roadmap Phase 4) — it's what would do trunk-to-tenant
routing/load-balancing/registration-fanout before a call ever reaches a
specific FreeSWITCH box. Reserved in this diagram only so today's
single-instance deployment doesn't imply SIP traffic terminates in a way
incompatible with adding this layer later. Kamailio/OpenSIPS are named as
the likely candidates (both are the standard choice for this role) — no
implementation, no evaluation between them performed or needed yet.
