import base64
import json
from unittest.mock import AsyncMock, patch

import pytest
from pipecat.frames.frames import EndFrame, InputAudioRawFrame, OutputAudioRawFrame, StartFrame

from api.services.telephony.providers.freeswitch.serializers import (
    FreeswitchFrameSerializer,
)


def _serializer(**overrides):
    hangup_strategy = overrides.pop("hangup_strategy", None)
    transfer_strategy = overrides.pop("transfer_strategy", None)
    return FreeswitchFrameSerializer(
        channel_id="chan-uuid",
        host="10.10.10.17",
        port=8021,
        esl_password="ClueCon",
        hangup_strategy=hangup_strategy,
        transfer_strategy=transfer_strategy,
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
    serializer = _serializer(hangup_strategy=hangup_strategy)
    await serializer.setup(StartFrame(audio_in_sample_rate=8000, audio_out_sample_rate=8000))

    result = await serializer.serialize(EndFrame())

    assert result is None
    hangup_strategy.execute_hangup.assert_awaited_once()
    context = hangup_strategy.execute_hangup.call_args.args[0]
    assert context["channel_id"] == "chan-uuid"
    assert context["host"] == "10.10.10.17"


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
