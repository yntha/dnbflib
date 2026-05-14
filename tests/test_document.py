from __future__ import annotations

import struct
import unittest
from pathlib import Path

from dnbflib import (
    AmbiguousObjectError,
    BinaryObjectString,
    BinaryTypeEnumeration,
    DNBFArrayNode,
    DNBFDocument,
    MemberNotFoundError,
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

    def test_object_node_creates_new_instance_from_template(self) -> None:
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
            new_finances = life_node.member("Finances").deref().new_instance({"BankBalance": 999999})
            life_node.member("Finances").set(new_finances)

            expected = (
                finances
                + _reference_class_record(
                    object_id=1,
                    class_name="Game.Life",
                    member_name="<Finances>k__BackingField",
                    ref_type_name="Game.Finances",
                    ref_id=6,
                )
                + struct.pack("<Biii", RecordTypeEnumeration.ClassWithId, 6, 5, 999999)
                + b"\x0b"
            )
            self.assertEqual(new_finances.object_id, 6)
            self.assertEqual(new_finances.record_type, "ClassWithId")
            self.assertEqual(new_finances.member("BankBalance").value, 999999)
            self.assertEqual(doc.to_bytes(), expected)

    def test_object_node_creates_new_instance_with_string_member(self) -> None:
        source = _string_class_record(object_id=5, class_name="Game.Person", member_name="Name", value="Alex") + b"\x0b"
        self.temp_path.write_bytes(source)

        with DNBFDocument.open(self.temp_path) as doc:
            person = doc.find_class("Person")
            new_person = person.new_instance(Name="Sam")

            expected_insert = (
                struct.pack("<Bii", RecordTypeEnumeration.ClassWithId, 9, 5)
                + struct.pack("<Bi", RecordTypeEnumeration.BinaryObjectString, 10)
                + _lp("Sam")
            )
            self.assertEqual(new_person.object_id, 9)
            self.assertEqual(new_person.member("Name").value, "Sam")
            self.assertEqual(doc.to_bytes(), source[:-1] + expected_insert + b"\x0b")

    def test_new_instance_rejects_unknown_member_values(self) -> None:
        source = _primitive_class_record(object_id=5, class_name="Game.Finances", member_name="Age", value=20) + b"\x0b"
        self.temp_path.write_bytes(source)

        with DNBFDocument.open(self.temp_path) as doc:
            with self.assertRaises(MemberNotFoundError):
                doc.find_class("Finances").new_instance({"Missing": 1})

    def test_member_reference_can_deref_array_node(self) -> None:
        inventory = _array_single_primitive_record(9, PrimitiveTypeEnumeration.Int32, [10, 20, 30])
        life = _reference_class_record(
            object_id=1,
            class_name="Game.Life",
            member_name="<Inventory>k__BackingField",
            ref_type_name="System.Int32[]",
            ref_id=9,
        )
        self.temp_path.write_bytes(inventory + life + b"\x0b")

        with DNBFDocument.open(self.temp_path) as doc:
            inventory_node = doc.find_class("Life").member("Inventory").deref()

            self.assertIsInstance(inventory_node, DNBFArrayNode)
            self.assertEqual(inventory_node.object_id, 9)
            self.assertEqual(inventory_node.record_type, "ArraySinglePrimitive")
            self.assertEqual(len(inventory_node), 3)
            self.assertEqual(inventory_node[1], 20)
            self.assertEqual(inventory_node.to_list(), [10, 20, 30])
            self.assertEqual([node.object_id for node in doc.objects()], [1])

    def test_array_node_items_decode_strings_references_and_nulls(self) -> None:
        item = _primitive_class_record(object_id=5, class_name="Game.Item", member_name="Value", value=42)
        inventory = (
            struct.pack("<Bii", RecordTypeEnumeration.ArraySingleString, 9, 3)
            + BinaryObjectString(10, "red").to_bytes()
            + struct.pack("<Bi", RecordTypeEnumeration.MemberReference, 5)
            + struct.pack("B", RecordTypeEnumeration.ObjectNull)
        )
        life = _reference_class_record(
            object_id=1,
            class_name="Game.Life",
            member_name="<Inventory>k__BackingField",
            ref_type_name="System.String[]",
            ref_id=9,
        )
        self.temp_path.write_bytes(item + inventory + life + b"\x0b")

        with DNBFDocument.open(self.temp_path) as doc:
            inventory_node = doc.find_class("Life").member("Inventory").deref()
            items = inventory_node.to_list()

            self.assertEqual(items[0], "red")
            self.assertEqual(items[1].object_id, 5)
            self.assertIsNone(items[2])


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


def _string_class_record(*, object_id: int, class_name: str, member_name: str, value: str) -> bytes:
    result = bytearray()
    result += struct.pack("B", RecordTypeEnumeration.ClassWithMembersAndTypes)
    result += struct.pack("<i", object_id)
    result += _lp(class_name)
    result += struct.pack("<i", 1)
    result += _lp(member_name)
    result += bytes([BinaryTypeEnumeration.String])
    result += struct.pack("<i", 2)
    result += struct.pack("<Bi", RecordTypeEnumeration.BinaryObjectString, 8)
    result += _lp(value)
    return bytes(result)


def _array_single_primitive_record(
    object_id: int,
    primitive_type: PrimitiveTypeEnumeration,
    values: list[int],
) -> bytes:
    result = bytearray()
    result += struct.pack("<BiiB", RecordTypeEnumeration.ArraySinglePrimitive, object_id, len(values), primitive_type)
    for value in values:
        result += struct.pack("<i", value)
    return bytes(result)


if __name__ == "__main__":
    unittest.main()
