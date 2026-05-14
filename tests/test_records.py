from __future__ import annotations

import struct
import unittest

from dnbflib.records import (
    ArraySingleObject,
    ArraySinglePrimitive,
    ArraySingleString,
    BinaryObjectString,
    ClassWithId,
    MemberReference,
    ObjectNull,
    ObjectNullMultiple,
    ObjectNullMultiple256,
    PrimitiveTypeEnumeration,
    RecordTypeEnumeration,
    encode_primitive_value,
)


class RecordWriterTests(unittest.TestCase):
    def test_member_reference_writes_record_type_and_reference_id(self) -> None:
        self.assertEqual(
            MemberReference(42).to_bytes(),
            struct.pack("<Bi", RecordTypeEnumeration.MemberReference, 42),
        )

    def test_null_markers_write_expected_bytes(self) -> None:
        self.assertEqual(ObjectNull().to_bytes(), b"\x0a")
        self.assertEqual(ObjectNullMultiple256(3).to_bytes(), b"\x0d\x03")
        self.assertEqual(
            ObjectNullMultiple(300).to_bytes(),
            struct.pack("<Bi", RecordTypeEnumeration.ObjectNullMultiple, 300),
        )

    def test_class_with_id_writes_header_and_member_payload(self) -> None:
        self.assertEqual(
            ClassWithId(object_id=100, metadata_id=5, member_bytes=b"\x01\x02").to_bytes(),
            struct.pack("<Bii", RecordTypeEnumeration.ClassWithId, 100, 5) + b"\x01\x02",
        )

    def test_encode_primitive_value(self) -> None:
        self.assertEqual(
            encode_primitive_value(1234, PrimitiveTypeEnumeration.Int32),
            struct.pack("<i", 1234),
        )
        self.assertEqual(encode_primitive_value("abc", PrimitiveTypeEnumeration.String), b"\x03abc")

    def test_array_single_primitive_writes_header_and_values(self) -> None:
        self.assertEqual(
            ArraySinglePrimitive(55, PrimitiveTypeEnumeration.Int16, [1, 2, 3]).to_bytes(),
            struct.pack(
                "<BiiBhhh",
                RecordTypeEnumeration.ArraySinglePrimitive,
                55,
                3,
                PrimitiveTypeEnumeration.Int16,
                1,
                2,
                3,
            ),
        )

    def test_array_single_string_writes_record_items(self) -> None:
        self.assertEqual(
            ArraySingleString(
                77,
                [
                    BinaryObjectString(10, "red"),
                    MemberReference(11),
                    ObjectNull(),
                ],
            ).to_bytes(),
            (
                struct.pack("<Bii", RecordTypeEnumeration.ArraySingleString, 77, 3)
                + BinaryObjectString(10, "red").to_bytes()
                + MemberReference(11).to_bytes()
                + ObjectNull().to_bytes()
            ),
        )

    def test_array_single_object_accepts_raw_record_items(self) -> None:
        self.assertEqual(
            ArraySingleObject(88, [MemberReference(20), b"\x0a"]).to_bytes(),
            struct.pack("<Bii", RecordTypeEnumeration.ArraySingleObject, 88, 2)
            + MemberReference(20).to_bytes()
            + b"\x0a",
        )


if __name__ == "__main__":
    unittest.main()
