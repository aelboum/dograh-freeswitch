# Troubleshooting

## Testing checklist (Phase 10)

Once the stack is up (`VPS_DEPLOYMENT_GUIDE.md`) and the firewall is open
(`VPS_FIREWALL.md`), work through these in order — each depends on the
previous one working:

1. **SIP registration** — `docker compose exec freeswitch fs_cli -x "sofia status gateway <SIP_GATEWAY_NAME>"` shows `REGED`/`UP`. If not, see "SIP trunk won't register" below.
2. **Incoming call** — call your DID; you should hear it connect (silence,
   since Dograh hasn't answered with audio yet at this stage) rather than a
   rejection tone/fast-busy.
3. **FreeSWITCH answers** — `fs_cli -x "show channels"` shows an active
   channel the moment the call connects.
4. **`mod_audio_stream` connects** — check `freeswitch-manager` logs
   (`docker compose logs -f freeswitch-manager`) for a `CHANNEL_PARK`
   event followed by the ESL manager attaching media.
5. **Audio reaches Dograh** — check `api` logs for the WebSocket connection
   from FreeSWITCH's `mod_audio_stream`.
6. **AI responds** — you hear the configured workflow's AI agent speak.
7. **TTS audio returns** — full round-trip conversation works.
8. **Call termination** — hang up cleanly; `fs_cli -x "show channels"`
   shows no leftover channel, and `workflow_runs` in the dashboard shows the
   call as completed, not stuck in-progress.

**Measuring call-answered → first-AI-audio latency**: this repo already has
turn-latency instrumentation in `pipecat` (see recent commit history,
`533fab8c`/`1c4ff965`) — use that rather than adding new timing code.

## SIP trunk won't register

- `docker compose logs freeswitch | grep -i sofia` — look for auth failures
  (wrong username/password/realm) vs. connectivity failures (proxy
  unreachable).
- Confirm `5060/udp` and `5060/tcp` are actually open
  (`VPS_FIREWALL.md`'s "Verifying rules after applying" section) — a
  registration that times out silently is usually a firewall issue, not a
  credentials issue.
- Confirm `SIP_EXTERNAL_IP`/`RTP_PUBLIC_IP` in `.env` are the VPS's real
  public IP, not a private/Docker-internal one — `curl -4 ifconfig.me` from
  the VPS host to double check.

## No audio (call connects but silent both ways)

- Almost always an RTP port issue on a VPS specifically (this is the
  most common gotcha vs. the Pi, which had no port-publishing to get wrong).
  Confirm `RTP_PORT_RANGE_START`/`RTP_PORT_RANGE_END` in `.env` match
  **exactly** what's in `docker-compose.vps.yml`'s `ports:` entry for
  `freeswitch` and what's open in the firewall — all three must agree (see
  `VPS_ARCHITECTURE.md`'s "Why 200 ports" note). If you widened the range in
  one place and not the others, this is the symptom.
- `fs_cli -x "rtp_getcodec <uuid>"` / checking `sofia status profile
  external` for the negotiated codec — a codec mismatch (rare, since
  `global_codec_prefs` covers common ones) can also present as no-audio.

## ESL connection refused / ACL denial

- Remember: `8021` is **never** published on the host by design (see
  `VPS_FIREWALL.md`) — `freeswitch-manager` must reach it via
  `freeswitch:8021` over `ai-pbx-network`, never via the VPS's public IP.
  If you're trying to connect from outside the Docker network (e.g.
  debugging from the VPS host itself), use
  `docker compose exec freeswitch fs_cli` instead of a raw ESL client
  against a host port that doesn't exist.
- `acl.conf.xml`'s `dograh` list denies by default — if you added a new
  service that needs ESL access, add its subnet to
  `DOGRAH_ESL_ALLOWED_CIDRS` in `.env` and restart the `freeswitch`
  container (re-renders on start, see `freeswitch/docker-entrypoint.sh`).

## Config changes not taking effect at all (a real gotcha, already fixed here — noted in case you extend this image)

FreeSWITCH's actual, compiled-in default config directory for a
`./configure --prefix=/usr/local/freeswitch` build (no explicit
`--sysconfdir`) is **`/usr/local/freeswitch/etc/freeswitch/`** — the
standard autotools `${prefix}/etc/${package}` layout — **not**
`/usr/local/freeswitch/conf/`, even though that's the path this deployment
audited on the source Raspberry Pi (`RASPBERRY_FREESWITCH_BACKUP.md`) and a
very common assumption from other FreeSWITCH guides. `make install` puts
the complete stock/vanilla config tree (root `freeswitch.xml`, every other
module's `autoload_configs/*.xml`, `dialplan/default.xml`, `directory/`,
etc.) at the `etc/freeswitch` path; a same-named `conf/` directory is simply
never read, no matter how complete or correct its contents are.

This deployment already renders bootstrap config into the *real* directory
(`docker-entrypoint.sh`'s `CONF_DIR`, and `docker-compose.vps.yml`'s
`freeswitch-config` volume mount both point at
`/usr/local/freeswitch/etc/freeswitch`) — confirmed via `strace` and a
source-level debug patch during this deployment's own verification pass,
after config changes silently had no effect for a while. If you ever see
FreeSWITCH behaving as if a config change (or even a `.env` value) simply
isn't there, and everything else checks out, this directory mismatch is the
first thing to re-verify — `docker compose exec freeswitch fs_cli -x
"global_getvar base_dir"` and confirm files actually live under
`$base_dir/etc/freeswitch`, not `$base_dir/conf`.

## `mod_audio_stream` / TLS handshake failures (wss://)

- Confirm the module actually loaded:
  `fs_cli -x "module_exists mod_audio_stream"` → `true`.
- If it loaded but `wss://` connections fail specifically: this exact
  failure mode was hit and fixed during the source Pi's own setup (see
  `RASPBERRY_FREESWITCH_BACKUP.md`'s `mod_audio_stream` section) — it was a
  missing `-DUSE_TLS=ON` at build time. This Dockerfile always passes that
  flag (`freeswitch/Dockerfile`'s `mod-audio-stream-builder` stage), so this
  specific failure shouldn't recur from a fresh build of this image — if it
  does, verify with `docker compose exec freeswitch sh -c "ldd
  /usr/local/freeswitch/mod/mod_audio_stream.so | grep ssl"` that the
  module actually links `libssl`; if it doesn't, the image wasn't rebuilt
  after a Dockerfile change (`docker compose up -d --build`, not just `up
  -d`).
- If Cloudflare tunnel WSS specifically fails: the `aelboum/libwsc` fork's
  `Host:`/`Origin:` header fix (also documented in
  `RASPBERRY_FREESWITCH_BACKUP.md`) is what makes this work through
  Cloudflare's edge — confirmed already fixed in the `v1.0.0-ai-pbx` tag
  this image builds from.

## `freeswitch` container unhealthy / FreeSWITCH won't start

- `docker compose logs freeswitch` — a missing required bootstrap env var
  fails fast with a clear message from `docker-entrypoint.sh` (lists exactly
  which var is missing) before FreeSWITCH even starts.
- If it's a module load failure specifically: the **live Pi setup hit a
  real, dangerous failure mode once** — loading a broken module build
  crashed the whole FreeSWITCH process, and retrying `load` in the same
  running process didn't recover (a full process restart was needed — see
  `RASPBERRY_FREESWITCH_BACKUP.md`'s `mod_audio_stream` section for the full
  story). In a container, `docker compose restart freeswitch` gives you that
  full-process restart for free — don't try to `fs_cli -x "load ..."` twice
  in the same running container if the first attempt failed.

## Placeholder-credential registration failure (expected, during local verification)

If you're following this deployment's own local verification steps
(building the image and bringing the stack up with `.env.example`-derived
placeholder SIP credentials, no real trunk), **gateway registration failing
is expected and correct** — there's no real trunk to register with. What
matters at that stage is that `mod_audio_stream` loaded and the `sofia`
profile bound successfully, not that registration succeeded. Don't treat
this as a bug to chase; it resolves itself once real trunk credentials are
in `.env`.
