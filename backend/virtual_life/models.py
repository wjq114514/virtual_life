"""Validated inputs for the versioned node protocol."""

import json
import re
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    field_validator,
    model_validator,
)

Identifier = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]
DisplayName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=256)]
Version = Annotated[int, Field(ge=0, le=2**63 - 1)]
JsonObject = dict[str, JsonValue]
InteractionMode = Literal["quiet", "chat", "focus", "away"]
MAX_PAYLOAD_BYTES = 65_536
MAX_PAYLOAD_DEPTH = 32


def bounded_json(value: JsonObject) -> JsonObject:
    """Bound both transport size and nesting without imposing host-specific keys."""
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    except (ValueError, TypeError) as exc:
        raise ValueError("Payload must contain finite JSON values") from exc
    if len(encoded.encode("utf-8")) > MAX_PAYLOAD_BYTES:
        raise ValueError(f"Payload must not exceed {MAX_PAYLOAD_BYTES} UTF-8 bytes")

    pending: list[tuple[JsonValue, int]] = [(value, 0)]
    while pending:
        item, depth = pending.pop()
        if depth > MAX_PAYLOAD_DEPTH:
            raise ValueError(f"Payload nesting must not exceed {MAX_PAYLOAD_DEPTH}")
        if isinstance(item, dict):
            pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            pending.extend((child, depth + 1) for child in item)
    return value


class RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class DeviceIdentity(RequestModel):
    device_id: Identifier | None = None
    mac_addresses: list[str] = Field(default_factory=list, max_length=16)

    @field_validator("mac_addresses")
    @classmethod
    def normalize_mac_addresses(cls, values: list[str]) -> list[str]:
        normalized = []
        for value in values:
            value = value.strip()
            if not re.fullmatch(
                r"(?:[0-9a-fA-F]{12}|(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}"
                r"|(?:[0-9a-fA-F]{2}-){5}[0-9a-fA-F]{2})",
                value,
            ):
                raise ValueError("MAC addresses must contain six hexadecimal octets")
            compact = re.sub(r"[:-]", "", value)
            if compact.lower() in ("000000000000", "ffffffffffff") or int(compact[:2], 16) & 1:
                raise ValueError("MAC addresses must identify a unicast interface")
            normalized.append(":".join(compact[i : i + 2].lower() for i in range(0, 12, 2)))
        return sorted(set(normalized))

    @model_validator(mode="after")
    def has_identity(self):
        if self.device_id is None and not self.mac_addresses:
            raise ValueError("Provide a stable device_id or a MAC address")
        return self


class NodeRegister(RequestModel):
    node_id: Identifier
    label: DisplayName
    kind: Literal["desktop", "android", "web", "other"]
    capabilities: list[Identifier] = Field(default_factory=list, max_length=64)
    device: DeviceIdentity | None = None

    @field_validator("capabilities")
    @classmethod
    def unique_capabilities(cls, value: list[str]) -> list[str]:
        return sorted(set(value))


class NodeHeartbeat(RequestModel):
    context: JsonObject = Field(default_factory=dict)

    @field_validator("context")
    @classmethod
    def validate_context(cls, value: JsonObject) -> JsonObject:
        return bounded_json(value)


class WebAccessRequest(RequestModel):
    browser_id: Identifier
    access_id: Identifier
    label: DisplayName = "本机设备"
    kind: Literal["desktop", "android", "other"] = "other"
    bind_node_id: Identifier | None = None


class WebAccessHeartbeat(NodeHeartbeat):
    pass


class CompanionCreate(RequestModel):
    companion_id: Identifier
    name: DisplayName


class ContextUpdate(RequestModel):
    command_id: Identifier
    node_id: Identifier
    epoch: Version
    expected_revision: Version
    activity: JsonObject | None = None
    interaction_mode: InteractionMode | None = None
    context: JsonObject | None = None
    memory_refs: list[Identifier] | None = Field(default=None, max_length=256)

    @field_validator("activity", "interaction_mode", "context", "memory_refs", mode="before")
    @classmethod
    def reject_explicit_null(cls, value):
        if value is None:
            raise ValueError("Omit an optional update field instead of setting it to null")
        return value

    @field_validator("activity", "context")
    @classmethod
    def validate_payload(cls, value: JsonObject | None) -> JsonObject | None:
        return bounded_json(value) if value is not None else None

    @field_validator("memory_refs")
    @classmethod
    def unique_memory_refs(cls, value: list[str] | None) -> list[str] | None:
        return list(dict.fromkeys(value)) if value is not None else None


class TransferPrepare(RequestModel):
    command_id: Identifier
    target_node_id: Identifier
    expected_revision: Version
    reason: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=512)
    ] = "manual"


class TransferAck(RequestModel):
    node_id: Identifier
    phase: Literal["ready", "released"]
    presentation_id: Identifier | None = None

    @model_validator(mode="after")
    def presentation_only_on_ready(self):
        # A native target binds its live presentation when it reports ready. The
        # source only reports released; presenting an id there is meaningless.
        if self.presentation_id is not None and self.phase != "ready":
            raise ValueError("presentation_id is only valid with phase=ready")
        return self


class LeaseRenew(RequestModel):
    node_id: Identifier
    epoch: Version


class ObservationCreate(RequestModel):
    command_id: Identifier
    modality: Identifier
    data: JsonObject = Field(default_factory=dict)
    user_initiated: bool = False

    @field_validator("data")
    @classmethod
    def validate_data(cls, value: JsonObject) -> JsonObject:
        return bounded_json(value)


class RoomFollow(RequestModel):
    """Authoritative single-party room migration.

    Unlike a prepared transfer (a two-party handshake for native renderers),
    follow_room is the "the lifeform flows to whichever room had the latest
    activity" path: the runtime moves the companion and releases the old lease
    in one commit, with no target acknowledgment required.
    """

    command_id: Identifier
    target_node_id: Identifier
    expected_revision: Version
    reason: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=512)
    ] = "room_activity"
