# FreeSWITCH Provider — Phase 4 Configuration Report

Internal validation note, package-local like `DESIGN.md`/`OPERATOR_GUIDE.md` —
not end-user product documentation, not part of the Mintlify `docs/` tree.
Passwords are redacted below; everything else is the real state of this install
as of 2026-07-28.

## Configured values

**Telephony configuration** (`telephony_configurations.id = 1`, name "FusionPBX LAN",
provider `freeswitch`, `is_default_outbound = true`):

| Field | Value |
| --- | --- |
| host | `192.168.1.60` |
| port | `8021` |
| esl_password | *(redacted — stored encrypted at rest, masked in the UI/API like every other provider's secrets)* |
| domain | `192.168.1.60` |
| dial_prefix | `sofia/gateway/a6cab9e7-0f14-4595-af57-2e638fa5de0f/` |
| audio_bridge_module | `mod_audio_stream` |

**Phone number binding** (`telephony_phone_numbers.id = 1`):

| Field | Value |
| --- | --- |
| address | `+723690372` |
| address_normalized | `+723690372` |
| inbound_workflow_id | `1` ("FreeSWITCH Phase 4 Test") |

**Test workflow** (`workflows.id = 1`, "FreeSWITCH Phase 4 Test"): Start Call → AI
Agent → End Call, simple greeting, no tools, no external integrations. Created and
published as v1 directly via `POST /api/v1/workflow/create/definition` (already in
`version_status: "published"` — real inbound calls run whatever's published, not a
draft; see the linked memory on that distinction).

All of the above was created through the normal Dograh API (`/organizations/telephony-configs`,
`/organizations/telephony-configs/1/phone-numbers`, `/workflow/create/definition`,
`/workflow/1/publish`) — no direct database writes.

## ESL connection status

**Live and stable.** `dograh-freeswitch-manager` connected, authenticated, and
subscribed to events against the real box; confirmed stable for 30+ seconds with
zero reconnects after fixing a real bug found during this validation (below).

```
FreeSWITCH ESL Manager starting...
[FreeSWITCH Manager] New config 1 for org 1: 192.168.1.60
[FreeSWITCH org=1] Started connection to 192.168.1.60:8021
[FreeSWITCH Manager] Active connections: 1
[FreeSWITCH org=1] Connecting to 192.168.1.60:8021...
[FreeSWITCH org=1] ESL connected to 192.168.1.60:8021
```

Separately, the config-save flow's live ESL auth check
(`preprocess_credentials_on_save` in `providers/freeswitch/__init__.py`) passed
against the real box during the actual save — a second, independent confirmation
the connection details are correct.

**Bug found and fixed during this validation**: `ESLTransport._read_frame()` applied
the same short command-reply timeout (8s default) to the long-lived event-listening
loop. With no live calls in progress, FreeSWITCH sends no events, so the connection
spuriously "timed out" and reconnected every ~8-9 seconds indefinitely — connect,
auth, subscribe, then a bogus `Connection error: .` (empty `TimeoutError`) on repeat.
Fixed by making `events()` wait indefinitely (`timeout=None`) rather than reusing the
command-reply timeout for the next-event read. Covered by a new regression test
(`test_events_does_not_time_out_during_idle_period`); all 26 provider unit tests still
pass.

## Gateway

| Field | Value |
| --- | --- |
| Friendly name (FusionPBX DB/UI) | `cheapconnect` |
| **Real dial-string identifier** | `a6cab9e7-0f14-4595-af57-2e638fa5de0f` |
| Profile | `external` |
| Trunk | CheapConnect (`voip.cheapconnect.net`) |
| Status | `REGED` / `UP` |
| Prior calls through it | 27 inbound, 2 outbound, 0 failed either direction |

The friendly name is **not** what `sofia/gateway/<name>/` needs — confirmed live via
`fs_cli -x "show gateways"`. Documented as an operator gotcha in `OPERATOR_GUIDE.md`.

## Manager status

`dograh-freeswitch-manager` container: running, 0 restarts, stable connection since
last recreate. Picked up telephony configuration 1 automatically via its 60s DB-poll
cycle (confirmed via the "New config 1 for org 1" log line appearing immediately on
startup, before the first poll interval even elapsed).

## Workflow status

Workflow 1 ("FreeSWITCH Phase 4 Test") exists, is published, and is bound to
`+723690372` as its inbound route. A synthetic inbound `CHANNEL_PARK` event was
replayed through the real `esl_manager.py`/`ESLConnection` code (no ESL commands
sent to the real box — `_answer`/`_attach_media` were swapped for logging stubs) and
correctly: acquired a concurrency slot, matched the called number to this phone
number/workflow, checked quota, created `workflow_runs.id = 1` (`state: initialized`,
`mode: freeswitch`, `call_type: inbound`), and built the exact `uuid_answer`/
`uuid_audio_stream` commands that would have been sent. The concurrency slot for that
synthetic run was released afterward; the `workflow_runs` row itself was left as
harmless residue (it never reached a real channel, so it will simply sit in
`initialized` state — safe to ignore or delete).

## Outbound preparation (generated, not executed)

Loaded the real provider instance from the real saved configuration
(`get_default_telephony_provider(organization_id=1)`) and called `initiate_call` with
`ESLTransport.bgapi` patched to capture the command instead of sending it — nothing
was transmitted to the real PBX. Generated command (destination is an illustrative
placeholder, not a real number to dial):

```
originate {origination_uuid='abf8f7a7-0118-428f-9748-00dd5b175382',workflow_run_id='1',workflow_id='1'}sofia/gateway/a6cab9e7-0f14-4595-af57-2e638fa5de0f/+15555550123 &park()
```

Verified: correct gateway UUID ✓, correct destination formatting (dial_prefix +
number) ✓, `workflow_id`/`workflow_run_id` attached as channel variables ✓, caller-ID
handling ✓ (no `origination_caller_id_number` var added since `from_number=None` was
passed — confirmed the conditional logic works).

## Inbound preparation (validated, not a real call)

```
Carrier -> FusionPBX -> park() -> FreeSWITCH CHANNEL_PARK event -> esl_manager.py
  -> phone-number/workflow lookup -> workflow_runs row created -> (uuid_answer,
     uuid_audio_stream start <ws-url>) -> AI pipeline handoff (on WS connect)
```

Every step through "would send uuid_answer/uuid_audio_stream" was exercised for real
against the real DB and real registered config (see Workflow status above) — only
the final two ESL commands and the actual phone call were not sent/placed.

## Phase 5 — real call results (2026-07-28, later same day)

Both a real outbound and a real inbound call were placed and completed successfully.

**Outbound** (`workflow_runs.id = 2`, to `+31643080655` via the real gateway):
originated → answered → `mod_audio_stream` connected
(`WebSocket /api/v1/telephony/ws/1/1/2 [accepted]`) → STT ("Connected to Flux") → TTS
("Connected to Dograh TTS service") → real two-way LLM conversation → natural hangup
→ recordings + transcript uploaded to MinIO → `state: completed`. Transcript excerpt:

```
assistant: Hello! I'm a test assistant, ready to help you out. How can I assist you today?
user: Yes.
assistant: Great! How can I help you today?
```

**Inbound** (`workflow_runs.id = 3`, real call to `723690372`): first attempt failed
— the caller heard "the number you have dialed cannot be reached." Root cause,
confirmed in `dograh-freeswitch-manager` logs, not a code bug:

```
[FreeSWITCH org=1] Inbound call to 31723690372 on channel ... — no matching phone number for config 1, hanging up
```

This resolved item 1 from the prior "remaining steps" list: the carrier delivers the
destination as `31723690372` (with the `31` prefix), not bare `723690372` as
originally assumed. The code correctly rejected an unrecognized number rather than
misrouting it — the fix was data, not logic. Deleted phone number `id=1`
(`+723690372`, address is immutable by design) and recreated it as `id=2` with
`address: "+31723690372"` via the normal API. Second real inbound call attempt
succeeded completely: matched the corrected binding → workflow run created → answered
→ media attached → full AI conversation → natural hangup → transcript uploaded →
`state: completed`. Transcript excerpt:

```
assistant: Hello! I'm a test assistant, ready to help you. How can I assist you today?
user: Yes. Hello. You speak with Iman?
assistant: Hello! I am an AI assistant, and I don't have a name like Iman. How can I help you today?
user: My Internet connection does not work.
assistant: I'm sorry to hear that you're having trouble with your internet connection. ...
```

**Conclusion: the FreeSWITCH provider works end-to-end against the real FusionPBX
install, both directions, with real audio and a real conversation.** No further
"remaining steps" from Phase 4 are outstanding. Two operationally-relevant notes for
future installs, both worth folding into `OPERATOR_GUIDE.md`:

- The exact `Caller-Destination-Number` format (with or without a country-code
  prefix) is carrier/dialplan-specific and must be confirmed with a real test call
  per install — don't assume the bare DID digits.
- `address` on a `telephony_phone_numbers` row is immutable; fixing a wrong binding
  means delete + recreate, not update.
