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

The open-source community edition (CMakeLists version 1.0.0) does NOT play
`streamAudio` messages back onto the channel itself — it decodes each
message to a temp file and fires a CUSTOM `mod_audio_stream::play`
FreeSWITCH event for an external ESL listener to `uuid_broadcast`.

**This box currently runs a different build**: the voxcom-us fork
(github.com/voxcom-us/mod_audio_stream), which instead writes decoded audio
directly into a per-channel ring buffer (`write_sbuffer`) drained in
real-time (one 20ms frame per tick) by its own write thread — no temp file,
no CUSTOM event, no `uuid_broadcast` round trip. Confirmed by reading its
`audio_streamer_glue.cpp` on the box directly. Since it never fires
`mod_audio_stream::play` for raw-PCM `streamAudio` messages (the only
message shape Dograh sends), `esl_manager.py`'s `_handle_audio_stream_play`/
persistent playback connection is dead code *for this specific module
build* — left in place because it's still correct for the community
edition, which a different operator's box may run.

That ring buffer defaults to just one 20ms frame of capacity
(`STREAM_BUFFER_SIZE` channel var unset), so the write thread silently skips
a tick — no audio, not even silence — whenever it runs dry between our WS
sends. `esl_manager.py` sets `STREAM_BUFFER_SIZE=200` (ms) via `uuid_setvar`
before `uuid_audio_stream start` to give it real slack. On the Dograh side,
this means audio should be forwarded close to real-time as the pipeline
produces it (see `AudioRawFrame` handling below) rather than batched into
large client-side chunks — large infrequent bursts starve the ring buffer
between sends, which is exactly what caused a live call to go silent in
long, repeating gaps (2026-08-01).

Not batching client-side is necessary but not sufficient, though: pipecat's
`BaseOutputTransport` (`transports/base_output.py`) does not itself pace
audio frames to real-time — it forwards each `TTSAudioRawFrame` downstream
as fast as the TTS backend streams it (confirmed by reading
`_audio_task_handler`/`_next_frame`, no real-time sleep in the no-mixer
path this provider uses). A TTS backend streaming faster than real-time
(common — audio often generates quicker than it plays) would otherwise let
Dograh get arbitrarily far ahead: every `streamAudio` message already
handed to FreeSWITCH is data it will play regardless of a later
`InterruptionFrame` (no clean way to claw it back — see the
`InterruptionFrame` branch below), so getting far ahead of real-time
silently defeats barge-in even though playback itself sounds fine. `_pace`
below throttles our own send rate to at most `playback_lead_ms` ahead of
real-time, which both keeps FreeSWITCH's small ring buffer fed *and* bounds
how much audio can possibly be "already committed" at interruption time.
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
            hangup_grace_ms: Delay before running the hangup/transfer strategy
                on EndFrame/CancelFrame, so the last already-sent audio (still
                draining out of mod_audio_stream's own playback buffer, see
                module docstring) isn't cut off mid-word. Matches
                esl_manager.py's STREAM_BUFFER_SIZE.
            playback_lead_ms: Maximum amount of audio (by playback duration)
                we allow ourselves to send ahead of real-time. See module
                docstring for why this matters beyond just avoiding
                mod_audio_stream buffer underruns: it also bounds how much
                audio can be "already committed" to play at the moment an
                InterruptionFrame arrives. Kept close to esl_manager.py's
                STREAM_BUFFER_SIZE (slightly above it, for jitter margin).
        """

        freeswitch_sample_rate: int = 8000
        sample_rate: int | None = None
        auto_hang_up: bool = True
        hangup_grace_ms: int = 200
        playback_lead_ms: int = 250

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

        # Monotonic time at which audio already sent will finish playing,
        # i.e. how far ahead of real-time we currently are. None means "not
        # currently pacing" (start of call, or just reset by an
        # InterruptionFrame/idle gap).
        self._playback_deadline: float | None = None

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

            # Decide + claim which action to run synchronously (attempted-flag
            # guard) so a second EndFrame/CancelFrame arriving before the
            # deferred task below has run can't race into running the same
            # strategy twice. Only the awaiting of the strategy itself — and
            # the grace delay for already-sent audio to drain out of
            # mod_audio_stream's own playback buffer — is deferred.
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
                if self._params.hangup_grace_ms > 0:
                    await asyncio.sleep(self._params.hangup_grace_ms / 1000)

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
            return None
        elif isinstance(frame, InterruptionFrame):
            # mod_audio_stream has no documented "clear playback" WS command,
            # and its `flush` ESL API command only discards audio still in
            # transit — the currently-loaded fork build has its actual
            # buffer-zeroing step disabled (see module docstring), so it
            # wouldn't cut off audio already sitting in the module's own
            # playback buffer anyway. Stopping here — not sending more — is
            # sufficient *because* AudioRawFrame handling below paces sends
            # to at most playback_lead_ms ahead of real-time: whatever's
            # "already committed" at this point is bounded by that margin,
            # not by however far ahead the TTS backend happened to stream.
            # Reset pacing so the next utterance doesn't inherit a stale
            # deadline from the one just cut off.
            self._playback_deadline = None
            return None
        elif isinstance(frame, AudioRawFrame):
            resampled = await self._output_resampler.resample(
                frame.audio, frame.sample_rate, self._freeswitch_sample_rate
            )
            if resampled is None or len(resampled) == 0:
                return None

            await self._pace(len(resampled))

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

    async def _pace(self, resampled_byte_count: int):
        """Sleeps just enough to keep our send rate within playback_lead_ms
        of real-time, then accounts for the chunk about to be sent.

        See module docstring: pipecat's output transport doesn't do this
        pacing itself, so without it we could hand FreeSWITCH arbitrarily
        more audio than it's actually played, which both risks overrunning
        mod_audio_stream's tiny ring buffer and makes InterruptionFrame
        unable to bound how much audio is already "committed".
        """
        now = time.monotonic()
        deadline = max(self._playback_deadline or now, now)
        lead = deadline - now
        allowed_lead = self._params.playback_lead_ms / 1000
        if lead > allowed_lead:
            await asyncio.sleep(lead - allowed_lead)
            deadline = time.monotonic() + allowed_lead

        # L16 mono: 2 bytes/sample.
        duration_secs = resampled_byte_count / (self._freeswitch_sample_rate * 2)
        self._playback_deadline = deadline + duration_secs

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
