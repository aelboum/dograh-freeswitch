"""FreeSWITCH (mod_audio_stream) WebSocket serializer for Pipecat.

Self-contained rather than re-exported from the pipecat submodule (unlike
ari/serializers.py) — see DESIGN.md for why: it keeps all FreeSWITCH-specific
code inside this provider package instead of adding a file to pipecat's own
upstream, a stricter isolation guarantee for this provider.

Wire protocol confirmed against amigniter/mod_audio_stream's actual source
(audio_streamer_glue.cpp), not just its README:
  - FreeSWITCH -> us: raw binary L16 PCM frames, no envelope (like Asterisk's
    chan_websocket, NOT like Twilio's base64-JSON). An optional single text
    frame carrying the verbatim `uuid_audio_stream start` metadata argument
    may arrive once before audio begins.
  - us -> FreeSWITCH (TTS playback): JSON text frames shaped
    {"type": "streamAudio", "data": {"audioDataType": "raw",
    "sampleRate": <rate>, "audioData": "<base64 L16 PCM>"}}.
This asymmetry (raw binary in, JSON+base64 out) is a real property of the
module, not an inconsistency in this file.

Correction (verified against the actual open-source module, CMakeLists
version 1.0.0, not the README's newer "commercial edition" claims): the
module does NOT play `streamAudio` messages back onto the channel itself. It
decodes each message to a temp file and fires a CUSTOM
`mod_audio_stream::play` FreeSWITCH event carrying the file path — an
external ESL listener is expected to catch that event and actually play the
file (`esl_manager.py`'s `_handle_audio_stream_play` does this via
`uuid_broadcast`). `playback_buffer_ms` below batches audio before sending
specifically to keep the resulting broadcast rate low enough to sound smooth.
"""

import asyncio
import base64
import json
import time
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
    TTSStoppedFrame,
)
from pipecat.serializers.base_serializer import FrameSerializer
from pipecat.utils.enums import EndTaskReason

from .esl_client import ESLTransport

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
            playback_buffer_ms: How much outbound audio to accumulate before
                emitting one `streamAudio` WS message. mod_audio_stream writes
                each message to its own temp file and fires a CUSTOM event for
                esl_manager to `uuid_broadcast` — at the pipeline's native
                ~20-40ms frame size that's 25-50 broadcasts/sec, and each
                broadcast's playback-setup overhead is audible as choppiness.
                Batching trades a bit of latency for fewer, larger broadcasts.
            priming_lead_ms: Extra audio held back before the *first* chunk
                of a fresh utterance is sent, on top of playback_buffer_ms.
                Measured on this exact deployment: a consistent, fixed ~100ms
                silence gap appears between every chunk's playback ending and
                the next one starting — the round-trip cost of buffer-full ->
                WS message -> mod_audio_stream file write -> CUSTOM event ->
                esl_manager pickup -> `uuid_broadcast`. Chunk N+1 is normally
                only "ready" right as chunk N finishes, leaving zero slack to
                absorb that cost, so it surfaces as an audible gap on *every*
                chunk boundary regardless of playback_buffer_ms — a bigger
                buffer only made gaps less frequent, never gap-free. Delaying
                the first chunk of each utterance by one extra buffer's worth
                front-loads a standing lead that comfortably covers the
                measured ~100ms tax for every subsequent chunk in that
                utterance, the standard jitter-buffer pre-roll trick.
            priming_idle_reset_ms: Gap (no AudioRawFrame) after which the next
                one is treated as starting a new utterance and re-primed,
                rather than a continuation with an already-established lead.
        """

        freeswitch_sample_rate: int = 8000
        sample_rate: int | None = None
        auto_hang_up: bool = True
        playback_buffer_ms: int = 250
        priming_lead_ms: int = 350
        priming_idle_reset_ms: int = 500

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

        self._playback_buffer = bytearray()
        self._playback_buffer_threshold_bytes = int(
            self._freeswitch_sample_rate * 2 * self._params.playback_buffer_ms / 1000
        )
        self._priming_lead_bytes = int(
            self._freeswitch_sample_rate * 2 * self._params.priming_lead_ms / 1000
        )
        self._priming = True
        self._last_audio_activity: float | None = None

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

            # The playback buffer may still hold up to playback_buffer_ms of
            # the final utterance (e.g. the tail of "have a nice day") since
            # AudioRawFrames are batched rather than sent immediately. Flush
            # it now — it's returned below so the transport actually sends
            # it — and give FreeSWITCH time to play it out before the
            # hangup/transfer strategy tears the channel down.
            pending_audio = self._flush_playback_buffer()

            # Decide + claim which action to run synchronously (same
            # attempted-flag guard as before) so a second EndFrame/CancelFrame
            # arriving before the deferred task below has run can't race into
            # running the same strategy twice. Only the awaiting of the
            # strategy itself — and the delay to let pending_audio play out —
            # is deferred.
            do_transfer = (
                frame_reason == EndTaskReason.TRANSFER_CALL.value
                and not self._transfer_attempted
            )
            do_hangup = (
                not do_transfer
                and self._params.auto_hang_up
                and not self._hangup_attempted
                and frame_reason != EndTaskReason.TRANSFER_CALL.value
            )
            if do_transfer:
                self._transfer_attempted = True
            elif do_hangup:
                self._hangup_attempted = True

            async def _finalize():
                if pending_audio is not None:
                    await asyncio.sleep(self._params.playback_buffer_ms / 1000)

                if do_transfer:
                    if self._transfer_strategy:
                        success = await self._transfer_strategy.execute_transfer(
                            self._strategy_context()
                        )
                        if not success:
                            logger.error(
                                f"Transfer strategy failed for channel {self._channel_id}"
                            )
                    else:
                        logger.warning(
                            f"No transfer strategy configured for channel {self._channel_id}"
                        )
                elif do_hangup:
                    if self._hangup_strategy:
                        success = await self._hangup_strategy.execute_hangup(
                            self._strategy_context()
                        )
                        if not success:
                            logger.error(
                                f"Hangup strategy failed for channel {self._channel_id}"
                            )
                    else:
                        logger.warning(
                            f"No hangup strategy configured for channel {self._channel_id}"
                        )

            if do_transfer or do_hangup:
                asyncio.create_task(_finalize())
            return pending_audio
        elif isinstance(frame, InterruptionFrame):
            # Dropping our own buffer only stops audio not yet sent — chunks
            # already flushed to mod_audio_stream have already been
            # uuid_broadcast'd by esl_manager (possibly several, queued,
            # since each broadcast is subject to FreeSWITCH's own lead-frame
            # delay) and keep playing on the channel regardless. Clearing the
            # buffer alone is not a barge-in: the bot keeps talking. Actually
            # stop the channel's current + queued playback via `uuid_break
            # ... all` (mirrors the fresh-connection-per-command pattern
            # strategies.py uses for uuid_kill/uuid_bridge — interruptions
            # are far less frequent than audio chunks, so this doesn't need
            # esl_manager's persistent playback connection).
            self._playback_buffer.clear()
            self._priming = True
            self._last_audio_activity = None
            asyncio.create_task(self._break_playback())
            return None
        elif isinstance(frame, TTSStoppedFrame):
            # Guaranteed by TTSService for every normally-completed utterance
            # (base-class fallback if the backend doesn't emit its own; see
            # _handle_audio_context in pipecat's tts_service.py), and skipped
            # only on interruption — which InterruptionFrame above already
            # handles by clearing instead. Flush whatever's left under the
            # playback_buffer_ms threshold so the tail of the utterance isn't
            # stranded until the next one starts (or dropped by the next
            # InterruptionFrame).
            return self._flush_playback_buffer()
        elif isinstance(frame, AudioRawFrame):
            now = time.monotonic()
            if (
                self._last_audio_activity is not None
                and (now - self._last_audio_activity) * 1000
                > self._params.priming_idle_reset_ms
            ):
                # No audio for a while — this is a new utterance (e.g. the
                # next agent turn), not a continuation, so it no longer has
                # the standing lead built up during the previous one.
                self._priming = True
            self._last_audio_activity = now

            resampled = await self._output_resampler.resample(
                frame.audio, frame.sample_rate, self._freeswitch_sample_rate
            )
            if resampled is None or len(resampled) == 0:
                return None

            self._playback_buffer.extend(resampled)
            required = self._playback_buffer_threshold_bytes + (
                self._priming_lead_bytes if self._priming else 0
            )
            if len(self._playback_buffer) < required:
                return None

            self._priming = False
            return self._flush_playback_buffer()

        return None

    def _flush_playback_buffer(self) -> str | None:
        if not self._playback_buffer:
            return None
        chunk = bytes(self._playback_buffer)
        self._playback_buffer.clear()
        message = {
            "type": "streamAudio",
            "data": {
                "audioDataType": "raw",
                "sampleRate": self._freeswitch_sample_rate,
                "audioData": base64.b64encode(chunk).decode("ascii"),
            },
        }
        return json.dumps(message)

    async def _break_playback(self):
        """Stops current + queued playback on the channel via `uuid_break ... all`.

        Fire-and-forget from serialize() so a live interruption isn't held up
        waiting on this ESL round trip.
        """
        try:
            transport = ESLTransport(self._host, int(self._port), self._esl_password)
            await transport.connect()
            try:
                reply = await transport.api(f"uuid_break {self._channel_id} all")
                if not reply.ok:
                    logger.warning(
                        f"uuid_break failed for channel {self._channel_id}: {reply.error_text}"
                    )
            finally:
                await transport.close()
        except Exception as e:
            logger.warning(
                f"Failed to break FreeSWITCH playback for channel {self._channel_id}: {e}"
            )

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
