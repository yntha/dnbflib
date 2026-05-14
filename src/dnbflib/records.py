from __future__ import annotations

import struct
from collections.abc import Sequence
from dataclasses import dataclass
from enum import IntEnum
from typing import Any, Protocol


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


def encode_primitive_value(value: Any, primitive_type: PrimitiveTypeEnumeration) -> bytes:
    """Encode a supported primitive value without a record header."""
    if primitive_type == PrimitiveTypeEnumeration.Null:
        return b""
    if primitive_type in (PrimitiveTypeEnumeration.String, PrimitiveTypeEnumeration.Decimal):
        return encode_length_prefixed_string(str(value))
    if primitive_type in (PrimitiveTypeEnumeration.TimeSpan, PrimitiveTypeEnumeration.DateTime):
        return struct.pack("<q", int(value))
    if primitive_type not in PRIMITIVE_FORMATS:
        raise ValueError(f"unsupported primitive type: {primitive_type.name}")

    fmt, _ = PRIMITIVE_FORMATS[primitive_type]
    return struct.pack(fmt, value)


@dataclass(frozen=True)
class MemberReference:
    """Writable MemberReference record."""

    ref_id: int
    record_type: RecordTypeEnumeration = RecordTypeEnumeration.MemberReference

    def to_bytes(self) -> bytes:
        return struct.pack("<Bi", self.record_type.value, int(self.ref_id))


@dataclass(frozen=True)
class ObjectNull:
    """Writable ObjectNull record."""

    record_type: RecordTypeEnumeration = RecordTypeEnumeration.ObjectNull

    def to_bytes(self) -> bytes:
        return struct.pack("B", self.record_type.value)


@dataclass(frozen=True)
class ObjectNullMultiple256:
    """Writable ObjectNullMultiple256 record."""

    count: int
    record_type: RecordTypeEnumeration = RecordTypeEnumeration.ObjectNullMultiple256

    def to_bytes(self) -> bytes:
        if not 0 <= int(self.count) <= 255:
            raise ValueError("ObjectNullMultiple256 count must fit in one byte")
        return struct.pack("BB", self.record_type.value, int(self.count))


@dataclass(frozen=True)
class ObjectNullMultiple:
    """Writable ObjectNullMultiple record."""

    count: int
    record_type: RecordTypeEnumeration = RecordTypeEnumeration.ObjectNullMultiple

    def to_bytes(self) -> bytes:
        if not 0 <= int(self.count) <= 0x7FFFFFFF:
            raise ValueError("ObjectNullMultiple count must fit in Int32")
        return struct.pack("<Bi", self.record_type.value, int(self.count))


@dataclass(frozen=True)
class ClassWithId:
    """Writable ClassWithId record for a new instance of existing class metadata."""

    object_id: int
    metadata_id: int
    member_bytes: bytes
    record_type: RecordTypeEnumeration = RecordTypeEnumeration.ClassWithId

    def to_bytes(self) -> bytes:
        return (
            struct.pack("B", self.record_type.value)
            + struct.pack("<i", int(self.object_id))
            + struct.pack("<i", int(self.metadata_id))
            + self.member_bytes
        )


@dataclass(frozen=True)
class ArraySinglePrimitive:
    """Writable single-dimensional primitive array record."""

    object_id: int
    primitive_type: PrimitiveTypeEnumeration
    values: Sequence[Any]
    record_type: RecordTypeEnumeration = RecordTypeEnumeration.ArraySinglePrimitive

    def to_bytes(self) -> bytes:
        primitive_type = PrimitiveTypeEnumeration(self.primitive_type)
        result = bytearray()
        result += struct.pack("<BiiB", self.record_type.value, int(self.object_id), len(self.values), primitive_type)
        for value in self.values:
            result += encode_primitive_value(value, primitive_type)
        return bytes(result)


@dataclass(frozen=True)
class ArraySingleObject:
    """Writable single-dimensional object array record."""

    object_id: int
    items: Sequence[Record | bytes | bytearray]
    record_type: RecordTypeEnumeration = RecordTypeEnumeration.ArraySingleObject

    def to_bytes(self) -> bytes:
        return _array_single_record_to_bytes(self.record_type, self.object_id, self.items)


@dataclass(frozen=True)
class ArraySingleString:
    """Writable single-dimensional string array record."""

    object_id: int
    items: Sequence[Record | bytes | bytearray]
    record_type: RecordTypeEnumeration = RecordTypeEnumeration.ArraySingleString

    def to_bytes(self) -> bytes:
        return _array_single_record_to_bytes(self.record_type, self.object_id, self.items)


def _array_single_record_to_bytes(
    record_type: RecordTypeEnumeration,
    object_id: int,
    items: Sequence[Record | bytes | bytearray],
) -> bytes:
    result = bytearray()
    result += struct.pack("<Bii", record_type.value, int(object_id), len(items))
    for item in items:
        result += _record_item_to_bytes(item)
    return bytes(result)


def _record_item_to_bytes(item: Record | bytes | bytearray) -> bytes:
    if isinstance(item, bytes):
        return item
    if isinstance(item, bytearray):
        return bytes(item)
    to_bytes = getattr(item, "to_bytes", None)
    if callable(to_bytes):
        return bytes(to_bytes())
    raise TypeError(f"array item cannot be serialized: {item!r}")
