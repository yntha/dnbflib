from __future__ import annotations

import struct
from enum import IntEnum
from typing import Protocol


class RecordTypeEnumeration(IntEnum):
    """BinaryFormatter record type identifiers."""

    SerializedStreamHeader = 0
    ClassWithId = 1
    SystemClassWithMembers = 2
    ClassWithMembers = 3
    SystemClassWithMembersAndTypes = 4
    ClassWithMembersAndTypes = 5
    BinaryObjectString = 6
    BinaryArray = 7
    MemberPrimitiveTyped = 8
    MemberReference = 9
    ObjectNull = 10
    MessageEnd = 11
    BinaryLibrary = 12
    ObjectNullMultiple256 = 13
    ObjectNullMultiple = 14
    ArraySinglePrimitive = 15
    ArraySingleObject = 16
    ArraySingleString = 17
    MethodCall = 21
    MethodReturn = 22


class BinaryTypeEnumeration(IntEnum):
    """BinaryFormatter member and array item type identifiers."""

    Primitive = 0
    String = 1
    Object = 2
    SystemClass = 3
    Class = 4
    ObjectArray = 5
    StringArray = 6
    PrimitiveArray = 7


class PrimitiveTypeEnumeration(IntEnum):
    """BinaryFormatter primitive value type identifiers."""

    Boolean = 1
    Byte = 2
    Char = 3
    Reserved4 = 4
    Decimal = 5
    Double = 6
    Int16 = 7
    Int32 = 8
    Int64 = 9
    SByte = 10
    Single = 11
    TimeSpan = 12
    DateTime = 13
    UInt16 = 14
    UInt32 = 15
    UInt64 = 16
    Null = 17
    String = 18


PRIMITIVE_FORMATS: dict[PrimitiveTypeEnumeration, tuple[str, int]] = {
    PrimitiveTypeEnumeration.Boolean: ("?", 1),
    PrimitiveTypeEnumeration.Byte: ("B", 1),
    PrimitiveTypeEnumeration.Char: ("<H", 2),
    PrimitiveTypeEnumeration.Double: ("<d", 8),
    PrimitiveTypeEnumeration.Int16: ("<h", 2),
    PrimitiveTypeEnumeration.Int32: ("<i", 4),
    PrimitiveTypeEnumeration.Int64: ("<q", 8),
    PrimitiveTypeEnumeration.SByte: ("<b", 1),
    PrimitiveTypeEnumeration.Single: ("<f", 4),
    PrimitiveTypeEnumeration.UInt16: ("<H", 2),
    PrimitiveTypeEnumeration.UInt32: ("<I", 4),
    PrimitiveTypeEnumeration.UInt64: ("<Q", 8),
}


class Record(Protocol):
    """Minimal protocol for writable records."""

    record_type: RecordTypeEnumeration

    def to_bytes(self) -> bytes:
        """Return the serialized record bytes."""


def encode_7bit_int(value: int) -> bytes:
    """Encode a BinaryFormatter length prefix."""
    if not 0 <= value <= 0x7FFFFFFF:
        raise ValueError(f"length out of range: {value}")

    encoded = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            encoded.append(byte | 0x80)
        else:
            encoded.append(byte)
            return bytes(encoded)


def encode_length_prefixed_string(value: str) -> bytes:
    """Encode a BinaryFormatter UTF-8 length-prefixed string."""
    encoded = value.encode("utf-8")
    return encode_7bit_int(len(encoded)) + encoded


class BinaryObjectString:
    """Writable BinaryObjectString record."""

    record_type = RecordTypeEnumeration.BinaryObjectString

    def __init__(self, object_id: int, value: str) -> None:
        self.object_id = object_id
        self.value = value

    def to_bytes(self) -> bytes:
        return (
            struct.pack("B", RecordTypeEnumeration.BinaryObjectString.value)
            + struct.pack("<i", self.object_id)
            + encode_length_prefixed_string(self.value)
        )

    def __repr__(self) -> str:
        return f"BinaryObjectString(object_id={self.object_id}, value={self.value!r})"
