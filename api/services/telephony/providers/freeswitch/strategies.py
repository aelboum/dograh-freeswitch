"""FreeSWITCH-specific call operation strategies.

Business logic for FreeSWITCH ESL call operations, invoked by
FreeswitchFrameSerializer on EndFrame/CancelFrame. Simpler than ARI's
bridge-swap strategies since FreeSWITCH has no separate bridge object: a
transfer is a fresh ESL connection issuing `uuid_bridge` to directly connect
the caller's channel to the already-answered destination channel that
`FreeswitchProvider.transfer_call` originated.
"""

from typing import Any, Dict

from loguru import logger
from pipecat.serializers.call_strategies import HangupStrategy, TransferStrategy

from .esl_client import ESLTransport


class FreeswitchTransferStrategy(TransferStrategy):
    """Bridges the caller's channel to an already-answered transfer destination.

    The destination channel was originated by `FreeswitchProvider.transfer_call`
    and its CHANNEL_ANSWER event correlated back to the transfer_id by
    esl_manager.py (mirroring ARI's `_handle_destination_answered`), which is
    what populates the transfer context this strategy looks up.
    """

    async def execute_transfer(self, context: Dict[str, Any]) -> bool:
        try:
            from api.services.telephony.call_transfer_manager import (
                get_call_transfer_manager,
            )

            channel_id = context["channel_id"]
            host = context["host"]
            port = context["port"]
            esl_password = context["esl_password"]

            if not channel_id or not host:
                logger.warning(
                    "Cannot execute FreeSWITCH transfer: missing channel_id or host"
                )
                return False

            call_transfer_manager = await get_call_transfer_manager()
            transfer_context = await call_transfer_manager.find_transfer_context_for_call(
                channel_id
            )
            if not transfer_context:
                logger.error(
                    f"[FreeSWITCH Transfer] No active transfer context found for "
                    f"caller {channel_id}"
                )
                return False

            destination_channel_id = transfer_context.call_sid
            if not destination_channel_id:
                logger.error(
                    "[FreeSWITCH Transfer] No destination channel in transfer context"
                )
                return False

            logger.info(
                f"[FreeSWITCH Transfer] Bridging caller {channel_id} to "
                f"destination {destination_channel_id} "
                f"(transfer_id={transfer_context.transfer_id})"
            )

            transport = ESLTransport(host, int(port), esl_password)
            await transport.connect()
            try:
                reply = await transport.api(
                    f"uuid_bridge {channel_id} {destination_channel_id}"
                )
                if not reply.ok:
                    logger.error(
                        f"[FreeSWITCH Transfer] uuid_bridge failed: {reply.error_text}"
                    )
                    return False
            finally:
                await transport.close()

            await call_transfer_manager.remove_transfer_context(
                transfer_context.transfer_id
            )
            logger.info(
                f"[FreeSWITCH Transfer] Bridge completed: {channel_id} <-> "
                f"{destination_channel_id}"
            )
            return True

        except Exception as e:
            logger.exception(f"Failed to execute FreeSWITCH transfer: {e}")
            return False


class FreeswitchHangupStrategy(HangupStrategy):
    """Hangs up a FreeSWITCH channel via ESL `uuid_kill`."""

    async def execute_hangup(self, context: Dict[str, Any]) -> bool:
        try:
            channel_id = context["channel_id"]
            host = context["host"]
            port = context["port"]
            esl_password = context["esl_password"]

            if not channel_id or not host:
                logger.warning(
                    "Cannot hang up FreeSWITCH channel: missing channel_id or host"
                )
                return False

            transport = ESLTransport(host, int(port), esl_password)
            await transport.connect()
            try:
                reply = await transport.api(f"uuid_kill {channel_id}")
                if reply.ok:
                    logger.info(f"Successfully terminated FreeSWITCH channel {channel_id}")
                    return True
                if "No Such Channel" in reply.error_text:
                    logger.debug(f"FreeSWITCH channel {channel_id} was already terminated")
                    return True
                logger.error(
                    f"Failed to terminate FreeSWITCH channel {channel_id}: "
                    f"{reply.error_text}"
                )
                return False
            finally:
                await transport.close()

        except Exception as e:
            logger.exception(f"Failed to hang up FreeSWITCH channel: {e}")
            return False
