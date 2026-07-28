"""FreeSWITCH (mod_audio_stream) WebSocket serializer for Pipecat.

Self-contained rather than re-exported from the pipecat submodule (unlike
ari/serializers.py) — see DESIGN.md for why: it keeps all FreeSWITCH-specific
code inside this provider package instead of adding a file to pipecat's own
upstream, a stricter isolation guarantee for this provider.

Wire protocol confirmed against amigniter/mod_audio_stream's actual source
(audio_streamer_glue.cpp, pinned to v1.0.3+), not just its README:
  - FreeSWITCH -> us: raw binary L16 PCM frames, no envelope (like Asterisk's
    chan_websocket, NOT like Twilio's base64-JSON). An optional single text
    frame carrying the verbatim `uuid_audio_stream start` metadata argument
    may arrive once before audio begins.
  - us -> FreeSWITCH (TTS playback): JSON text frames shaped
    {"type": "streamAudio", "data": {"audioDataType": "raw",
    "sampleRate": <rate>, "audioData": "<base64 L16 PCM>"}}.
This asymmetry (raw binary in, JSON+base64 out) is a real property of the
module, not an inconsistency in this file.
"""

import base64
import json
from typing import TYPE_CHECKING

from loguru import logger

from pipecat.audio.utils import create_stream_resampler
from pipecat.frames.frames import (
    AudioRawFrame,
    CancelFrame,
    EndFrame,
    Frame,
    InputAudioRawFrame,
    InterruptionFrame,
    StartFrame,
)
from pipecat.serializers.base_serializer import FrameSerializer
from pipecat.utils.enums import EndTaskReason

if TYPE_CHECKING:
    from pipecat.serializers.call_strategies import HangupStrategy, TransferStrategy


class FreeswitchFrameSerializer(FrameSerializer):
    """Serializer for FreeSWITCH mod_audio_stream WebSocket audio streaming.

    On EndFrame/CancelFrame, delegates to a HangupStrategy (uuid_kill) or,
    for transfer-reasoned end frames, a TransferStrategy (uuid_bridge to an
    already-answered destination channel).
    """

    class InputParams(FrameSerializer.InputParams):
        """Configuration parameters for FreeswitchFrameSerializer.

        Parameters:
            freeswitch_sample_rate: Sample rate configured on the
                `uuid_audio_stream start` command, defaults to 8000 Hz.
            sample_rate: Optional override for pipeline input sample rate.
            auto_hang_up: Whether to automatically terminate the channel on EndFrame.
        """

        freeswitch_sample_rate: int = 8000
        sample_rate: int | None = None
        auto_hang_up: bool = True

    def __init__(
        self,
        channel_id: str,
        host: str,
        port: int,
        esl_password: str,
        transfer_strategy: "TransferStrategy | None" = None,
        hangup_strategy: "HangupStrategy | None" = None,
        params: InputParams | None = None,
    ):
        """Initialize the FreeswitchFrameSerializer.

        Args:
            channel_id: The FreeSWITCH channel UUID.
            host: FreeSWITCH ESL host, used by the hangup/transfer strategies.
            port: FreeSWITCH ESL port.
            esl_password: FreeSWITCH ESL password.
            transfer_strategy: Strategy for handling call transfers.
            hangup_strategy: Strategy for handling call hangups.
            params: Configuration parameters.
        """
        params = params or FreeswitchFrameSerializer.InputParams()
        super().__init__(params)
        self._params: FreeswitchFrameSerializer.InputParams = params

        self._channel_id = channel_id
        self._host = host
        self._port = port
        self._esl_password = esl_password
        self._transfer_strategy = transfer_strategy
        self._hangup_strategy = hangup_strategy

        self._freeswitch_sample_rate = self._params.freeswitch_sample_rate
        self._sample_rate = 0  # Pipeline input rate, set in setup()

        self._input_resampler = create_stream_resampler(
            clear_after_secs=self._params.resampler_clear_after_secs
        )
        self._output_resampler = create_stream_resampler(
            clear_after_secs=self._params.resampler_clear_after_secs
        )
        self._hangup_attempted = False
        self._transfer_attempted = False

    async def setup(self, frame: StartFrame):
        """Sets up the serializer with pipeline configuration."""
        self._sample_rate = self._params.sample_rate or frame.audio_in_sample_rate

    def _strategy_context(self) -> dict:
        return {
            "channel_id": self._channel_id,
            "host": self._host,
            "port": self._port,
            "esl_password": self._esl_password,
        }

    async def serialize(self, frame: Frame) -> str | bytes | None:
        """Serializes a Pipecat frame to FreeSWITCH mod_audio_stream format."""
        if isinstance(frame, (EndFrame, CancelFrame)):
            frame_reason = getattr(frame, "reason", None)
            logger.debug(f"Processing {type(frame).__name__} with reason: {frame_reason}")

            if (
                frame_reason == EndTaskReason.TRANSFER_CALL.value
                and not self._transfer_attempted
            ):
                self._transfer_attempted = True
                if self._transfer_strategy:
                    success = await self._transfer_strategy.execute_transfer(
                        self._strategy_context()
                    )
                    if not success:
                        logger.error(f"Transfer strategy failed for channel {self._channel_id}")
                else:
                    logger.warning(
                        f"No transfer strategy configured for channel {self._channel_id}"
                    )
                return None
            elif (
                self._params.auto_hang_up
                and not self._hangup_attempted
                and frame_reason != EndTaskReason.TRANSFER_CALL.value
            ):
                self._hangup_attempted = True
                if self._hangup_strategy:
                    success = await self._hangup_strategy.execute_hangup(
                        self._strategy_context()
                    )
                    if not success:
                        logger.error(f"Hangup strategy failed for channel {self._channel_id}")
                else:
                    logger.warning(f"No hangup strategy configured for channel {self._channel_id}")
                return None
        elif isinstance(frame, InterruptionFrame):
            # mod_audio_stream has no documented "clear playback" WS command;
            # returning None stops us from sending more audio, same as ari/.
            return None
        elif isinstance(frame, AudioRawFrame):
            resampled = await self._output_resampler.resample(
                frame.audio, frame.sample_rate, self._freeswitch_sample_rate
            )
            if resampled is None or len(resampled) == 0:
                return None

            message = {
                "type": "streamAudio",
                "data": {
                    "audioDataType": "raw",
                    "sampleRate": self._freeswitch_sample_rate,
                    "audioData": base64.b64encode(resampled).decode("ascii"),
                },
            }
            return json.dumps(message)

        return None

    async def deserialize(self, data: str | bytes) -> Frame | None:
        """Deserializes FreeSWITCH mod_audio_stream WebSocket data to Pipecat frames.

        Binary messages are raw L16 PCM audio bytes. Text messages are either
        the verbatim initial-metadata string (not JSON-wrapped by the
        module) or, if we ever choose to send our own text control frames
        for round-tripping, JSON — logged either way, no frame produced.
        """
        if isinstance(data, bytes):
            deserialized_data = await self._input_resampler.resample(
                data, self._freeswitch_sample_rate, self._sample_rate
            )
            if deserialized_data is None or len(deserialized_data) == 0:
                return None

            return InputAudioRawFrame(
                audio=deserialized_data,
                num_channels=1,  # FreeSWITCH mono capture (mix-type "mono")
                sample_rate=self._sample_rate,
            )
        else:
            try:
                message = json.loads(data)
                logger.debug(f"FreeSWITCH WebSocket JSON message: {message}")
            except json.JSONDecodeError:
                # Verbatim initial-metadata text frame, not JSON — expected once.
                logger.debug(f"FreeSWITCH initial metadata: {data}")
            return None
