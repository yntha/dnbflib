from __future__ import annotations

import struct
import unittest
from pathlib import Path

from dnbflib import (
    AmbiguousObjectError,
    BinaryTypeEnumeration,
    DNBFDocument,
    PrimitiveTypeEnumeration,
    RecordTypeEnumeration,
)


class DocumentTraversalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_path = Path.cwd() / "_tmp_document_test.bin"
        if self.temp_path.exists():
            self.temp_path.unlink()

    def tearDown(self) -> None:
        if self.temp_path.exists():
            self.temp_path.unlink()

    def test_object_node_traversal_edits_referenced_object_member(self) -> None:
        finances = _primitive_class_record(
            object_id=5,
            class_name="Game.Finances",
            member_name="<BankBalance>k__BackingField",
            value=100,
        )
        life = _reference_class_record(
            object_id=1,
            class_name="Game.Life",
            member_name="<Finances>k__BackingField",
            ref_type_name="Game.Finances",
            ref_id=5,
        )
        source = finances + life + b"\x0b"
        self.temp_path.write_bytes(source)

        with DNBFDocument.open(self.temp_path) as doc:
            life_node = doc.find_class("Life")
            finances_node = life_node.member("Finances").deref()
            finances_node.member("BankBalance").set(123456)

            expected = (
                _primitive_class_record(
                    object_id=5,
                    class_name="Game.Finances",
                    member_name="<BankBalance>k__BackingField",
                    value=123456,
                )
                + life
                + b"\x0b"
            )
            self.assertEqual(doc.to_bytes(), expected)

    def test_find_class_raises_when_multiple_instances_match(self) -> None:
        first = _primitive_class_record(object_id=1, class_name="Game.Life", member_name="Age", value=20)
        second = _primitive_class_record(object_id=2, class_name="Game.Life", member_name="Age", value=40)
        self.temp_path.write_bytes(first + second + b"\x0b")

        with DNBFDocument.open(self.temp_path) as doc:
            with self.assertRaises(AmbiguousObjectError):
                doc.find_class("Life")

            selected = doc.one(class_name="Life", where=lambda node: node.member("Age").value == 40)
            self.assertEqual(selected.object_id, 2)

    def test_document_allocates_object_ids_after_existing_and_pending_objects(self) -> None:
        source = _primitive_class_record(object_id=5, class_name="Game.Finances", member_name="Age", value=20) + b"\x0b"
        self.temp_path.write_bytes(source)

        with DNBFDocument.open(self.temp_path) as doc:
            self.assertEqual(doc._next_object_id(), 6)
            doc._append_object_record(
                record_type=RecordTypeEnumeration.ClassWithId,
                raw=struct.pack("<Biii", RecordTypeEnumeration.ClassWithId, 6, 5, 30),
                object_id=6,
                metadata_id=5,
                decoded={"type": "ClassWithId", "fields": {"object_id": 6, "metadata_id": 5, "members": []}},
            )
            self.assertEqual(doc._next_object_id(), 7)

    def test_document_writes_inserted_object_records_before_message_end(self) -> None:
        source = _primitive_class_record(object_id=5, class_name="Game.Finances", member_name="Age", value=20) + b"\x0b"
        self.temp_path.write_bytes(source)
        inserted = struct.pack("<Biii", RecordTypeEnumeration.ClassWithId, 6, 5, 30)

        with DNBFDocument.open(self.temp_path) as doc:
            node = doc._append_object_record(
                record_type=RecordTypeEnumeration.ClassWithId,
                raw=inserted,
                object_id=6,
                metadata_id=5,
                decoded={"type": "ClassWithId", "fields": {"object_id": 6, "metadata_id": 5, "members": []}},
            )

            self.assertEqual(node.object_id, 6)
            self.assertEqual(doc.object(6).object_id, 6)
            self.assertEqual(doc.to_bytes(), source[:-1] + inserted + b"\x0b")


def _lp(value: str) -> bytes:
    encoded = value.encode("utf-8")
    if len(encoded) > 0x7F:
        raise ValueError("test helper only supports short strings")
    return bytes([len(encoded)]) + encoded


def _primitive_class_record(*, object_id: int, class_name: str, member_name: str, value: int) -> bytes:
    result = bytearray()
    result += struct.pack("B", RecordTypeEnumeration.ClassWithMembersAndTypes)
    result += struct.pack("<i", object_id)
    result += _lp(class_name)
    result += struct.pack("<i", 1)
    result += _lp(member_name)
    result += bytes([BinaryTypeEnumeration.Primitive])
    result += bytes([PrimitiveTypeEnumeration.Int32])
    result += struct.pack("<i", 2)
    result += struct.pack("<i", value)
    return bytes(result)


def _reference_class_record(
    *,
    object_id: int,
    class_name: str,
    member_name: str,
    ref_type_name: str,
    ref_id: int,
) -> bytes:
    result = bytearray()
    result += struct.pack("B", RecordTypeEnumeration.ClassWithMembersAndTypes)
    result += struct.pack("<i", object_id)
    result += _lp(class_name)
    result += struct.pack("<i", 1)
    result += _lp(member_name)
    result += bytes([BinaryTypeEnumeration.Class])
    result += _lp(ref_type_name)
    result += struct.pack("<i", 2)
    result += struct.pack("<i", 2)
    result += struct.pack("<Bi", RecordTypeEnumeration.MemberReference, ref_id)
    return bytes(result)


if __name__ == "__main__":
    unittest.main()
