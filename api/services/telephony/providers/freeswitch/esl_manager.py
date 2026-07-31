"""FreeSWITCH ESL Event Listener Manager.

Standalone process (mirrors api/services/telephony/ari_manager.py) that:
1. Queries the database for all `freeswitch` telephony configurations
2. Maintains one persistent, reconnecting inbound-mode ESL connection per
   configured FreeSWITCH box
3. Handles reconnection logic with exponential backoff
4. Processes CHANNEL_PARK/CHANNEL_ANSWER/CHANNEL_HANGUP_COMPLETE events to
   drive inbound call creation, outbound/transfer correlation, and teardown
5. Periodically refreshes configuration to detect new/removed configs

FreeSWITCH has no Asterisk-style bridge object, so this is simpler than
ari_manager.py in one respect: there's no separate external-media channel to
create and bridge — mod_audio_stream attaches directly to the caller's own
channel as a media bug. See DESIGN.md for the full architecture and the
CHANNEL_PARK-as-unified-"under our control" reasoning.
"""

from api.logging_config import setup_logging

setup_logging()
import asyncio
from typing import Dict, Optional, Set

import redis.asyncio as aioredis
from loguru import logger

from api.constants import REDIS_URL
from api.db import db_client
from api.enums import CallType, WorkflowRunMode
from api.services.call_concurrency import (
    CallConcurrencyLimitError,
    call_concurrency,
)
from api.services.quota_service import authorize_workflow_run_start
from api.services.telephony.call_transfer_manager import get_call_transfer_manager
from api.services.telephony.providers.freeswitch.esl_client import (
    ESLEvent,
    ESLTransport,
)
from api.services.telephony.transfer_event_protocol import (
    TransferEvent,
    TransferEventType,
)
from api.services.workflow.run_creation import prepare_workflow_run_inputs

# Redis key pattern and TTL for channel-to-run mapping
_CHANNEL_KEY_PREFIX = "fs:channel:"
_CHANNEL_KEY_TTL = 3600  # 1 hour safety expiry

_TRACKED_EVENTS = (
    "CHANNEL_PARK",
    "CHANNEL_ANSWER",
    "CHANNEL_HANGUP",
    "CHANNEL_HANGUP_COMPLETE",
    "CUSTOM",
    "mod_audio_stream::play",
)

_AUDIO_STREAM_PLAY_SUBCLASS = "mod_audio_stream::play"


class ESLConnection:
    """Manages a single ESL connection for one FreeSWITCH telephony config."""

    def __init__(
        self,
        organization_id: int,
        telephony_configuration_id: int,
        host: str,
        port: int,
        esl_password: str,
        dial_prefix: str,
        audio_bridge_module: str = "mod_audio_stream",
    ):
        self.organization_id = organization_id
        self.telephony_configuration_id = telephony_configuration_id
        self.host = host
        self.port = port
        self.esl_password = esl_password
        self.dial_prefix = dial_prefix
        self.audio_bridge_module = audio_bridge_module

        self._transport: Optional[ESLTransport] = None
        self._playback_transport: Optional[ESLTransport] = None
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._reconnect_delay = 1
        self._max_reconnect_delay = 300

        self._redis_client: Optional[aioredis.Redis] = None
        self._call_transfer_manager = None

    async def _get_redis(self) -> aioredis.Redis:
        if not self._redis_client:
            self._redis_client = await aioredis.from_url(
                REDIS_URL, decode_responses=True
            )
        return self._redis_client

    async def _get_transfer_manager(self):
        if not self._call_transfer_manager:
            self._call_transfer_manager = await get_call_transfer_manager()
        return self._call_transfer_manager

    async def _set_channel_run(self, channel_id: str, workflow_run_id: str):
        r = await self._get_redis()
        await r.set(
            f"{_CHANNEL_KEY_PREFIX}{channel_id}", workflow_run_id, ex=_CHANNEL_KEY_TTL
        )

    async def _get_channel_run(self, channel_id: str) -> Optional[str]:
        r = await self._get_redis()
        return await r.get(f"{_CHANNEL_KEY_PREFIX}{channel_id}")

    async def _delete_channel_run(self, channel_id: str):
        r = await self._get_redis()
        await r.delete(f"{_CHANNEL_KEY_PREFIX}{channel_id}")

    @property
    def connection_key(self) -> str:
        """Unique key for this connection — one per FreeSWITCH config row."""
        return f"config:{self.telephony_configuration_id}"

    async def start(self):
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._connection_loop())
        logger.info(
            f"[FreeSWITCH org={self.organization_id}] Started connection to "
            f"{self.host}:{self.port}"
        )

    async def stop(self):
        self._running = False
        if self._transport:
            await self._transport.close()
        if self._playback_transport:
            await self._playback_transport.close()
            self._playback_transport = None
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info(
            f"[FreeSWITCH org={self.organization_id}] Stopped connection to "
            f"{self.host}:{self.port}"
        )

    async def _connection_loop(self):
        while self._running:
            try:
                await self._connect_and_listen()
            except asyncio.CancelledError:
                break
            except Exception as e:
                if not self._running:
                    break
                logger.warning(
                    f"[FreeSWITCH org={self.organization_id}] Connection error: {e}. "
                    f"Reconnecting in {self._reconnect_delay}s..."
                )
                await asyncio.sleep(self._reconnect_delay)
                self._reconnect_delay = min(
                    self._reconnect_delay * 2, self._max_reconnect_delay
                )

    async def _connect_and_listen(self):
        logger.info(
            f"[FreeSWITCH org={self.organization_id}] Connecting to "
            f"{self.host}:{self.port}..."
        )
        transport = ESLTransport(self.host, self.port, self.esl_password)
        await transport.connect()
        self._transport = transport
        self._reconnect_delay = 1
        logger.info(
            f"[FreeSWITCH org={self.organization_id}] ESL connected to "
            f"{self.host}:{self.port}"
        )

        await transport.subscribe(*_TRACKED_EVENTS)

        try:
            async for event in transport.events():
                if not self._running:
                    return
                await self._handle_event(event)
        finally:
            self._transport = None
            await transport.close()

    async def _handle_event(self, event: ESLEvent):
        event_name = event.get("Event-Name", "")
        channel_id = event.get("Unique-ID", "")

        logger.trace(
            f"[FreeSWITCH EVENT org={self.organization_id}] {event_name}: "
            f"channel={channel_id}"
        )

        if event_name == "CHANNEL_PARK":
            await self._handle_channel_park(channel_id, event)
        elif event_name == "CHANNEL_ANSWER":
            await self._handle_channel_answer(channel_id, event)
        elif event_name in ("CHANNEL_HANGUP", "CHANNEL_HANGUP_COMPLETE"):
            await self._handle_channel_hangup(channel_id, event)
        elif (
            event_name == "CUSTOM"
            and event.get("Event-Subclass") == _AUDIO_STREAM_PLAY_SUBCLASS
        ):
            await self._handle_audio_stream_play(channel_id, event)

    async def _handle_channel_park(self, channel_id: str, event: ESLEvent):
        """A channel just entered our control (park). This is the unified
        trigger for both a ringing inbound call and our own outbound/
        transfer-destination legs (which land in park once answered, since
        `originate ... &park()` runs its destination app on answer)."""
        transfer_id = event.get("variable_transfer_id")
        if transfer_id:
            # Transfer destination — handled via CHANNEL_ANSWER correlation,
            # nothing to attach here (it's about to be bridged away).
            return

        workflow_run_id = event.get("variable_workflow_run_id")
        workflow_id = event.get("variable_workflow_id")
        if workflow_run_id and workflow_id:
            await self._handle_outbound_parked(channel_id, workflow_run_id, workflow_id)
            return

        direction = event.get("Call-Direction", "")
        if direction == "inbound":
            await self._handle_inbound_park(channel_id, event)

    async def _handle_channel_answer(self, channel_id: str, event: ESLEvent):
        transfer_id = event.get("variable_transfer_id")
        if transfer_id:
            await self._handle_destination_answered(transfer_id, channel_id)

    async def _handle_channel_hangup(self, channel_id: str, event: ESLEvent):
        workflow_run_id = await self._get_channel_run(channel_id)
        if not workflow_run_id:
            return
        try:
            await call_concurrency.unregister_active_call(int(workflow_run_id))
        except Exception as e:
            logger.debug(
                f"[FreeSWITCH org={self.organization_id}] unregister_active_call "
                f"no-op or failed for run {workflow_run_id}: {e}"
            )
        await self._delete_channel_run(channel_id)
        logger.info(
            f"[FreeSWITCH org={self.organization_id}] Channel {channel_id} hung up "
            f"(run {workflow_run_id})"
        )

    async def _handle_inbound_park(self, channel_id: str, event: ESLEvent):
        """Handle a genuinely new inbound call parked while ringing.

        The operator's dialplan must route Dograh-bound DIDs to `park()`
        while ringing (documented in OPERATOR_GUIDE.md) — this mirrors ARI's
        Stasis-app dialplan prerequisite. We then explicitly answer the
        channel ourselves, same as ARI's `_answer_channel` step.
        """
        caller_number = event.get("Caller-Caller-ID-Number", "unknown")
        called_number = event.get("Caller-Destination-Number", "unknown")
        concurrency_slot = None
        workflow_run = None

        try:
            phone_row = await db_client.find_active_phone_number_for_inbound(
                self.organization_id, called_number, "freeswitch"
            )
            if (
                not phone_row
                or phone_row.telephony_configuration_id
                != self.telephony_configuration_id
            ):
                logger.warning(
                    f"[FreeSWITCH org={self.organization_id}] Inbound call to "
                    f"{called_number} on channel {channel_id} — no matching phone "
                    f"number for config {self.telephony_configuration_id}, hanging up"
                )
                await self._hangup(channel_id)
                return

            inbound_workflow_id = phone_row.inbound_workflow_id
            if not inbound_workflow_id:
                logger.warning(
                    f"[FreeSWITCH org={self.organization_id}] Phone number "
                    f"{phone_row.address} has no inbound_workflow_id — hanging up"
                )
                await self._hangup(channel_id)
                return

            workflow = await db_client.get_workflow(
                inbound_workflow_id, organization_id=self.organization_id
            )
            if not workflow:
                logger.warning(
                    f"[FreeSWITCH org={self.organization_id}] Workflow "
                    f"{inbound_workflow_id} not found — hanging up"
                )
                await self._hangup(channel_id)
                return

            user_id = workflow.user_id

            try:
                concurrency_slot = await call_concurrency.acquire_org_slot(
                    self.organization_id, source="freeswitch_inbound", timeout=0
                )
            except CallConcurrencyLimitError:
                logger.warning(
                    f"[FreeSWITCH org={self.organization_id}] Concurrent call limit "
                    f"reached; hanging up inbound channel {channel_id}"
                )
                await self._hangup(channel_id)
                return

            run_inputs = await prepare_workflow_run_inputs(db_client, workflow)
            workflow_run = await db_client.create_workflow_run(
                name=f"FreeSWITCH Inbound {caller_number}",
                workflow_id=inbound_workflow_id,
                mode=WorkflowRunMode.FREESWITCH.value,
                user_id=user_id,
                call_type=CallType.INBOUND,
                initial_context={
                    "caller_number": caller_number,
                    "called_number": called_number,
                    "direction": "inbound",
                    "provider": "freeswitch",
                    "telephony_configuration_id": self.telephony_configuration_id,
                },
                gathered_context={"call_id": channel_id},
                organization_id=self.organization_id,
                definition_id=run_inputs.definition_id,
            )
            await call_concurrency.bind_workflow_run(concurrency_slot, workflow_run.id)

            logger.info(
                f"[FreeSWITCH org={self.organization_id}] Created inbound workflow "
                f"run {workflow_run.id} for channel {channel_id} "
                f"(caller={caller_number}, called={called_number})"
            )

            quota_result = await authorize_workflow_run_start(
                workflow_id=inbound_workflow_id,
                organization_id=self.organization_id,
                workflow_run_id=workflow_run.id,
            )
            if not quota_result.has_quota:
                logger.warning(
                    f"[FreeSWITCH org={self.organization_id}] Quota exceeded for user "
                    f"{user_id} — hanging up inbound call {channel_id}"
                )
                await call_concurrency.release_workflow_run_slot(workflow_run.id)
                await self._hangup(channel_id)
                return

            await self._set_channel_run(channel_id, str(workflow_run.id))
            await self._answer(channel_id)
            await self._attach_media(channel_id, workflow_run.id, inbound_workflow_id)

        except Exception as e:
            if workflow_run:
                await call_concurrency.release_workflow_run_slot(workflow_run.id)
            elif concurrency_slot:
                await call_concurrency.release_slot(concurrency_slot)
            logger.error(
                f"[FreeSWITCH org={self.organization_id}] Error handling inbound "
                f"park for channel {channel_id}: {e}"
            )
            try:
                await self._hangup(channel_id)
            except Exception:
                pass

    async def _handle_outbound_parked(
        self, channel_id: str, workflow_run_id: str, workflow_id: str
    ):
        """Our own outbound-originated leg reached park (i.e. was answered —
        `originate`'s destination app runs on answer)."""
        try:
            await self._set_channel_run(channel_id, workflow_run_id)
            await db_client.update_workflow_run(
                run_id=int(workflow_run_id),
                gathered_context={"call_id": channel_id},
            )
            await self._attach_media(channel_id, int(workflow_run_id), int(workflow_id))
            logger.info(
                f"[FreeSWITCH org={self.organization_id}] Outbound call answered: "
                f"channel={channel_id}, run={workflow_run_id}"
            )
        except Exception as e:
            logger.error(
                f"[FreeSWITCH org={self.organization_id}] Error handling outbound "
                f"park for channel {channel_id}: {e}"
            )

    async def _attach_media(self, channel_id: str, workflow_run_id: int, workflow_id: int):
        """Start mod_audio_stream on this channel, pointed at Dograh's
        generic per-provider media WebSocket route."""
        from api.utils.common import get_backend_endpoints

        _, wss_backend_endpoint = await get_backend_endpoints()
        ws_url = (
            f"{wss_backend_endpoint}/api/v1/telephony/ws/"
            f"{workflow_id}/{self.organization_id}/{workflow_run_id}"
        )

        transport = await self._connect_control()
        try:
            command = (
                f"uuid_audio_stream {channel_id} start {ws_url} mono 8000 "
                f"dograh-run-{workflow_run_id}"
            )
            reply = await transport.api(command)
            if not reply.ok:
                logger.error(
                    f"[FreeSWITCH org={self.organization_id}] Failed to start "
                    f"{self.audio_bridge_module} on {channel_id}: {reply.error_text}"
                )
        finally:
            await transport.close()

    async def _connect_control(self) -> ESLTransport:
        """Open a short-lived ESL connection for one-off control commands,
        separate from the long-lived event-subscription connection."""
        transport = ESLTransport(self.host, self.port, self.esl_password)
        await transport.connect()
        return transport

    async def _get_playback_transport(self) -> ESLTransport:
        """Persistent connection for `uuid_broadcast` playback commands.

        Unlike `_connect_control` (fine for one-off answer/hangup calls),
        TTS chunks arrive roughly every 40ms — reconnecting per chunk would
        add a full connect+auth round trip to every playback command, badly
        degrading (or outright breaking) real-time audio.
        """
        if self._playback_transport is None or not self._playback_transport.connected:
            self._playback_transport = ESLTransport(
                self.host, self.port, self.esl_password
            )
            await self._playback_transport.connect()
        return self._playback_transport

    async def _handle_audio_stream_play(self, channel_id: str, event: ESLEvent):
        """mod_audio_stream (community edition) doesn't play TTS audio back
        onto the channel itself — it writes each decoded chunk to a temp
        file and fires this CUSTOM event carrying the file path, expecting
        an external listener to act on it. We're that listener: turn the
        notification into actual audio via `uuid_broadcast`.
        """
        raw_body = event.get("_body")
        if not raw_body:
            return

        import json

        try:
            payload = json.loads(raw_body)
        except (json.JSONDecodeError, TypeError):
            logger.warning(
                f"[FreeSWITCH org={self.organization_id}] Unparseable "
                f"{_AUDIO_STREAM_PLAY_SUBCLASS} body for channel {channel_id}: "
                f"{raw_body[:200]}"
            )
            return

        file_path = payload.get("file")
        if not file_path:
            return

        try:
            transport = await self._get_playback_transport()
            reply = await transport.api(f"uuid_broadcast {channel_id} {file_path} aleg")
            if not reply.ok:
                logger.warning(
                    f"[FreeSWITCH org={self.organization_id}] uuid_broadcast failed "
                    f"for channel {channel_id}, file {file_path}: {reply.error_text}"
                )
        except Exception as e:
            logger.warning(
                f"[FreeSWITCH org={self.organization_id}] Playback transport error "
                f"for channel {channel_id}: {e}"
            )
            self._playback_transport = None

    async def _answer(self, channel_id: str):
        transport = await self._connect_control()
        try:
            await transport.api(f"uuid_answer {channel_id}")
        finally:
            await transport.close()

    async def _hangup(self, channel_id: str):
        transport = await self._connect_control()
        try:
            await transport.api(f"uuid_kill {channel_id}")
        finally:
            await transport.close()

    async def _handle_destination_answered(self, transfer_id: str, channel_id: str):
        """Transfer destination channel answered — publish success event.

        Mirrors ari_manager.py's `_handle_destination_answered`. The actual
        bridge happens in FreeswitchTransferStrategy once the pipeline's
        EndFrame(reason=TRANSFER_CALL) fires for the caller leg.
        """
        try:
            transfer_manager = await self._get_transfer_manager()
            await transfer_manager.store_transfer_channel_mapping(channel_id, transfer_id)
            context = await transfer_manager.get_transfer_context(transfer_id)
            if not context:
                logger.error(
                    f"[FreeSWITCH Transfer org={self.organization_id}] No transfer "
                    f"context found for {transfer_id}"
                )
                return

            logger.info(
                f"[FreeSWITCH Transfer org={self.organization_id}] Destination "
                f"{channel_id} answered for transfer {transfer_id}"
            )

            success_event = TransferEvent(
                type=TransferEventType.DESTINATION_ANSWERED,
                transfer_id=transfer_id,
                original_call_sid=context.original_call_sid,
                transfer_call_sid=channel_id,
                conference_name=context.conference_name,
                message="Transfer destination answered",
                status="success",
                action="destination_answered",
            )
            await transfer_manager.publish_transfer_event(success_event)
        except Exception as e:
            logger.error(
                f"[FreeSWITCH Transfer org={self.organization_id}] Error handling "
                f"transfer answer: {e}"
            )


class ESLManager:
    """Manages ESL connections for all FreeSWITCH telephony configurations."""

    def __init__(self):
        self._connections: Dict[str, ESLConnection] = {}
        self._running = False
        self._config_refresh_interval = 60

    async def start(self):
        self._running = True
        logger.info("FreeSWITCH ESL Manager starting...")

        await self._refresh_connections()

        while self._running:
            await asyncio.sleep(self._config_refresh_interval)
            if self._running:
                await self._refresh_connections()

    async def stop(self):
        self._running = False
        logger.info("FreeSWITCH ESL Manager stopping...")
        for conn in self._connections.values():
            await conn.stop()
        self._connections.clear()
        logger.info("FreeSWITCH ESL Manager stopped")

    async def _refresh_connections(self):
        try:
            active_configs = await self._load_configs()
        except Exception as e:
            logger.error(f"Failed to load FreeSWITCH configurations: {e}")
            return

        active_keys: Set[str] = set()

        for config in active_configs:
            conn = ESLConnection(
                config["organization_id"],
                config["telephony_configuration_id"],
                config["host"],
                config["port"],
                config["esl_password"],
                config["dial_prefix"],
                config["audio_bridge_module"],
            )
            key = conn.connection_key
            active_keys.add(key)

            if key not in self._connections:
                logger.info(
                    f"[FreeSWITCH Manager] New config "
                    f"{config['telephony_configuration_id']} for org "
                    f"{config['organization_id']}: {config['host']}"
                )
                self._connections[key] = conn
                await conn.start()
            else:
                existing = self._connections[key]
                if (
                    existing.host != conn.host
                    or existing.port != conn.port
                    or existing.esl_password != conn.esl_password
                    or existing.dial_prefix != conn.dial_prefix
                ):
                    logger.info(
                        f"[FreeSWITCH Manager] Config "
                        f"{config['telephony_configuration_id']} changed, "
                        f"reconnecting..."
                    )
                    await existing.stop()
                    self._connections[key] = conn
                    await conn.start()

        removed_keys = set(self._connections.keys()) - active_keys
        for key in removed_keys:
            conn = self._connections.pop(key)
            logger.info(
                f"[FreeSWITCH Manager] Removing connection for org "
                f"{conn.organization_id}"
            )
            await conn.stop()

        if active_configs:
            logger.info(
                f"[FreeSWITCH Manager] Active connections: {len(self._connections)}"
            )
        else:
            logger.debug("[FreeSWITCH Manager] No FreeSWITCH configurations found")

    async def _load_configs(self) -> list:
        rows = await db_client.list_all_telephony_configurations_by_provider(
            "freeswitch"
        )

        configs = []
        for row in rows:
            credentials = row.credentials or {}
            host = credentials.get("host")
            port = credentials.get("port", 8021)
            esl_password = credentials.get("esl_password")
            dial_prefix = credentials.get("dial_prefix", "")
            audio_bridge_module = credentials.get(
                "audio_bridge_module", "mod_audio_stream"
            )

            if not all([host, esl_password]):
                logger.warning(
                    f"[FreeSWITCH Manager] Incomplete config {row.id} for org "
                    f"{row.organization_id}, skipping"
                )
                continue

            configs.append(
                {
                    "organization_id": row.organization_id,
                    "telephony_configuration_id": row.id,
                    "host": host,
                    "port": int(port),
                    "esl_password": esl_password,
                    "dial_prefix": dial_prefix,
                    "audio_bridge_module": audio_bridge_module,
                }
            )

        return configs


async def main():
    """Entry point for the FreeSWITCH ESL manager process."""
    import signal

    manager = ESLManager()

    loop = asyncio.get_running_loop()
    shutdown_event = asyncio.Event()

    def signal_handler():
        logger.info("Received shutdown signal")
        shutdown_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, signal_handler)

    manager_task = asyncio.create_task(manager.start())

    await shutdown_event.wait()

    await manager.stop()
    manager_task.cancel()
    try:
        await manager_task
    except asyncio.CancelledError:
        pass

    logger.info("FreeSWITCH ESL Manager exited cleanly")


if __name__ == "__main__":
    asyncio.run(main())
