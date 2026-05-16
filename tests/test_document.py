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
    DNBFDocumentError,
    MemberNotFoundError,
    ObjectNotFoundError,
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
            finances_node = life_node.member("Finances").deref_object()
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
            new_finances = life_node.member("Finances").deref_object().new_instance({"BankBalance": 999999})
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
            inventory_node = doc.find_class("Life").member("Inventory").deref_array()

            self.assertIsInstance(inventory_node, DNBFArrayNode)
            self.assertEqual(inventory_node.object_id, 9)
            self.assertEqual(inventory_node.record_type, "ArraySinglePrimitive")
            self.assertEqual(len(inventory_node), 3)
            self.assertEqual(inventory_node[1], 20)
            self.assertEqual(inventory_node.to_list(), [10, 20, 30])
            self.assertEqual([node.object_id for node in doc.objects()], [1])

    def test_open_lazy_scans_only_until_requested_object(self) -> None:
        inventory = _array_single_primitive_record(9, PrimitiveTypeEnumeration.Int32, [10, 20, 30])
        life = _reference_class_record(
            object_id=1,
            class_name="Game.Life",
            member_name="<Inventory>k__BackingField",
            ref_type_name="System.Int32[]",
            ref_id=9,
        )
        self.temp_path.write_bytes(inventory + life + b"\x0b")

        with DNBFDocument.open_lazy(self.temp_path) as doc:
            self.assertEqual(doc._records, [])

            inventory_node = doc.object(9)

            self.assertIsInstance(inventory_node, DNBFArrayNode)
            self.assertEqual([record.object_id for record in doc._records], [9])
            self.assertEqual(inventory_node.to_list(), [10, 20, 30])

    def test_open_lazy_scans_forward_when_reference_is_needed(self) -> None:
        inventory = (
            struct.pack("<Bii", RecordTypeEnumeration.ArraySingleObject, 9, 1)
            + struct.pack("<Bi", RecordTypeEnumeration.MemberReference, 5)
        )
        item = _primitive_class_record(object_id=5, class_name="Game.Item", member_name="Value", value=42)
        self.temp_path.write_bytes(inventory + item + b"\x0b")

        with DNBFDocument.open_lazy(self.temp_path) as doc:
            inventory_node = doc.object(9)
            self.assertEqual([record.object_id for record in doc._records], [9])

            item_node = inventory_node[0]

            self.assertEqual(item_node.object_id, 5)
            self.assertEqual(item_node.member("Value").value, 42)
            self.assertEqual([record.object_id for record in doc._records], [9, 5])

    def test_member_reference_requires_matching_deref_kind(self) -> None:
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
            inventory_member = doc.find_class("Life").member("Inventory")

            self.assertEqual(inventory_member.deref_array().object_id, 9)
            with self.assertRaises(ObjectNotFoundError):
                inventory_member.deref_object()

    def test_get_first_returns_first_matching_object_without_full_scan(self) -> None:
        first_life = _primitive_class_record(object_id=1, class_name="Game.Life", member_name="Age", value=20)
        second_life = _primitive_class_record(object_id=2, class_name="Game.Life", member_name="Age", value=40)
        item = _primitive_class_record(object_id=3, class_name="Game.Item", member_name="Value", value=99)
        self.temp_path.write_bytes(first_life + second_life + item + b"\x0b")

        with DNBFDocument.open_lazy(self.temp_path) as doc:
            life = doc.get_first(class_name="Life")

            self.assertEqual(life.object_id, 1)
            self.assertEqual(life.member("Age").value, 20)
            self.assertEqual([record.object_id for record in doc._records], [1])

    def test_get_first_raises_when_no_object_matches(self) -> None:
        item = _primitive_class_record(object_id=3, class_name="Game.Item", member_name="Value", value=99)
        self.temp_path.write_bytes(item + b"\x0b")

        with DNBFDocument.open_lazy(self.temp_path) as doc:
            with self.assertRaises(ObjectNotFoundError):
                doc.get_first(class_name="Life")

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
            inventory_node = doc.find_class("Life").member("Inventory").deref_array()
            items = inventory_node.to_list()

            self.assertEqual(items[0], "red")
            self.assertEqual(items[1].object_id, 5)
            self.assertIsNone(items[2])

    def test_array_node_edits_primitive_items_in_place(self) -> None:
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
            inventory_node = doc.find_class("Life").member("Inventory").deref_array()
            inventory_node[1] = 99

            expected = _array_single_primitive_record(9, PrimitiveTypeEnumeration.Int32, [10, 99, 30]) + life + b"\x0b"
            self.assertEqual(inventory_node.to_list(), [10, 99, 30])
            self.assertEqual(doc.to_bytes(), expected)

    def test_array_node_edits_record_items_in_place(self) -> None:
        first_item = _primitive_class_record(object_id=5, class_name="Game.Item", member_name="Value", value=42)
        second_item = _primitive_class_record(object_id=6, class_name="Game.Item", member_name="Value", value=84)
        inventory = (
            struct.pack("<Bii", RecordTypeEnumeration.ArraySingleObject, 9, 3)
            + BinaryObjectString(10, "red").to_bytes()
            + struct.pack("<Bi", RecordTypeEnumeration.MemberReference, 5)
            + struct.pack("B", RecordTypeEnumeration.ObjectNull)
        )
        life = _reference_class_record(
            object_id=1,
            class_name="Game.Life",
            member_name="<Inventory>k__BackingField",
            ref_type_name="System.Object[]",
            ref_id=9,
        )
        self.temp_path.write_bytes(first_item + second_item + inventory + life + b"\x0b")

        with DNBFDocument.open(self.temp_path) as doc:
            inventory_node = doc.find_class("Life").member("Inventory").deref_array()
            inventory_node[0] = "blue"
            inventory_node[1] = doc.object(6)
            inventory_node[2] = "green"

            expected_inventory = (
                struct.pack("<Bii", RecordTypeEnumeration.ArraySingleObject, 9, 3)
                + BinaryObjectString(10, "blue").to_bytes()
                + struct.pack("<Bi", RecordTypeEnumeration.MemberReference, 6)
                + BinaryObjectString(11, "green").to_bytes()
            )
            self.assertEqual(inventory_node[0], "blue")
            self.assertEqual(inventory_node[1].object_id, 6)
            self.assertEqual(inventory_node[2], "green")
            self.assertEqual(doc.to_bytes(), first_item + second_item + expected_inventory + life + b"\x0b")

    def test_reference_member_can_be_set_to_array_node(self) -> None:
        first_inventory = _array_single_primitive_record(9, PrimitiveTypeEnumeration.Int32, [10])
        second_inventory = _array_single_primitive_record(10, PrimitiveTypeEnumeration.Int32, [20])
        life = _reference_class_record(
            object_id=1,
            class_name="Game.Life",
            member_name="<Inventory>k__BackingField",
            ref_type_name="System.Int32[]",
            ref_id=9,
        )
        self.temp_path.write_bytes(first_inventory + second_inventory + life + b"\x0b")

        with DNBFDocument.open(self.temp_path) as doc:
            life_node = doc.find_class("Life")
            life_node.member("Inventory").set(doc.object(10))

            expected_life = _reference_class_record(
                object_id=1,
                class_name="Game.Life",
                member_name="<Inventory>k__BackingField",
                ref_type_name="System.Int32[]",
                ref_id=10,
            )
            self.assertEqual(doc.to_bytes(), first_inventory + second_inventory + expected_life + b"\x0b")

    def test_array_node_mutates_primitive_array_length(self) -> None:
        inventory = _array_single_primitive_record(9, PrimitiveTypeEnumeration.Int32, [10, 20, 30])
        self.temp_path.write_bytes(inventory + b"\x0b")

        with DNBFDocument.open(self.temp_path) as doc:
            inventory_node = doc.object(9)
            inventory_node.append(40)
            inventory_node.insert(1, 15)
            del inventory_node[3]
            inventory_node.resize(6, fill=0)
            inventory_node.resize(4)

            expected = _array_single_primitive_record(9, PrimitiveTypeEnumeration.Int32, [10, 15, 20, 40]) + b"\x0b"
            self.assertEqual(inventory_node.to_list(), [10, 15, 20, 40])
            self.assertEqual(doc.to_bytes(), expected)

    def test_array_node_requires_fill_when_growing_primitive_array(self) -> None:
        inventory = _array_single_primitive_record(9, PrimitiveTypeEnumeration.Int32, [10])
        self.temp_path.write_bytes(inventory + b"\x0b")

        with DNBFDocument.open(self.temp_path) as doc:
            inventory_node = doc.object(9)
            with self.assertRaises(DNBFDocumentError):
                inventory_node.resize(2)

    def test_array_node_mutates_record_array_length(self) -> None:
        first_item = _primitive_class_record(object_id=5, class_name="Game.Item", member_name="Value", value=42)
        second_item = _primitive_class_record(object_id=6, class_name="Game.Item", member_name="Value", value=84)
        inventory = (
            struct.pack("<Bii", RecordTypeEnumeration.ArraySingleObject, 9, 2)
            + struct.pack("<Bi", RecordTypeEnumeration.MemberReference, 5)
            + struct.pack("B", RecordTypeEnumeration.ObjectNull)
        )
        self.temp_path.write_bytes(first_item + second_item + inventory + b"\x0b")

        with DNBFDocument.open(self.temp_path) as doc:
            inventory_node = doc.object(9)
            inventory_node.append("green")
            inventory_node.insert(1, doc.object(6))
            del inventory_node[2]
            inventory_node.resize(5, fill=None)
            inventory_node.resize(3)

            expected_inventory = (
                struct.pack("<Bii", RecordTypeEnumeration.ArraySingleObject, 9, 3)
                + struct.pack("<Bi", RecordTypeEnumeration.MemberReference, 5)
                + struct.pack("<Bi", RecordTypeEnumeration.MemberReference, 6)
                + BinaryObjectString(10, "green").to_bytes()
            )
            self.assertEqual(inventory_node[0].object_id, 5)
            self.assertEqual(inventory_node[1].object_id, 6)
            self.assertEqual(inventory_node[2], "green")
            self.assertEqual(doc.to_bytes(), first_item + second_item + expected_inventory + b"\x0b")

    def test_binary_array_rejects_length_mutation(self) -> None:
        binary_array = _binary_array_primitive_record(9, PrimitiveTypeEnumeration.Int32, [1, 2])
        self.temp_path.write_bytes(binary_array + b"\x0b")

        with DNBFDocument.open(self.temp_path) as doc:
            array_node = doc.object(9)
            with self.assertRaises(DNBFDocumentError):
                array_node.append(3)
            with self.assertRaises(DNBFDocumentError):
                array_node.insert(0, 0)
            with self.assertRaises(DNBFDocumentError):
                del array_node[0]
            with self.assertRaises(DNBFDocumentError):
                array_node.resize(3, fill=0)

    def test_document_creates_primitive_array(self) -> None:
        self.temp_path.write_bytes(b"\x0b")

        with DNBFDocument.open(self.temp_path) as doc:
            array_node = doc.new_primitive_array([10, 20, 30], item_type=PrimitiveTypeEnumeration.Int32)

            expected = _array_single_primitive_record(1, PrimitiveTypeEnumeration.Int32, [10, 20, 30]) + b"\x0b"
            self.assertEqual(array_node.object_id, 1)
            self.assertEqual(array_node.record_type, "ArraySinglePrimitive")
            self.assertEqual(array_node.to_list(), [10, 20, 30])
            self.assertEqual(doc.to_bytes(), expected)

    def test_document_creates_string_array(self) -> None:
        self.temp_path.write_bytes(b"\x0b")

        with DNBFDocument.open(self.temp_path) as doc:
            array_node = doc.new_string_array(["red", None, "blue"])

            expected = (
                struct.pack("<Bii", RecordTypeEnumeration.ArraySingleString, 1, 3)
                + BinaryObjectString(2, "red").to_bytes()
                + struct.pack("B", RecordTypeEnumeration.ObjectNull)
                + BinaryObjectString(3, "blue").to_bytes()
                + b"\x0b"
            )
            self.assertEqual(array_node.object_id, 1)
            self.assertEqual(array_node.to_list(), ["red", None, "blue"])
            self.assertEqual(doc.to_bytes(), expected)

    def test_document_creates_object_array(self) -> None:
        item = _primitive_class_record(object_id=5, class_name="Game.Item", member_name="Value", value=42)
        self.temp_path.write_bytes(item + b"\x0b")

        with DNBFDocument.open(self.temp_path) as doc:
            array_node = doc.new_object_array([doc.object(5), None])

            expected_array = (
                struct.pack("<Bii", RecordTypeEnumeration.ArraySingleObject, 6, 2)
                + struct.pack("<Bi", RecordTypeEnumeration.MemberReference, 5)
                + struct.pack("B", RecordTypeEnumeration.ObjectNull)
            )
            self.assertEqual(array_node.object_id, 6)
            self.assertEqual(array_node[0].object_id, 5)
            self.assertIsNone(array_node[1])
            self.assertEqual(doc.to_bytes(), item + expected_array + b"\x0b")

    def test_document_assigns_new_array_to_reference_field(self) -> None:
        old_scores = _array_single_primitive_record(9, PrimitiveTypeEnumeration.Int32, [1])
        life = _reference_class_record(
            object_id=1,
            class_name="Game.Life",
            member_name="<Scores>k__BackingField",
            ref_type_name="System.Int32[]",
            ref_id=9,
        )
        self.temp_path.write_bytes(old_scores + life + b"\x0b")

        with DNBFDocument.open(self.temp_path) as doc:
            life_node = doc.find_class("Life")
            new_scores = doc.new_primitive_array([10, 20], item_type=PrimitiveTypeEnumeration.Int32)
            life_node.member("Scores").set(new_scores)

            expected_life = _reference_class_record(
                object_id=1,
                class_name="Game.Life",
                member_name="<Scores>k__BackingField",
                ref_type_name="System.Int32[]",
                ref_id=10,
            )
            expected_scores = _array_single_primitive_record(10, PrimitiveTypeEnumeration.Int32, [10, 20])
            self.assertEqual(doc.to_bytes(), old_scores + expected_life + expected_scores + b"\x0b")

    def test_document_creates_empty_object_array(self) -> None:
        self.temp_path.write_bytes(b"\x0b")

        with DNBFDocument.open(self.temp_path) as doc:
            array_node = doc.new_object_array([])
            expected = struct.pack("<Bii", RecordTypeEnumeration.ArraySingleObject, 1, 0) + b"\x0b"
            self.assertEqual(array_node.to_list(), [])
            self.assertEqual(doc.to_bytes(), expected)

    def test_document_rejects_unsupported_object_array_item(self) -> None:
        self.temp_path.write_bytes(b"\x0b")

        with DNBFDocument.open(self.temp_path) as doc:
            with self.assertRaises(DNBFDocumentError):
                doc.new_object_array([object()])

    def test_reference_member_exposes_declared_item_type(self) -> None:
        inventory = _array_single_object_record(object_id=9, items=[None])
        life = _reference_class_record(
            object_id=1,
            class_name="Game.Life",
            member_name="<Inventory>k__BackingField",
            ref_type_name="Game.Item[]",
            ref_id=9,
        )
        self.temp_path.write_bytes(inventory + life + b"\x0b")

        with DNBFDocument.open(self.temp_path) as doc:
            inventory_member = doc.find_class("Life").member("Inventory")

            self.assertEqual(inventory_member.declared_type, "Game.Item[]")
            self.assertEqual(inventory_member.item_type, "Game.Item")

    def test_reference_member_gets_item_template(self) -> None:
        item_template = _primitive_class_record(object_id=5, class_name="Game.Item", member_name="Value", value=42)
        inventory = _array_single_object_record(object_id=9, items=[None])
        life = _reference_class_record(
            object_id=1,
            class_name="Game.Life",
            member_name="<Inventory>k__BackingField",
            ref_type_name="Game.Item[]",
            ref_id=9,
        )
        self.temp_path.write_bytes(item_template + inventory + life + b"\x0b")

        with DNBFDocument.open(self.temp_path) as doc:
            inventory_member = doc.find_class("Life").member("Inventory")
            template = inventory_member.get_item_template()
            new_item = template.new_instance({"Value": 99})
            new_inventory = doc.new_object_array([new_item])
            inventory_member.set(new_inventory)

            expected_life = _reference_class_record(
                object_id=1,
                class_name="Game.Life",
                member_name="<Inventory>k__BackingField",
                ref_type_name="Game.Item[]",
                ref_id=11,
            )
            expected_new_item = struct.pack("<Biii", RecordTypeEnumeration.ClassWithId, 10, 5, 99)
            expected_inventory = (
                struct.pack("<Bii", RecordTypeEnumeration.ArraySingleObject, 11, 1)
                + struct.pack("<Bi", RecordTypeEnumeration.MemberReference, 10)
            )
            expected = item_template + inventory + expected_life + expected_new_item + expected_inventory + b"\x0b"
            self.assertEqual(doc.to_bytes(), expected)


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


def _array_single_object_record(object_id: int, items: list[object | None]) -> bytes:
    result = bytearray()
    result += struct.pack("<Bii", RecordTypeEnumeration.ArraySingleObject, object_id, len(items))
    for item in items:
        if item is None:
            result += struct.pack("B", RecordTypeEnumeration.ObjectNull)
        else:
            raise ValueError("test helper only supports null object array items")
    return bytes(result)


def _binary_array_primitive_record(
    object_id: int,
    primitive_type: PrimitiveTypeEnumeration,
    values: list[int],
) -> bytes:
    result = bytearray()
    result += struct.pack("<BiBi", RecordTypeEnumeration.BinaryArray, object_id, 0, 1)
    result += struct.pack("<i", len(values))
    result += struct.pack("BB", BinaryTypeEnumeration.Primitive, primitive_type)
    for value in values:
        result += struct.pack("<i", value)
    return bytes(result)


if __name__ == "__main__":
    unittest.main()
