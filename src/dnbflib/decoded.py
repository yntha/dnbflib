from __future__ import annotations

from typing import Any

from dnbflib.indexer import StoredRecord
from dnbflib.yaml_export import _decode_supported_record, _encode_class_member_value


class DNBFDecodedError(Exception):
    """Raised when decoded-record operations fail."""


def decode_supported_record(
    stored_record: StoredRecord,
    decode_context: dict[int, dict[str, Any]],
) -> dict[str, Any] | None:
    """Decode a supported record into an editable manifest-like mapping."""
    return _decode_supported_record(stored_record, decode_context)


def encode_class_member_value(member: dict[str, Any]) -> bytes:
    """Encode an editable class member mapping back to record bytes."""
    return _encode_class_member_value(member)
