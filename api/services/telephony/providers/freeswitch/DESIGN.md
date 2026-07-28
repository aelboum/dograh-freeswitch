# FreeSWITCH Provider — Architecture & Design

## Why this exists

Dograh ships providers for Twilio, Plivo, Vonage, Vobiz, Cloudonix, Telnyx,
and Asterisk (ARI). This package adds `freeswitch` as a standalone
`TelephonyProvider` for operators who run their own FreeSWITCH box (bare,
containerized, or managed by FusionPBX) instead of a hosted telephony API.

Everything in this design follows the pre-existing extension contract in
`../AGENTS.md`: touch this folder, plus exactly two lines outside it
(`providers/__init__.py`'s import list, `api/schemas/telephony_config.py`'s
discriminated union). No other core file was edited to build this.

## Architecture choice: ESL, not SIP/RTP or REST/XML-RPC

FreeSWITCH's Event Socket Library (ESL) is a single TCP socket carrying both
commands (`api`/`bgapi`) and events — the closest FreeSWITCH-native analog to
Asterisk's ARI, which splits the same concerns across a REST API and a
separate WebSocket. Two alternatives were considered and rejected:

- **Raw SIP/RTP/WebRTC bridge**: would mean Dograh reimplementing
  FreeSWITCH's own job (a softswitch). Enormous scope, large ongoing
  maintenance surface, and it duplicates infrastructure the operator already
  runs.
- **FreeSWITCH REST/XML-RPC**: FreeSWITCH has no first-class, ARI-equivalent
  REST control plane. `mod_xml_rpc` is a weaker, less-used bolt-on with
  little ecosystem support compared to ESL.

**Inbound-mode ESL only.** Dograh connects *out* to FreeSWITCH's
`mod_event_socket` (the operator's box listens; Dograh is the client). ESL's
other mode — *outbound mode*, where FreeSWITCH's dialplan opens a connection
back to a listener Dograh would have to run per call via the `socket`
dialplan app — was rejected as more invasive for operators than inbound
mode, which needs only a `park()`/answer step in their dialplan (see
OPERATOR_GUIDE.md), not a bespoke `socket` app wired into every extension.

## Media: mod_audio_stream

Call audio is bridged via the community module
[`amigniter/mod_audio_stream`](https://github.com/amigniter/mod_audio_stream)
(pin **v1.0.3 or later** — earlier versions predate its raw-binary
bidirectional streaming support). This was verified against the module's
actual source (`audio_streamer_glue.cpp`), not just its README, since a
second, non-interchangeable module (`mod_audio_fork`, different ESL command
name, different wire framing) exists in this space and the two are easy to
conflate.

Confirmed wire protocol (asymmetric — this is a real property of the module,
not an inconsistency in `serializers.py`):

- **FreeSWITCH → Dograh**: raw binary L16 PCM frames, no envelope (like
  Asterisk's `chan_websocket`, unlike Twilio's base64-JSON). An optional
  single verbatim text frame (the `uuid_audio_stream start` command's
  `metadata` argument) may arrive once before audio begins — not JSON, sent
  as-is by the module.
- **Dograh → FreeSWITCH** (TTS playback): JSON text frames:
  ```json
  {"type": "streamAudio", "data": {"audioDataType": "raw", "sampleRate": 8000, "audioData": "<base64 L16 PCM>"}}
  ```
- The module's own connect/disconnect status is a FreeSWITCH-internal custom
  event (`mod_audio_stream::connect` etc.) — not sent over the wire to us.

Started via ESL: `uuid_audio_stream <uuid> start <wss-url> mono <rate> <metadata>`,
targeting Dograh's existing **generic** per-provider media route
(`WS /api/v1/telephony/ws/{workflow_id}/{organization_id}/{workflow_run_id}`)
— no new route was needed, unlike ARI which needs a dedicated `/ws/ari`
route because Asterisk's externalMedia channel requires pre-registered
`websocket_client.conf` entries rather than a literal URL argument.

## Call flow

**Inbound**: operator's dialplan routes Dograh-bound DIDs to `park()` while
ringing (an operator prerequisite, documented in OPERATOR_GUIDE.md — the
direct analog of ARI's Stasis-app dialplan requirement). `esl_manager.py`
observes `CHANNEL_PARK`, resolves the called number against
`telephony_phone_numbers` scoped to that connection's `telephony_configuration_id`,
creates the workflow run, explicitly answers the channel
(`uuid_answer`), then attaches `mod_audio_stream`.

**Outbound**: `FreeswitchProvider.initiate_call` issues
`bgapi originate {origination_uuid=...,workflow_run_id=...,workflow_id=...}<dial_prefix><number> &park()`.
Because `originate`'s destination application runs once the call is
answered, `CHANNEL_PARK` on our own stamped channel variables is the
"answered, attach media" trigger — no separate explicit answer step needed
for outbound (unlike inbound, where the dialplan parks while still ringing).

**Transfers**: FreeSWITCH has no Asterisk-style bridge object, so this
differs from ARI's bridge-swap. `transfer_call` originates a destination
leg (mirroring ARI's `transfer_call`); `esl_manager.py` correlates its
`CHANNEL_ANSWER` back to the `transfer_id` and publishes a
`DESTINATION_ANSWERED` event via the shared `call_transfer_manager`
(mirroring ARI's `_handle_destination_answered`); when the pipeline's
`EndFrame(reason=TRANSFER_CALL)` fires, `FreeswitchTransferStrategy` directly
`uuid_bridge`s the caller's channel to the now-answered destination — no
separate bridge/ext-media-channel juggling required.

**Teardown**: `CHANNEL_HANGUP`/`CHANNEL_HANGUP_COMPLETE` releases the
concurrency slot and clears the Redis channel→run mapping
(`fs:channel:{uuid}`, TTL-bound — ephemeral, process-local state, same
rationale as ARI's Redis usage).

## Components

| Concern | File |
| --- | --- |
| ESL wire protocol (auth, api/bgapi, event framing) | `esl_client.py` |
| `TelephonyProvider` implementation (originate, transfer, status parsing) | `provider.py` |
| Pydantic config schema | `config.py` |
| `ProviderSpec` registration + UI form fields | `__init__.py` |
| WebSocket transport factory | `transport.py` |
| mod_audio_stream frame codec | `serializers.py` |
| Hangup/transfer strategies (invoked from the serializer's EndFrame handling) | `strategies.py` |
| Standalone ESL event-listener process (mirrors `ari_manager.py`) | `esl_manager.py` |

`serializers.py` subclasses pipecat's **public** `FrameSerializer` base
directly rather than re-exporting a class added to the `pipecat` submodule
(unlike `ari/serializers.py`, which does live inside `pipecat`). This is a
deliberately stricter isolation choice: it keeps 100% of FreeSWITCH-specific
code inside this provider package instead of adding a file to pipecat's own
separate upstream repo.

## Files added (all new)

- `api/services/telephony/providers/freeswitch/` (this whole package)
- `scripts/run_freeswitch_manager.sh`
- `docker-compose.freeswitch.yml`
- `api/tests/telephony/freeswitch/`
- `api/tests/telephony/test_freeswitch_esl_manager.py`

## Files touched outside this folder

The two sanctioned `providers/AGENTS.md` edits:

- `api/services/telephony/providers/__init__.py` — one import line
- `api/schemas/telephony_config.py` — one discriminated-union entry, one
  response field

Plus one more, discovered at deployment time and not covered by that
contract (which predates any provider needing a standalone background
process): **`api/Dockerfile`'s entrypoint-scripts `COPY` list**, one line
adding `./scripts/run_freeswitch_manager.sh`. This isn't optional — Docker
images only contain files an explicit `COPY` names, and the pre-existing
`ari` provider's `run_ari_manager.sh` needed the exact same treatment when
*it* was added. Any standalone-process provider (one with its own
`esl_manager.py`/`ari_manager.py`-style background worker, not just
request/response HTTP handling) requires this fourth edit; providers with
no such process (every other one in this repo except `ari`) don't.

Nothing else. In particular: **no database migration.** `workflow_runs.mode`
was converted from a Postgres enum to `VARCHAR(64)` in
`api/alembic/versions/4d8e9b2a3c5f_drop_workflow_run_mode_enum.py`
specifically so new providers need no schema change — its own docstring:
*"new providers can be added purely in application code... only the database
column type changes."* Adding `FREESWITCH = "freeswitch"` to
`api/enums.py::WorkflowRunMode` (a Python-side constant, not a DB
constraint) was the entire change needed there.

## Known v1 constraints

- **One FreeSWITCH config per org for outbound/UI-initiated calls without an
  explicit config id.** `account_id_credential_field=""` mirrors ARI (no
  HTTP webhook, so no account-id webhook-matching concept applies), but
  unlike Asterisk, FreeSWITCH/FusionPBX operators more plausibly run
  multiple physical boxes per org. The *inbound* path already disambiguates
  correctly since `esl_manager.py` scopes phone-number lookup by
  `telephony_configuration_id` per connection — the gap is specifically
  outbound calls that don't specify which config to use.
- **`dial_prefix` (gateway/dialplan naming) is entirely operator-defined and
  cannot be validated at config-save time** — a wrong value only surfaces as
  an ESL `-ERR` on the first real call attempt. The save-time connectivity
  check (`preprocess_credentials_on_save` in `__init__.py`) only proves ESL
  reachability/auth, not that the dial prefix is real.
- **Transfer is a direct `uuid_bridge`, not a warm-transfer conference.**
  Sufficient for connecting the caller to an answered destination; a
  fancier warm-transfer UX (announce-then-bridge, hold music, etc.) can be
  layered on later without changing `transfer_call`'s public contract.
- **The hand-rolled ESL client (`esl_client.py`) is intentionally minimal** —
  only `auth`/`api`/`bgapi`/event-subscription, no full ESL feature set — so
  it can be swapped for a maintained library (e.g. Genesis) later without
  touching call-logic code or tests (both are written against the
  `ESLTransport` interface, not raw sockets, except `test_esl_client.py`
  itself which tests that exact boundary).

## Upstream-merge safety

See `OPERATOR_GUIDE.md` for the git workflow. In short: since this provider
only ever touches its own folder plus two single-line edits in shared files,
an `upstream/main` merge should only ever conflict on those two lines (and
only if upstream also touched the exact same lines, e.g. adding another
provider to the same union around the same position).
