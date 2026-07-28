"""FreeSWITCH (ESL) implementation of the TelephonyProvider interface.

Uses FreeSWITCH's Event Socket Library (ESL, inbound mode) for call control
(originate, hangup, transfer) and relies on a separate esl_manager process
for event listening — mirroring how the ari/ provider splits control (ARI
REST) from events (a separate ari_manager process). See DESIGN.md for why
ESL was chosen over SIP/RTP or a REST/XML-RPC approach.
"""

import json
import uuid
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from loguru import logger

from api.db import db_client
from api.enums import TelephonyCallStatus, WorkflowRunMode
from api.services.telephony.base import (
    CallInitiationResult,
    NormalizedInboundData,
    TelephonyProvider,
)
from api.services.telephony.providers.freeswitch.esl_client import ESLTransport

if TYPE_CHECKING:
    from fastapi import WebSocket


class FreeswitchProvider(TelephonyProvider):
    """FreeSWITCH ESL implementation of TelephonyProvider.

    Uses ESL `bgapi`/`api` commands for call control and relies on a
    separate esl_manager process for ESL event listening (inbound calls,
    status changes). No HTTP webhooks exist for this provider — the ESL
    connection itself is the control+event channel and its password is the
    only authentication boundary.
    """

    PROVIDER_NAME = WorkflowRunMode.FREESWITCH.value
    WEBHOOK_ENDPOINT = None  # FreeSWITCH uses ESL events, not webhooks

    def __init__(self, config: Dict[str, Any]):
        """
        Initialize FreeswitchProvider with configuration.

        Args:
            config: Dictionary containing:
                - host: FreeSWITCH ESL host
                - port: FreeSWITCH ESL port (default 8021)
                - esl_password: mod_event_socket password
                - domain: FreeSWITCH SIP domain
                - dial_prefix: dial-string prefix for outbound origination
                - audio_bridge_module: which module bridges media (mod_audio_stream)
                - from_numbers: list of extensions/DIDs (optional)
        """
        self.host = config.get("host", "")
        self.port = int(config.get("port") or 8021)
        self.esl_password = config.get("esl_password", "")
        self.domain = config.get("domain", "")
        self.dial_prefix = config.get("dial_prefix", "")
        self.audio_bridge_module = config.get("audio_bridge_module", "mod_audio_stream")
        self.from_numbers = config.get("from_numbers", [])

        if isinstance(self.from_numbers, str):
            self.from_numbers = [self.from_numbers]

    async def _connect(self) -> ESLTransport:
        transport = ESLTransport(self.host, self.port, self.esl_password)
        await transport.connect()
        return transport

    async def initiate_call(
        self,
        to_number: str,
        webhook_url: str,
        workflow_run_id: Optional[int] = None,
        from_number: Optional[str] = None,
        **kwargs: Any,
    ) -> CallInitiationResult:
        """
        Originate an outbound call via ESL.

        Issues `bgapi originate` with the workflow context stamped as inline
        channel variables (the ESL equivalent of ARI's `appArgs`), landing
        the new leg in `park()` so esl_manager's CHANNEL_PARK handler can
        attach media once it observes the answer.
        """
        if not self.validate_config():
            raise ValueError("FreeSWITCH provider not properly configured")

        origination_uuid = str(uuid.uuid4())
        channel_vars = {
            "origination_uuid": origination_uuid,
            "workflow_run_id": str(workflow_run_id or ""),
            "workflow_id": str(kwargs.get("workflow_id", "")),
        }
        if from_number:
            channel_vars["origination_caller_id_number"] = from_number

        var_str = ",".join(f"{k}='{v}'" for k, v in channel_vars.items() if v)
        dial_string = f"{{{var_str}}}{self.dial_prefix}{to_number}"
        command = f"originate {dial_string} &park()"

        logger.info(
            f"[FreeSWITCH] Originating call to {to_number} "
            f"via {self.dial_prefix}, workflow_run_id={workflow_run_id}"
        )

        transport = await self._connect()
        try:
            reply = await transport.bgapi(command)
            if not reply.ok:
                logger.error(f"[FreeSWITCH] Originate failed: {reply.error_text}")
                raise ValueError(f"Failed to originate FreeSWITCH call: {reply.error_text}")

            return CallInitiationResult(
                call_id=origination_uuid,
                status="initiated",
                caller_number=from_number,
                provider_metadata={"call_id": origination_uuid},
                raw_response={"reply": reply.body, "headers": reply.headers},
            )
        finally:
            await transport.close()

    async def get_call_status(self, call_id: str) -> Dict[str, Any]:
        """Get channel status from FreeSWITCH via `uuid_dump`."""
        if not self.validate_config():
            raise ValueError("FreeSWITCH provider not properly configured")

        transport = await self._connect()
        try:
            reply = await transport.api(f"uuid_dump {call_id}")
            if not reply.ok:
                raise Exception(f"Failed to get channel status: {reply.error_text}")
            return {"call_id": call_id, "raw": reply.body}
        finally:
            await transport.close()

    async def get_available_phone_numbers(self) -> List[str]:
        """Return configured extensions/DIDs."""
        return self.from_numbers

    def validate_config(self) -> bool:
        """Cheap presence check — cannot perform a live ESL handshake here

        since this method's signature is synchronous. The real connectivity
        test happens in `preprocess_credentials_on_save` at config-save time
        (see __init__.py), which is async and I/O-permitted.
        """
        return bool(self.host and self.port and self.esl_password)

    async def verify_webhook_signature(
        self, url: str, params: Dict[str, Any], signature: str
    ) -> bool:
        """FreeSWITCH does not use webhook signatures - events come via ESL."""
        return True

    async def get_webhook_response(
        self, workflow_id: int, organization_id: int, workflow_run_id: int
    ) -> str:
        """FreeSWITCH does not use webhook responses - call control is via ESL."""
        logger.warning(
            "get_webhook_response called for FreeSWITCH - this should not happen. "
            "FreeSWITCH uses ESL for call control, not webhooks."
        )
        return ""

    async def get_call_cost(self, call_id: str) -> Dict[str, Any]:
        """FreeSWITCH does not provide call cost information."""
        return {
            "cost_usd": 0.0,
            "duration": 0,
            "status": "unknown",
            "error": "FreeSWITCH does not support cost retrieval",
        }

    def parse_status_callback(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Parse an ESL event dict (not an HTTP callback) into generic status
        callback format."""
        event_name = data.get("Event-Name", "")
        channel_state = data.get("Channel-State", "")

        event_status_map = {
            "CHANNEL_ANSWER": TelephonyCallStatus.ANSWERED,
            "CHANNEL_HANGUP": TelephonyCallStatus.COMPLETED,
            "CHANNEL_HANGUP_COMPLETE": TelephonyCallStatus.COMPLETED,
            "CHANNEL_PARK": TelephonyCallStatus.RINGING,
        }
        status = event_status_map.get(event_name, channel_state.lower() or event_name)

        return {
            "call_id": data.get("Unique-ID", ""),
            "status": status,
            "from_number": data.get("Caller-Caller-ID-Number"),
            "to_number": data.get("Caller-Destination-Number"),
            "direction": data.get("Call-Direction"),
            "duration": None,
            "extra": data,
        }

    async def handle_websocket(
        self,
        websocket: "WebSocket",
        workflow_id: int,
        organization_id: int,
        workflow_run_id: int,
    ) -> None:
        """Handle WebSocket connection from FreeSWITCH's mod_audio_stream.

        Unlike Twilio (JSON "connected"/"start" messages), mod_audio_stream
        sends an optional single metadata text frame then streams raw binary
        L16 PCM audio immediately — closer to Asterisk's chan_websocket.
        """
        from api.services.pipecat.run_pipeline import run_pipeline_telephony

        workflow_run = await db_client.get_workflow_run(
            workflow_run_id, organization_id=organization_id
        )
        channel_id = ""
        if workflow_run and workflow_run.gathered_context:
            channel_id = workflow_run.gathered_context.get("call_id", "")

        logger.info(
            f"[FreeSWITCH] Starting pipeline for workflow_run {workflow_run_id}, "
            f"channel={channel_id}"
        )

        await run_pipeline_telephony(
            websocket,
            provider_name=self.PROVIDER_NAME,
            workflow_id=workflow_id,
            workflow_run_id=workflow_run_id,
            organization_id=organization_id,
            call_id=channel_id,
            transport_kwargs={"channel_id": channel_id},
        )

    # ======== INBOUND CALL METHODS ========
    # No HTTP webhook exists for this provider — inbound calls are observed
    # via ESL events by esl_manager.py, not by these methods. They're
    # implemented anyway (mirroring the `ari` provider) since the ABC
    # requires them and other code may still reference the contract.

    @classmethod
    def can_handle_webhook(
        cls, webhook_data: Dict[str, Any], headers: Dict[str, str]
    ) -> bool:
        """FreeSWITCH does not use HTTP webhooks for inbound calls."""
        return False

    @staticmethod
    def parse_inbound_webhook(webhook_data: Dict[str, Any]) -> NormalizedInboundData:
        """Parse an ESL event dict into normalized inbound format."""
        return NormalizedInboundData(
            provider=FreeswitchProvider.PROVIDER_NAME,
            call_id=webhook_data.get("Unique-ID", ""),
            from_number=webhook_data.get("Caller-Caller-ID-Number", ""),
            to_number=webhook_data.get("Caller-Destination-Number", ""),
            direction="inbound",
            call_status=webhook_data.get("Channel-State", ""),
            account_id=None,
            raw_data=webhook_data,
        )

    @staticmethod
    def validate_account_id(config_data: dict, webhook_account_id: str) -> bool:
        """FreeSWITCH doesn't use account IDs for validation."""
        return True

    async def verify_inbound_signature(
        self,
        url: str,
        webhook_data: Dict[str, Any],
        headers: Dict[str, str],
        body: str = "",
    ) -> bool:
        """FreeSWITCH authenticates via the ESL password, not signatures."""
        return True

    async def start_inbound_stream(
        self,
        *,
        websocket_url: str,
        workflow_run_id: int,
        normalized_data,
        backend_endpoint: str,
    ):
        """FreeSWITCH does not generate HTTP responses for inbound calls."""
        from fastapi import Response

        return Response(content="", status_code=204)

    @staticmethod
    def generate_error_response(error_type: str, message: str) -> tuple:
        """Generate a generic JSON error response."""
        from fastapi import Response

        return Response(
            content=json.dumps({"error": error_type, "message": message}),
            media_type="application/json",
        )

    # ======== CALL TRANSFER METHODS ========
    #
    # FreeSWITCH has no Asterisk-style bridge object, so the shape differs
    # from ARI's bridge-swap but the protocol is the same: transfer_call()
    # originates a destination leg (like ARI's transfer_call), esl_manager
    # correlates its CHANNEL_ANSWER event back to the transfer_id and
    # publishes a DESTINATION_ANSWERED TransferEvent (like ARI's
    # _handle_destination_answered), and FreeswitchTransferStrategy (in
    # strategies.py) then directly `uuid_bridge`s the caller's channel to
    # the now-answered destination channel when the pipeline's EndFrame
    # (reason=TRANSFER_CALL) fires.

    def supports_transfers(self) -> bool:
        """FreeSWITCH supports call transfers via ESL originate + uuid_bridge."""
        return True

    async def transfer_call(
        self,
        destination: str,
        transfer_id: str,
        conference_name: str,
        timeout: int = 30,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """Originate the transfer destination channel.

        Mirrors ARIProvider.transfer_call: creates the destination channel
        and returns immediately. Completion happens asynchronously — the
        destination's CHANNEL_ANSWER event (observed by esl_manager) is
        correlated back to transfer_id and drives the bridge in
        FreeswitchTransferStrategy.
        """
        if not self.validate_config():
            raise ValueError("FreeSWITCH provider not properly configured")

        number = destination.split("/")[-1]
        origination_uuid = str(uuid.uuid4())
        channel_vars = {
            "origination_uuid": origination_uuid,
            "transfer_id": transfer_id,
        }
        var_str = ",".join(f"{k}='{v}'" for k, v in channel_vars.items())
        dial_string = f"{{{var_str}}}{self.dial_prefix}{number}"
        command = f"originate {dial_string} &park() {timeout}"

        logger.info(
            f"[FreeSWITCH Transfer] Originating transfer destination {number} "
            f"(transfer_id={transfer_id}, timeout={timeout}s)"
        )

        transport = await self._connect()
        try:
            reply = await transport.bgapi(command)
            if not reply.ok:
                logger.error(f"[FreeSWITCH Transfer] Originate failed: {reply.error_text}")
                raise Exception(
                    f"FreeSWITCH failed to originate transfer destination: {reply.error_text}"
                )

            return {
                "call_sid": origination_uuid,
                "status": "initiated",
                "provider": self.PROVIDER_NAME,
                "raw_response": {"reply": reply.body},
            }
        finally:
            await transport.close()

    # transfer_external_pbx_call intentionally left as the base-class
    # default (returns None) — no FusionPBX-specific PBX-passthrough
    # requirement has been requested yet. See DESIGN.md.
