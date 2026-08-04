# Raspberry Pi FreeSWITCH — Backup / Audit Record

Read-only audit of the live Pi (`192.168.1.60`), collected via SSH
(`pi`/Raspbian default creds) on 2026-08-04. **Nothing on the Pi was
modified to produce this document.** This is the source-of-truth snapshot
the VPS container build (`deploy/vps-ai-pbx/freeswitch/`) is derived from —
see [`ARCHITECTURE.md`](./ARCHITECTURE.md) and
[`VPS_ARCHITECTURE.md`](./VPS_ARCHITECTURE.md) for how each fact below maps
into the new deployment.

## System

| | |
| --- | --- |
| Hostname | `freeswitch` |
| OS | Debian GNU/Linux 12 (bookworm), `aarch64` |
| FreeSWITCH | `1.10.12-release` (git `a88d069`, built 2024-08-02) |
| Install method | Plain source build at `/usr/local/freeswitch` (source tree `/usr/src/freeswitch`) — **not** FusionPBX, no `/etc/freeswitch` |
| Process management | systemd (`freeswitch.service`), `ExecStart=/usr/local/freeswitch/bin/freeswitch -ncwait -nonat` |
| Uptime at audit | up since 2026-08-02, stable (0 unexpected restarts observed) |

`spandsp` and `sofia-sip` are **not** installed from Debian's apt packages —
both are built from source (`/usr/src/spandsp-src`, `/usr/src/sofia-sip-src`,
both `github.com/freeswitch/*`, pinned commits). This is load-bearing:
Debian bookworm's own `libspandsp-dev` package is `0.0.6` (pre-3.0
versioning), while FreeSWITCH 1.10.12 requires `spandsp >= 3.0`. `sofia-sip`
isn't packaged for Debian at all. **Any from-source FreeSWITCH build on
Debian/bookworm must repeat this** — confirmed independently when
`aelboum/mod_audio_stream`'s own reproducibility CI hit the exact same
version trap building against apt's `libspandsp-dev`.

## Loaded modules (`conf/autoload_configs/modules.conf.xml`)

```
mod_console       mod_logfile       mod_xml_cdr       mod_event_socket
mod_json_cdr      mod_sofia         mod_commands      mod_curl
mod_db            mod_dptools       mod_expr          mod_fifo
mod_hash          mod_audio_stream  mod_pgsql          mod_dialplan_xml
mod_spandsp       mod_opus          mod_sndfile       mod_native_file
mod_local_stream  mod_tone_stream
```

`mod_xml_curl` is **present in the file but commented out**, with the
original comment: *"disabled: needs FSPBX real gateway-url in
xml_curl.conf.xml, placeholder only"*. This is not in active use today but
is the natural hook for the future Configuration Service documented in
`ARCHITECTURE.md` — noted here because it's a real, already-present
breadcrumb, not a new idea.

## SIP profiles (`conf/sip_profiles/`)

- **`external`** — the profile actually in use for the real trunk.
  `sip-port`, `rtp-ip`, `sip-ip`, `ext-rtp-ip`, `ext-sip-ip` are all set from
  `vars.xml` pre-process variables (`$${external_sip_ip}`,
  `$${external_rtp_ip}`, etc.) — **no IP is hardcoded in the profile file
  itself**, which is exactly the pattern the VPS deployment's `envsubst`
  templates preserve.
- **`internal`** — present, stock/unused.
- One gateway: `sip_profiles/external/cheapconnect.xml` —
  `realm`/`proxy` = `voip.cheapconnect.net`, `register=true`,
  `expire-seconds=1800`, `retry-seconds=30`, `caller-id-in-from=true`,
  `ping=25`. State at audit time: `REGED`/`UP`.
  **Credentials are not reproduced in this document.** They were collected
  live during this session directly into the new deployment's `.env` (never
  written to any committed file) — see `.env.example`'s
  `SIP_GATEWAY_*` vars. (Note: an unrelated redaction-script bug briefly
  printed this trunk's password in the planning chat transcript for this
  session; the user was notified directly and advised to rotate it when
  convenient — flagged here for the record, not because it belongs in this
  file.)

## Dialplan

`conf/dialplan/public/00_inbound_did.xml` routes the inbound DID straight to
`park()` — this is the exact prerequisite
`api/services/telephony/providers/freeswitch/OPERATOR_GUIDE.md` documents
for Dograh's ESL manager to take control of the call
(`esl_manager.py` observes `CHANNEL_PARK`). Copied verbatim into
`freeswitch/conf/dialplan/public/00_inbound_did.xml` in this deployment —
it's already generic (no hardcoded IP/DID pattern beyond the operator's own
DID match, which stays operator-supplied either way).

## `vars.xml` (relevant settings only)

- `domain` defaults to `$${local_ip_v4}` (no DNS name configured).
- `global_codec_prefs` / `outbound_codec_prefs`: `OPUS,G722,PCMU,PCMA,H264,VP8`.
- `bind_server_ip=auto`.
- `external_rtp_ip` / `external_sip_ip`: stock stun-based defaults
  (`stun:stun.freeswitch.org`) present in the file but not what's actually
  in effect — the box resolves its real address via `bind_server_ip=auto`
  on its LAN interface in practice. On the VPS, these become the real
  `SIP_EXTERNAL_IP`/`RTP_PUBLIC_IP` env vars (the VPS has a real public IP,
  so no STUN indirection is needed).

## Event Socket (ESL)

- `conf/autoload_configs/event_socket.conf.xml`: `listen-ip="::"`,
  `listen-port="8021"`, `apply-inbound-acl="dograh"`.
- `conf/autoload_configs/acl.conf.xml`'s `dograh` list: `default="deny"`,
  allowing only `127.0.0.1/32`, the Windows dev host's LAN IP, and its
  Docker bridge subnet (`172.18.0.0/16`) — a real allow-list pattern,
  carried forward into the VPS deployment with VPS-side CIDRs
  (`DOGRAH_ESL_ALLOWED_CIDRS`).
- ESL password lives only in `event_socket.conf.xml` on the box — never
  copied into this document or any other.

## RTP

`conf/autoload_configs/switch.conf.xml` has `rtp-start-port`/`rtp-end-port`
**commented out**, so FreeSWITCH uses its compiled-in default range
**16384–32768** — matches the task's stated range exactly; no override
needed on the VPS.

## `mod_audio_stream`

- Installed binary: `/usr/local/freeswitch/mod/mod_audio_stream.so`, built
  2026-08-01, from `github.com/aelboum/mod_audio_stream` (a fork of
  `amigniter/mod_audio_stream`, MIT) with two fixes on top of upstream:
  1. A CMake visibility fix (`C_VISIBILITY_PRESET default` /
     `CXX_VISIBILITY_PRESET default` / `VISIBILITY_INLINES_HIDDEN OFF`) —
     without it the module fails to load with
     `undefined symbol: mod_audio_stream_module_interface`.
  2. A `libwsc` submodule patch fixing the WebSocket handshake
     `Host:`/`Origin:` headers to omit the port on standard ports 443/80,
     which Cloudflare's tunnel requires exactly.
- **Built with `-DUSE_TLS=ON`** (confirmed via `readelf -d`: links
  `libssl.so.3`/`libcrypto.so.3`/`libevent_openssl-2.1.so.7` directly — a
  default build has none of those). Required for `wss://` through the
  Cloudflare tunnel; **the VPS Dockerfile must also pass `-DUSE_TLS=ON`**,
  or the resulting module silently can't do `wss://` despite building clean.
- Released, CI-built, reproducible source: `github.com/aelboum/mod_audio_stream`
  tag `v1.0.0-ai-pbx` (GitHub Actions build, ~5 minutes, x86_64 `.so` release
  asset attached) — this is what `deploy/vps-ai-pbx/freeswitch/Dockerfile`
  builds from, not a fresh rebuild of the community module.
- **Architecture note for the module itself, not addressed by this
  migration**: the module's write-callback still uses FreeSWITCH's
  `uuid_broadcast` API (via an external ESL listener reacting to the
  module's `mod_audio_stream::play` custom event) rather than direct
  media-bug audio injection, which carries an intrinsic ~100ms
  `lead-frames` delay hardcoded in FreeSWITCH core itself
  (`switch_ivr_async.c`) — not fixable by configuration, mitigated
  client-side by the existing `serializers.py` pacing/priming logic. Not in
  scope for this VPS migration; noted here only because it's true on the
  Pi today and remains true on the VPS with the same module.

## Network / firewall (audit finding, action item for the VPS)

- Interfaces: `eth0` (`192.168.1.60/24`), `wlan0` (`192.168.1.107/24`) — LAN
  only, **no public IP**.
- `iptables`: policy `ACCEPT` on all chains (no rules). `ufw`: `inactive`.
  **There is no host firewall on this box at all today.**
- This is safe *only* because the box has no public IP and reaches
  CheapConnect purely via outbound SIP registration (the trunk sends INVITEs
  back through the NAT pinhole the REGISTER keeps open) — there is no
  inbound port-forwarding configured on the home router either.
- **This does not carry forward to the VPS.** A VPS has a real, routable
  public IP, so the absence of a firewall here must not be treated as
  precedent — see [`VPS_FIREWALL.md`](./VPS_FIREWALL.md) for the real rules
  required once this is public-facing.

## SIP provider (CheapConnect) settings summary

| Setting | Value |
| --- | --- |
| Realm/Proxy | `voip.cheapconnect.net` |
| Registration | `register=true`, 1800s expiry, 30s retry |
| Ping | 25s keepalive |
| Public IP requirement | None today (NAT-traversal via registration). On the VPS, the trunk should be pointed at the VPS's real public IP — simpler and more standard than the Pi's current NAT-dependent setup |
