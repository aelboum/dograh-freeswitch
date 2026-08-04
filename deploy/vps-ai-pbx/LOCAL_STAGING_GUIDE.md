# Local staging on a Windows PC (Docker Desktop)

How to run this exact deployment — `docker-compose.vps.yml` — on a Windows
development machine as a **temporary, production-like staging stand-in** for
the Raspberry Pi FreeSWITCH box, before it ever touches a real VPS. This
document is the local-machine companion to `VPS_DEPLOYMENT_GUIDE.md`; read
that first for what each service does and how to verify it.

**This is staging, not production.** Secrets in this setup are freshly
generated, local-only values — never copy them to, or reuse them from, a real
VPS `.env`. SIP trunk credentials here are placeholders (`sip.invalid`) by
default; see "Going live locally" below if you actually want this PC to take
real calls in place of the Pi.

## PC requirements

- Windows 10/11 with virtualization enabled (required for Docker Desktop's
  WSL2 backend).
- **Docker Desktop, set to Linux containers** (default on modern installs —
  check via the whale-icon tray menu if unsure. `docker info --format
  '{{.OSType}}'` should print `linux`).
- ~15 GB free disk (FreeSWITCH is compiled from source; the `api` image
  bundles pipecat + ffmpeg + ML deps; the `ui` image runs a full Next.js
  production build).
- A LAN connection with a stable private IPv4 (found via `ipconfig`, see
  below) — Wi-Fi is fine, but if your router issues a new DHCP lease and the
  IP changes, you'll need to update `.env` and restart the stack.

## Docker Desktop requirements

- **Linux containers** (not Windows containers) — this stack has no Windows
  images.
- WSL2 backend recommended (Settings → General).
- No special resource limits needed beyond Docker Desktop's defaults on a
  reasonably modern machine, but the first build (FreeSWITCH from source +
  the `api`/`ui` images) is CPU- and disk-I/O-heavy and can take 10–20
  minutes depending on the machine. Rebuilds after that are fast (Docker
  layer caching).

## Prerequisite: initialize the `pipecat` submodule

The `api` and `freeswitch-manager` images build `pipecat` from source
(`api/Dockerfile`'s `pipecat` install step). If you cloned this repo without
`--recurse-submodules`, that directory is empty and the build fails with
`/tmp/pipecat does not appear to be a Python project`. Fix once, from the
repo root:

```bash
git submodule update --init --recursive
```

## Local IP configuration

Unlike a VPS (which has a real public IP), this deployment runs entirely on
your LAN — closer to how the source Raspberry Pi ran (LAN-only, no public
IP, NAT-traversal via outbound SIP registration) than to the VPS target
architecture. Find your LAN IP:

```powershell
ipconfig
# Look for the "IPv4 Address" under your active Wi-Fi or Ethernet adapter,
# e.g. 192.168.1.179 — NOT 172.x (that's a Docker/WSL virtual adapter) and
# NOT 127.0.0.1.
```

Use that IP — never `localhost`/`127.0.0.1` — for `SIP_EXTERNAL_IP`,
`RTP_PUBLIC_IP`, `PUBLIC_HOST`, `PUBLIC_BASE_URL`, and `AI_BACKEND_URL` in
`.env`. If your LAN IP changes (new DHCP lease), update `.env` and run
`docker compose -f docker-compose.vps.yml up -d` again — the affected
services re-render their config from the new values on restart.

`DOGRAH_ESL_ALLOWED_CIDRS` defaults to `172.16.0.0/12,192.168.1.0/24,127.0.0.1/32`
in this local `.env`, which covers Docker Desktop's actual bridge subnet
(verify with `docker network inspect vps-ai-pbx_ai-pbx-network` — Docker
Desktop for Windows doesn't always land on the same `/16` a plain Linux host
would) plus a typical home LAN. Adjust if your LAN uses a different range
(e.g. `10.0.0.0/24`).

## Start commands

```bash
cd deploy/vps-ai-pbx
cp .env.example .env   # only if you don't already have one — see below
docker compose -f docker-compose.vps.yml -f docker-compose.local.override.yml up -d --build
docker compose -f docker-compose.vps.yml ps   # wait for all healthchecks
```

The second `-f docker-compose.local.override.yml` publishes `api`'s and
`ui`'s ports to the host so a browser on your LAN can reach them — see
"Reaching the UI/API" below for why that's a separate file instead of being
in `docker-compose.vps.yml` itself. Every command in this guide that
operates on the running stack (`ps`, `logs`, `exec`, `down`, ...) works fine
with just `-f docker-compose.vps.yml` since Compose only needs the extra
`-f` when you're changing what's *deployed* (`up`); leave it off freely for
read-only/lifecycle commands once the containers exist.

`--build` is required (not just `up -d`) whenever `freeswitch/`, `api/`,
`ui/`, or `scripts/` changed — see "Why `api`/`ui`/`freeswitch-manager` build
from source" below for why this local `.env`/compose setup builds those
images instead of pulling them.

If `.env` doesn't exist yet, copy `.env.example` and fill in (see that
file's inline comments for every field):

- `SIP_EXTERNAL_IP` / `RTP_PUBLIC_IP` / `PUBLIC_HOST`: your LAN IP.
- `PUBLIC_BASE_URL`: `http://<lan-ip>:3010`, `AI_BACKEND_URL`:
  `http://<lan-ip>:8000`.
- `SIP_GATEWAY_*`: leave as placeholders (e.g. realm/proxy `sip.invalid`)
  unless you're intentionally going live — see below.
- Passwords/secrets (`ESL_PASSWORD`, `POSTGRES_PASSWORD`, `REDIS_PASSWORD`,
  `MINIO_ROOT_PASSWORD`, `OSS_JWT_SECRET`): generate fresh random values
  (`openssl rand -hex 24`, etc.) — never reuse anything from a real
  deployment.
- `CLOUDFLARE_TUNNEL_TOKEN`: leave blank for an ephemeral quick tunnel (see
  "Reaching the UI/API" below).

## Stop commands

```bash
cd deploy/vps-ai-pbx
docker compose -f docker-compose.vps.yml down       # stop + remove containers, keep volumes
docker compose -f docker-compose.vps.yml down -v    # also wipe Postgres/MinIO/FreeSWITCH-config volumes
```

Plain `down` is what you want between test sessions — your Postgres data,
MinIO buckets, and rendered FreeSWITCH config survive. `down -v` gives you a
truly clean slate (e.g. testing `install.sh`'s first-boot path again).

## Reaching the UI/API

Open **`http://<lan-ip>:3010`** in a browser (e.g. `http://192.168.1.179:3010`)
— that's the Dograh UI.

This works because of `docker-compose.local.override.yml` (see the start
command above), which publishes `ui` (3010) and `api` (8000) to the host.
That's a local-only file, deliberately kept separate from
`docker-compose.vps.yml` itself, for two reasons:

- **The browser needs both, directly.** `ui/src/lib/apiClient.ts` has the
  browser call the API at its own address directly — not proxied through
  the Next.js server, and not tunnel-aware — so both `3010` and `8000` need
  to be reachable from wherever you're browsing from, not just `3010`.
- **Publishing ports this way must never happen on a real VPS.** Docker's
  `-p`/`ports:` publishing inserts `iptables` rules that bypass `ufw`
  entirely (a well-known Docker/ufw interaction) — if `docker-compose.vps.yml`
  itself published these ports, `VPS_FIREWALL.md`'s firewall rules would be
  silently defeated the moment the stack started, regardless of what `ufw`
  says. Never add `-f docker-compose.local.override.yml` when deploying to
  an actual VPS.

The `cloudflared` service's default ephemeral quick tunnel
(`CLOUDFLARE_TUNNEL_TOKEN` left blank) forwards only to `api:8000`, **not**
the UI — it exists to validate/exercise the API from outside your LAN (e.g.
for webhook-style testing), not as the primary way to browse the UI locally.
If you want to see its URL anyway:

```bash
docker compose -f docker-compose.vps.yml logs cloudflared | grep trycloudflare
```

## FreeSWITCH validation

```bash
ESL_PW=$(grep '^ESL_PASSWORD=' .env | cut -d= -f2)
docker compose -f docker-compose.vps.yml exec freeswitch fs_cli -p "$ESL_PW" -x "status"
docker compose -f docker-compose.vps.yml exec freeswitch fs_cli -p "$ESL_PW" -x "module_exists mod_audio_stream"
docker compose -f docker-compose.vps.yml exec freeswitch fs_cli -p "$ESL_PW" -x "sofia status"
```

With placeholder `SIP_GATEWAY_*` credentials, `sofia status` showing the
`external` profile as `RUNNING` and the gateway as `FAIL_WAIT`/`TRYING` (not
`REGED`) is **expected and correct** — there's no real trunk to register
with yet. See `TROUBLESHOOTING.md`'s "Placeholder-credential registration
failure" section.

## Troubleshooting

Start with `TROUBLESHOOTING.md` — everything there applies unchanged. A few
things specific to this local/Windows setup:

- **`freeswitch-manager` exits immediately with `stat
  ./scripts/run_freeswitch_manager.sh: no such file or directory`**: the
  published `dograhai/dograh-api:latest` tag on Docker Hub was out of sync
  with this repo's `scripts/` directory when this guide was written. This
  `.env`/compose setup works around it by building `api`/`ui`/
  `freeswitch-manager` from your local checkout instead of pulling — see
  below. If you hit this again after `git pull`, run `docker compose -f
  docker-compose.vps.yml up -d --build` to force a fresh local build.
- **`pipecat does not appear to be a Python project`**: the `pipecat`
  submodule isn't checked out — see the prerequisite above.
- **Port 5060 already in use**: another SIP stack (a softphone, an existing
  `docker-compose.freeswitch.yml` setup, Windows' own services) may already
  bind it. `netstat -ano | findstr :5060` (PowerShell) to find the culprit.
- **Other LAN devices (e.g. a real SIP phone) can't reach this PC**: Windows
  Firewall may be blocking inbound connections to Docker Desktop's exposed
  ports. Add inbound allow rules for UDP/TCP 5060 and UDP 20000–20199, or
  temporarily test with Windows Firewall's "Private network" prompt allowed
  when Docker Desktop first asks.
- **LAN IP changed after a reboot/reconnect**: update `SIP_EXTERNAL_IP`,
  `RTP_PUBLIC_IP`, `PUBLIC_HOST`, `PUBLIC_BASE_URL`, `AI_BACKEND_URL` in
  `.env`, then `docker compose -f docker-compose.vps.yml -f
  docker-compose.local.override.yml up -d` (no `--build` needed — only
  config re-renders). Always pass both `-f` files together for `up` — if you
  run `up` with only `docker-compose.vps.yml`, Compose will recreate `api`/
  `ui` *without* the override's published ports.

## Why `api`/`ui`/`freeswitch-manager` build from source here

`docker-compose.vps.yml`'s `api`, `ui`, and `freeswitch-manager` services
carry both an `image:` tag (pointing at `dograhai/dograh-api:latest` /
`dograh-ui:latest`) and a `build:` block pointing at this checkout's
`api/Dockerfile` / `ui/Dockerfile`. This is a real fix, not a local-only
hack: the published `:latest` tag was found to be missing
`scripts/run_freeswitch_manager.sh` (added to this repo more recently than
the last image publish), which broke `freeswitch-manager` identically on a
theoretical fresh VPS install too, since `install.sh` also just pulls that
tag. Building from the already-checked-out source (which `install.sh`
guarantees exists on a VPS as well, since it clones the full repo) removes
the dependency on the published image staying in sync with the repo — see
the `build:` comments in `docker-compose.vps.yml` for the full rationale.
`docker compose up -d --build` always rebuilds these three from whatever
commit is currently checked out.

## Going live locally (optional, not required for staging validation)

If you want this PC to actually take real calls in place of the Pi (not
just validate the infrastructure):

1. Confirm the Raspberry Pi is powered off first — the same CheapConnect
   trunk credentials can't usefully register from two places at once.
2. Replace the placeholder `SIP_GATEWAY_*` values in `.env` with the real
   trunk credentials (username, password, realm `voip.cheapconnect.net`,
   proxy) — see `RASPBERRY_FREESWITCH_BACKUP.md` for the non-secret details
   of the existing trunk config.
3. `docker compose -f docker-compose.vps.yml -f docker-compose.local.override.yml
   up -d` (no `--build` needed for a `.env`-only change) and re-check
   `sofia status gateway <SIP_GATEWAY_NAME>` for `REGED`/`UP`.
4. In the Dograh UI (`http://<lan-ip>:3010`), add a
   FreeSWITCH telephony configuration (ESL host `freeswitch`, port `8021`,
   the `ESL_PASSWORD` from `.env`) and bind your DID to a workflow — see
   `VPS_DEPLOYMENT_GUIDE.md`'s "Configuring Dograh to use this FreeSWITCH".

## Known differences: this local PC deployment vs. a future VPS deployment

| Aspect | Local PC (this guide) | VPS (`VPS_DEPLOYMENT_GUIDE.md`) |
| --- | --- | --- |
| `SIP_EXTERNAL_IP`/`RTP_PUBLIC_IP` | LAN IP (e.g. `192.168.1.179`) | Real public IPv4 |
| NAT/reachability | Relies on outbound-registration NAT pinhole, same as the source Pi — no inbound port-forwarding | Direct inbound — no NAT involved |
| Host firewall | None applied by this guide (Windows Firewall prompts are ad hoc) | `VPS_FIREWALL.md`'s explicit `ufw`/`iptables` rules required |
| `api`/`ui`/`freeswitch-manager` images | Built from local source (`build:` in `docker-compose.vps.yml`, this checkout's exact commit) | `install.sh` pulls `dograhai/dograh-api:latest`/`dograh-ui:latest` — will hit the same build fallback if that tag is ever stale again |
| `api`/`ui` reachability | Directly on the LAN via published host ports (`docker-compose.local.override.yml`) | Via `PUBLIC_BASE_URL`/`AI_BACKEND_URL` pointing at real public DNS names, fronted by a properly-configured named Cloudflare tunnel (multiple ingress rules) or reverse proxy — **never** raw published ports, which would bypass `ufw` (see "Reaching the UI/API") |
| SIP trunk | Placeholder (`sip.invalid`) by default; real creds optional or a step 2 exercise | Real trunk required from the start |
| Cloudflare tunnel | Ephemeral quick tunnel (`CLOUDFLARE_TUNNEL_TOKEN` blank), URL changes every restart | Named tunnel with a stable token/hostname recommended for anything long-lived |
| Secrets | Freshly generated, local-only, low-stakes | Same generation method, but these ARE the production secrets — treat accordingly |
| Disk/compute | Whatever the dev PC has | Sized per `VPS_ARCHITECTURE.md`'s capacity planning |
| Uptime expectations | Runs only while this PC/Docker Desktop is on | `restart: unless-stopped` + systemd/Docker's own restart policy, expected to run continuously |

## Migrating this exact deployment to a VPS later

Because both environments run identically from `docker-compose.vps.yml`, the
migration is mostly a `.env` diff, not a re-architecture:

1. Provision the VPS, install Docker (or run `install.sh`, which does this).
2. Clone this repo there (`git clone --recurse-submodules ...` — don't
   forget the submodule) and copy the *shape* of your local `.env`, but:
   - Replace `SIP_EXTERNAL_IP`/`RTP_PUBLIC_IP`/`PUBLIC_HOST` with the VPS's
     real public IP/hostname.
   - Replace the placeholder `SIP_GATEWAY_*` with the real trunk (if not
     already done locally).
   - Generate **new** secrets — do not carry over the local ones.
   - Set a real `CLOUDFLARE_TUNNEL_TOKEN` (named tunnel) instead of leaving
     it blank.
3. Apply `VPS_FIREWALL.md`'s firewall rules.
4. `docker compose -f docker-compose.vps.yml up -d --build` and work through
   `VPS_DEPLOYMENT_GUIDE.md`'s verification steps.
5. If you want a full production cutover from the Pi (not just this
   PC-as-staging exercise), see `MIGRATION_PLAN.md`'s sequencing.
