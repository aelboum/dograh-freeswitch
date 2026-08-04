# VPS Architecture — deploy/vps-ai-pbx

Concrete network/container design for this deployment. For the broader
SaaS-platform architecture (tenant model, service boundaries, future
services), see [`ARCHITECTURE.md`](./ARCHITECTURE.md) — this document is the
"how it's actually wired" companion to that "why it's designed this way"
document.

## Network diagram

```
                              Internet
                                 │
                    ┌────────────┼─────────────────┐
                    │            │                 │
               SIP Provider  Cloudflare      (future: SIP Proxy,
              (5060/udp+tcp,  Tunnel          not built this pass —
               RTP 20000-      (HTTPS/WSS,     see ARCHITECTURE.md §12)
               20199/udp,      no inbound
               configurable)   ports needed)
                    │               │
        ┌───────────▼───────────────▼───────────────────────────┐
        │  VPS host — ai-pbx-network (Docker bridge)             │
        │                                                        │
        │   ┌────────────┐        ┌─────────────┐                │
        │   │ freeswitch │──ESL──►│ freeswitch- │                │
        │   │ (container)│  8021  │  manager    │                │
        │   │            │ (internal only,      │                │
        │   │            │  never published)    │                │
        │   └─────┬──────┘        └──────┬──────┘                │
        │         │ mod_audio_stream      │                      │
        │         │ (wss://, internal)    │                      │
        │         ▼                       ▼                      │
        │   ┌────────────────────────────────────┐               │
        │   │            dograh-api               │◄── cloudflared
        │   └──────┬───────────────┬──────────────┘               │
        │          │               │                              │
        │   ┌──────▼─────┐  ┌──────▼─────┐  ┌───────────┐         │
        │   │  postgres  │  │   redis    │  │  minio    │         │
        │   └────────────┘  └────────────┘  └───────────┘         │
        │                                                          │
        │   ┌────────────┐                                        │
        │   │ dograh-ui  │◄── cloudflared                          │
        │   └────────────┘                                        │
        └──────────────────────────────────────────────────────────┘
```

## Container communication

| From | To | Protocol / port | Published on host? |
| --- | --- | --- | --- |
| SIP Provider | `freeswitch` | SIP, `5060/udp+tcp` | **Yes** — must be public |
| SIP Provider (media) | `freeswitch` | RTP, `20000-20199/udp` (`RTP_PORT_RANGE_START/END`, capacity-sized default — see note below) | **Yes** — must be public |
| `freeswitch-manager` | `freeswitch` | ESL, `8021/tcp` | **No** — internal network only (`freeswitch:8021`) |
| `freeswitch` | `dograh-api` | `mod_audio_stream` WSS, dynamic per-call URL | No — internal network, resolved via `get_backend_endpoints()` |
| `dograh-api`/`freeswitch-manager` | `postgres` | `5432/tcp` | No — internal only |
| `dograh-api`/`freeswitch-manager` | `redis` | `6379/tcp` | No — internal only |
| `dograh-api` | `minio` | `9000/tcp` | No — internal only |
| `cloudflared` | `dograh-api`, `dograh-ui` | HTTP, internal | No — cloudflared makes the outbound tunnel connection; nothing inbound needed |
| Operator (you) | `dograh-ui` / `dograh-api` | HTTPS, via Cloudflare tunnel | Public, but only through Cloudflare's edge, not a direct host port |

**Only two things are ever exposed directly on the VPS's public IP: SIP
(`5060`) and RTP (`20000-20199` by default).** Everything else routes
through the Cloudflare tunnel (outbound-initiated, no inbound port needed)
or stays strictly internal to `ai-pbx-network`.

**Why 200 ports, not the Pi's 16384-32768 (~16k ports):** the source
Raspberry Pi deployment uses FreeSWITCH's compiled-in default range (see
`RASPBERRY_FREESWITCH_BACKUP.md`) because a bare-metal/systemd install has
no per-port publishing cost. Docker bridge networking does — publishing
16k+ individual UDP ports means docker-proxy/iptables set up that many rules
just to start the container, which is impractically slow. `docker-entrypoint.sh`
narrows FreeSWITCH's own `rtp-start-port`/`rtp-end-port` to match exactly
what `docker-compose.vps.yml` publishes (`RTP_PORT_RANGE_START`/`_END`,
default `20000`-`20199`), sized for roughly 100 concurrent two-way audio
calls — ample for this single-tenant bootstrap deployment. Widen both
together in `.env` if real concurrent-call volume ever needs more.

## Exposed ports (summary — full rules + rationale in `VPS_FIREWALL.md`)

| Port | Protocol | Purpose | Exposure |
| --- | --- | --- | --- |
| `5060` | UDP + TCP | SIP signaling | Public (required) |
| `20000-20199` (configurable) | UDP | RTP media | Public (required) |
| `8021` | TCP | ESL | **Never public** — internal Docker network only |
| `80`/`443` | TCP | HTTP(S) | Only if not using Cloudflare tunnel exclusively |

## Tenant Model (summary — full table and rationale in `ARCHITECTURE.md` §2)

This deployment is explicitly **single-tenant** at the FreeSWITCH layer: one
container, one bootstrap gateway. Everything above FreeSWITCH
(`telephony_configurations`, `telephony_phone_numbers`, `workflows`,
`workflow_runs`, `workflow_recordings`) is already tenant-scoped by
`organization_id` in Dograh's existing data model — this deployment doesn't
change that, it just doesn't yet extend tenant-partitioning down into
FreeSWITCH itself. See `ARCHITECTURE.md` §2 for the full entity table and
`SAAS_ROADMAP.md` Phase 3 for how that gap closes.

## Future SIP routing layer (reserved, not built)

```
Internet → SIP Proxy (future: Kamailio/OpenSIPS) → FreeSWITCH Cluster → Dograh
```

Not implemented or scaffolded in this deployment — see `ARCHITECTURE.md` §12
for the full rationale. Reserved here only so the diagram above doesn't
imply a dead end once a second FreeSWITCH instance is added.
