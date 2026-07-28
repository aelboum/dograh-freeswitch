"""FreeSWITCH telephony configuration schemas."""

from typing import List, Literal

from pydantic import BaseModel, Field


class FreeswitchConfigurationRequest(BaseModel):
    """Request schema for FreeSWITCH (ESL) configuration."""

    provider: Literal["freeswitch"] = Field(default="freeswitch")
    host: str = Field(..., description="FreeSWITCH box's ESL-reachable hostname/IP")
    port: int = Field(default=8021, description="mod_event_socket ESL port")
    esl_password: str = Field(..., description="mod_event_socket ESL password")
    domain: str = Field(..., description="FreeSWITCH SIP domain / FusionPBX domain")
    dial_prefix: str = Field(
        ...,
        description=(
            "Dial-string prefix for outbound origination, e.g. "
            "'sofia/gateway/my-trunk/' or 'sofia/internal/'. Operator-specific "
            "and cannot be validated until the first call attempt."
        ),
    )
    audio_bridge_module: Literal["mod_audio_stream"] = Field(
        default="mod_audio_stream",
        description="FreeSWITCH module used to bridge call audio over WebSocket",
    )
    from_numbers: List[str] = Field(
        default_factory=list,
        description="Extensions/DIDs available for outbound calls (optional)",
    )


class FreeswitchConfigurationResponse(BaseModel):
    """Response schema for FreeSWITCH configuration with masked sensitive fields."""

    provider: Literal["freeswitch"] = Field(default="freeswitch")
    host: str
    port: int
    esl_password: str  # Masked
    domain: str
    dial_prefix: str
    audio_bridge_module: Literal["mod_audio_stream"] = "mod_audio_stream"
    from_numbers: List[str]
