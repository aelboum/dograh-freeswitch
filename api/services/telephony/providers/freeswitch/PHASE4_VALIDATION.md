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

## Remaining steps before real call testing

1. **Confirm the exact `Caller-Destination-Number` format FreeSWITCH delivers for
   `723690372`.** The dialplan regex allows an optional `31` prefix
   (`^(31)?723690372$`); if the carrier actually delivers `31723690372`, it will
   normalize to `+31723690372` and **not** match the stored `+723690372` — this can
   only be confirmed by an actual inbound call or by inspecting a live `CHANNEL_PARK`
   event's `Caller-Destination-Number` field for this DID.
2. **Pin an actual `mod_audio_stream` version-behavior check under a real call** —
   the wire format was verified against source, but a real call is the first time
   audio actually flows through `FreeswitchFrameSerializer` end-to-end.
3. **Get a real destination number** for an outbound test call (a placeholder was
   used above only to show command construction).
4. **Place one real outbound call**, then **one real inbound call to 723690372**,
   watching `docker logs dograh-freeswitch-manager` and the workflow run's transcript,
   once you're ready — this report stops short of that per the instruction not to
   place real calls yet.
