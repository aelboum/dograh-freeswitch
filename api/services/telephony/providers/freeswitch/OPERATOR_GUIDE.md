# FreeSWITCH Provider — Operator Guide

This is a runbook for operators connecting their own FreeSWITCH (or
FusionPBX-managed FreeSWITCH) box to Dograh. See `DESIGN.md` for the
architecture this implements.

## Prerequisites

- A FreeSWITCH instance (bare install, Docker container, or FusionPBX) with
  `mod_event_socket` and [`mod_audio_stream`](https://github.com/amigniter/mod_audio_stream)
  (v1.0.3+) loaded.
- Network reachability: Dograh's backend must be able to reach the
  FreeSWITCH box's ESL port (default `8021`) — outbound only, Dograh is
  always the ESL client. FreeSWITCH must be able to reach Dograh's backend
  over WebSocket for `mod_audio_stream` (this is resolved dynamically per
  call via `get_backend_endpoints()`, the same mechanism every other
  provider uses — no separate "public host" field to configure).

## 1. Dialplan: route Dograh-bound DIDs to `park()`

Dograh's ESL manager observes `CHANNEL_PARK` as the signal that a call is
under its control (this mirrors Asterisk's Stasis-app requirement for the
`ari` provider). Add a dialplan extension for each DID/extension you want
Dograh to answer:

```xml
<extension name="dograh-inbound">
  <condition field="destination_number" expression="^(YOUR_DID)$">
    <action application="park"/>
  </condition>
</extension>
```

In FusionPBX: create an inbound route pointing at an extension whose
dialplan action is `park` (not `bridge`/`transfer`) — do this via the
FusionPBX UI's Dialplan Manager, or ask your FusionPBX administrator.
Dograh answers the channel itself once it resolves which workflow the
called number maps to (via the phone number's `inbound_workflow_id` in
Dograh's UI).

**Confirmed gotcha, verified against a real install**: the destination number
Dograh must bind the phone number to in the UI is whatever FreeSWITCH's
`Caller-Destination-Number` actually contains for that DID — which may include
a country-code prefix your dialplan regex tolerates but your DID string
doesn't literally show. E.g. a dialplan matching `^(31)?723690372$` may still
deliver `31723690372` (with the prefix) on real calls, not bare `723690372`.
A phone number bound to the wrong form fails inbound routing silently from
the caller's perspective (they just hear a generic "cannot be reached"
message) — check `dograh-freeswitch-manager`'s logs for a
`no matching phone number` warning showing the actual delivered number, and
bind to that exact string. Note `address` on a phone number is immutable
once created (by design) — fixing a wrong binding means deleting and
recreating it, not editing it.

## 2. Outbound: configure `dial_prefix`

`dial_prefix` in the Dograh telephony configuration form is prepended
directly to the destination number in the ESL `originate` command, e.g.:

- Via a FusionPBX gateway: `sofia/gateway/<gateway-name>/`
- Direct internal dialing: `sofia/internal/`

**FusionPBX gotcha, verified against a real install**: the friendly gateway
name shown in FusionPBX's UI/DB (`v_gateways.gateway`, e.g. `cheapconnect`)
is **not** what `sofia/gateway/<name>/` needs — mod_sofia registers the
gateway internally under its `gateway_uuid` instead. Don't guess the dial
string from the DB or UI label; confirm the real one over ESL first:

```
fs_cli -x "show gateways"
```

Look at the `Profile::Gateway-Name` column (e.g.
`external::a6cab9e7-0f14-4595-af57-2e638fa5de0f`) — the part after `::` is
what actually goes in `dial_prefix`
(`sofia/gateway/a6cab9e7-0f14-4595-af57-2e638fa5de0f/`), not the friendlier
name FusionPBX's gateway list shows you.

This is entirely your FreeSWITCH/FusionPBX configuration — Dograh cannot
validate it exists. A wrong prefix surfaces as an ESL `-ERR` on the first
call attempt, not at config-save time (the save-time check only confirms
Dograh can reach and authenticate to your ESL port).

## 3. Configure the provider in Dograh

In the Dograh UI, add a telephony configuration with provider **FreeSWITCH**
and fill in:

| Field | Value |
| --- | --- |
| ESL Host | Your FreeSWITCH box's hostname/IP |
| ESL Port | `8021` (default) |
| ESL Password | Your `mod_event_socket` password (**do not use the default `ClueCon` in production** — set a strong password in `event_socket.conf.xml`) |
| Domain | Your FreeSWITCH SIP domain |
| Dial Prefix | See step 2 |
| From Extensions | Optional — extensions/DIDs available for outbound calls |

Saving the form attempts a live ESL auth handshake; a failure aborts the
save with the specific error (auth rejected vs. unreachable).

## 4. Deploy the ESL manager

The ESL manager is a separate, optional process — Dograh's default install
runs without it. Enable it with:

```bash
docker compose -f docker-compose.yaml -f docker-compose.freeswitch.yml \
  --profile freeswitch up -d
```

This starts exactly one additional container (`dograh-freeswitch-manager`)
that connects out to every configured FreeSWITCH box. It has no HTTP
surface and publishes no ports.

## 5. Security

- `mod_event_socket`'s ACL (`event_socket.conf.xml`'s `<param name="apply-inbound-acl" .../>` or a firewall rule) should restrict which hosts can reach port 8021 to Dograh's backend/manager only — ESL has no authentication beyond the shared password.
- Set a strong ESL password; it's stored encrypted-at-rest the same way every other provider's credentials are (JSONB `credentials` column, masked in the UI).

## Upstream-merge workflow

This provider was built to touch only its own folder plus two single-line
edits elsewhere (see `DESIGN.md`). Pulling in upstream Dograh changes:

```bash
git remote add upstream https://github.com/dograh-hq/dograh.git   # once
git fetch upstream
git checkout main
git merge upstream/main
```

Conflicts should only ever appear in the two shared files this provider
touches:

- `api/services/telephony/providers/__init__.py`
- `api/schemas/telephony_config.py`

— and only if upstream also modified the exact same lines (e.g. added
another new provider around the same position in the import list or
discriminated union). Resolve by keeping both providers' entries, then:

```bash
# run tests
docker run --rm -v "$(pwd):/workspace" -w /workspace/api <api-image> \
  python -m pytest tests/telephony/freeswitch/ tests/telephony/test_freeswitch_esl_manager.py -q
# or, with a local dev environment set up per api/AGENTS.md:
source venv/bin/activate && set -a && source api/.env.test && set +a
python -m pytest api/tests/telephony/freeswitch/ api/tests/telephony/test_freeswitch_esl_manager.py

# redeploy
docker compose -f docker-compose.yaml -f docker-compose.freeswitch.yml \
  --profile freeswitch up -d --build
```

No other file in this repository needs to change for an upstream merge to
succeed — everything specific to FreeSWITCH lives in this one folder.
