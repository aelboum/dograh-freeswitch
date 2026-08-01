import asyncio
import base64
import json
from unittest.mock import AsyncMock, patch

import pytest
from pipecat.frames.frames import (
    EndFrame,
    InputAudioRawFrame,
    InterruptionFrame,
    OutputAudioRawFrame,
    StartFrame,
)

from api.services.telephony.providers.freeswitch.serializers import (
    FreeswitchFrameSerializer,
)


def _serializer(**overrides):
    hangup_strategy = overrides.pop("hangup_strategy", None)
    transfer_strategy = overrides.pop("transfer_strategy", None)
    params = FreeswitchFrameSerializer.InputParams(**overrides) if overrides else None
    return FreeswitchFrameSerializer(
        channel_id="chan-uuid",
        host="10.10.10.17",
        port=8021,
        esl_password="ClueCon",
        hangup_strategy=hangup_strategy,
        transfer_strategy=transfer_strategy,
        params=params,
    )


@pytest.mark.asyncio
async def test_deserialize_binary_audio_produces_input_frame():
    serializer = _serializer()
    await serializer.setup(StartFrame(audio_in_sample_rate=8000, audio_out_sample_rate=8000))

    # 8000Hz L16 mono silence, matching freeswitch_sample_rate == pipeline rate
    # so the resampler is a passthrough (no ratio conversion needed).
    raw_pcm = b"\x00\x00" * 160
    frame = await serializer.deserialize(raw_pcm)

    assert isinstance(frame, InputAudioRawFrame)
    assert frame.num_channels == 1
    assert frame.sample_rate == 8000


@pytest.mark.asyncio
async def test_deserialize_verbatim_text_metadata_is_ignored():
    serializer = _serializer()
    await serializer.setup(StartFrame(audio_in_sample_rate=8000, audio_out_sample_rate=8000))

    frame = await serializer.deserialize("dograh-run-42")
    assert frame is None


@pytest.mark.asyncio
async def test_serialize_audio_frame_wraps_json_base64_stream_audio():
    serializer = _serializer()
    await serializer.setup(StartFrame(audio_in_sample_rate=8000, audio_out_sample_rate=8000))

    # Forwarded immediately, no client-side batching — see module docstring
    # for why (mod_audio_stream's own small ring buffer needs a steady near
    # real-time trickle, not large infrequent bursts).
    pcm = b"\x01\x02" * 160
    frame = OutputAudioRawFrame(audio=pcm, sample_rate=8000, num_channels=1)
    serialized = await serializer.serialize(frame)

    assert isinstance(serialized, str)
    message = json.loads(serialized)
    assert message["type"] == "streamAudio"
    assert message["data"]["audioDataType"] == "raw"
    assert message["data"]["sampleRate"] == 8000
    decoded = base64.b64decode(message["data"]["audioData"])
    assert len(decoded) > 0


@pytest.mark.asyncio
async def test_serialize_end_frame_calls_hangup_strategy():
    hangup_strategy = AsyncMock()
    hangup_strategy.execute_hangup = AsyncMock(return_value=True)
    serializer = _serializer(hangup_strategy=hangup_strategy, hangup_grace_ms=0)
    await serializer.setup(StartFrame(audio_in_sample_rate=8000, audio_out_sample_rate=8000))

    result = await serializer.serialize(EndFrame())

    assert result is None
    # Hangup runs via a fire-and-forget asyncio.create_task (deferred by
    # hangup_grace_ms so any already-sent trailing audio gets a chance to
    # play out of mod_audio_stream's own buffer first); give it a turn on
    # the loop before asserting on it.
    await asyncio.sleep(0)
    hangup_strategy.execute_hangup.assert_awaited_once()
    context = hangup_strategy.execute_hangup.call_args.args[0]
    assert context["channel_id"] == "chan-uuid"
    assert context["host"] == "10.10.10.17"


@pytest.mark.asyncio
async def test_serialize_interruption_frame_stops_without_sending():
    serializer = _serializer()
    await serializer.setup(StartFrame(audio_in_sample_rate=8000, audio_out_sample_rate=8000))

    result = await serializer.serialize(InterruptionFrame())

    assert result is None


class _FakeClock:
    """Deterministic stand-in for time.monotonic()/asyncio.sleep(), so pacing
    math can be asserted exactly without real wall-clock waits. sleep()
    advances the clock by the requested duration, mirroring what really
    sleeping would do to a subsequent time.monotonic() read."""

    def __init__(self, start: float = 1000.0):
        self.now = start
        self.sleep_calls: list[float] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float):
        self.sleep_calls.append(seconds)
        self.now += seconds


@pytest.mark.asyncio
async def test_pace_sends_first_frame_immediately_without_sleeping():
    serializer = _serializer()
    clock = _FakeClock()
    with (
        patch("api.services.telephony.providers.freeswitch.serializers.time.monotonic", clock.monotonic),
        patch("api.services.telephony.providers.freeswitch.serializers.asyncio.sleep", clock.sleep),
    ):
        # 100ms of 8kHz/16-bit audio: 8000 * 2 * 0.1 = 1600 bytes.
        await serializer._pace(1600)

    assert clock.sleep_calls == []
    assert serializer._playback_deadline == pytest.approx(1000.1)


@pytest.mark.asyncio
async def test_pace_sleeps_once_lead_exceeds_playback_lead_ms():
    serializer = _serializer(playback_lead_ms=250)
    clock = _FakeClock()
    with (
        patch("api.services.telephony.providers.freeswitch.serializers.time.monotonic", clock.monotonic),
        patch("api.services.telephony.providers.freeswitch.serializers.asyncio.sleep", clock.sleep),
    ):
        # Four 100ms chunks sent back-to-back (clock not advancing between
        # calls, as if the TTS backend streamed them instantly). Each call
        # checks the lead already banked from *prior* chunks before adding
        # its own: after 3 calls that's 0/0.1/0.2s (all <= 0.25s allowed),
        # so only the 4th call (checking the 0.3s banked by the first three)
        # exceeds the allowed lead and sleeps.
        chunk_bytes = 1600  # 100ms at 8kHz/16-bit
        await serializer._pace(chunk_bytes)
        await serializer._pace(chunk_bytes)
        await serializer._pace(chunk_bytes)
        assert clock.sleep_calls == []
        await serializer._pace(chunk_bytes)

    assert clock.sleep_calls == [pytest.approx(0.05)]  # 0.3s banked - 0.25s allowed
    # sleep(0.05) advances the clock to 1000.05; deadline resets to
    # now + allowed_lead (1000.3), then the 4th chunk's own 0.1s is added.
    assert serializer._playback_deadline == pytest.approx(1000.4)


@pytest.mark.asyncio
async def test_interruption_frame_resets_pacing_deadline():
    serializer = _serializer()
    await serializer.setup(StartFrame(audio_in_sample_rate=8000, audio_out_sample_rate=8000))

    frame = OutputAudioRawFrame(audio=b"\x01\x02" * 160, sample_rate=8000, num_channels=1)
    await serializer.serialize(frame)
    assert serializer._playback_deadline is not None

    await serializer.serialize(InterruptionFrame())

    assert serializer._playback_deadline is None


@pytest.mark.asyncio
async def test_create_transport_raises_when_credentials_incomplete():
    from api.services.telephony.providers.freeswitch.transport import create_transport

    with patch(
        "api.services.telephony.providers.freeswitch.transport.load_credentials_for_transport",
        new=AsyncMock(return_value={"host": "", "esl_password": ""}),
    ):
        from unittest.mock import MagicMock

        from api.services.pipecat.audio_config import AudioConfig

        with pytest.raises(ValueError):
            await create_transport(
                MagicMock(),
                workflow_run_id=1,
                audio_config=AudioConfig(
                    transport_in_sample_rate=8000,
                    transport_out_sample_rate=8000,
                    pipeline_sample_rate=16000,
                ),
                organization_id=2,
                channel_id="chan-uuid",
            )
