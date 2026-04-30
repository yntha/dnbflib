from __future__ import annotations

import json
import shutil
import struct
import unittest
from pathlib import Path

from dnbflib import (
    BinaryObjectString,
    BinaryTypeEnumeration,
    DNBFRecordStore,
    PrimitiveTypeEnumeration,
    RecordTypeEnumeration,
    export_dnbf_to_yaml,
    export_record_store_to_yaml,
    rebuild_yaml_export,
)


class YamlExportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_parent = Path.cwd() / "_tmp_yaml_export_tests"
        self.export_root = self.temp_parent / self._testMethodName
        if self.export_root.exists():
            shutil.rmtree(self.export_root)
        self.export_root.mkdir(parents=True)

    def tearDown(self) -> None:
        if self.export_root.exists():
            shutil.rmtree(self.export_root)
        if self.temp_parent.exists() and not any(self.temp_parent.iterdir()):
            self.temp_parent.rmdir()

    def test_export_rebuilds_losslessly_from_raw_sidecars(self) -> None:
        source = b"\x00header\x0b"
        store = DNBFRecordStore(":memory:", source_bytes=source)
        store.add_raw_record(record_type=RecordTypeEnumeration.SerializedStreamHeader, offset=0, raw=b"\x00header")
        store.add_raw_record(record_type=RecordTypeEnumeration.MessageEnd, offset=7, raw=b"\x0b")

        export_record_store_to_yaml(store, self.export_root)

        self.assertEqual(rebuild_yaml_export(self.export_root), source)
        self.assertTrue((self.export_root / "manifest.yaml").exists())
        self.assertTrue(
            (self.export_root / "records" / "control" / "000001_SerializedStreamHeader" / "record.yaml").exists()
        )
        self.assertTrue(
            (self.export_root / "records" / "control" / "000001_SerializedStreamHeader" / "raw.bin").exists()
        )

    def test_supported_decoded_string_record_can_be_edited(self) -> None:
        original = BinaryObjectString(123, "old value").to_bytes() + b"\x0b"
        replacement = BinaryObjectString(123, "new value").to_bytes() + b"\x0b"
        store = DNBFRecordStore(":memory:", source_bytes=original)
        store.add_raw_record(
            record_type=RecordTypeEnumeration.BinaryObjectString,
            offset=0,
            raw=BinaryObjectString(123, "old value").to_bytes(),
            object_id=123,
        )
        store.add_raw_record(record_type=RecordTypeEnumeration.MessageEnd, offset=len(original) - 1, raw=b"\x0b")

        manifest_path = export_record_store_to_yaml(store, self.export_root)
        record = _load_record_file(manifest_path, 0)
        record["rebuild"] = "decoded"
        record["decoded"]["fields"]["value"] = "new value"
        _write_record_file(manifest_path, 0, record)

        self.assertEqual(rebuild_yaml_export(self.export_root), replacement)

    def test_binary_export_falls_back_to_lossless_raw_stream_when_parser_is_unavailable(self) -> None:
        source_path = self.export_root / "sample.bin"
        source = b"arbitrary binaryformatter bytes"
        source_path.write_bytes(source)
        package_path = self.export_root / "package"

        manifest_path = export_dnbf_to_yaml(source_path, package_path)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        self.assertEqual(manifest["records"][0]["record_type"], "UnparsedStream")
        self.assertEqual(rebuild_yaml_export(package_path), source)

    def test_supported_decoded_member_primitive_typed_can_be_edited(self) -> None:
        original_record = struct.pack(
            "<BBi", RecordTypeEnumeration.MemberPrimitiveTyped, PrimitiveTypeEnumeration.Int32, 100
        )
        replacement_record = struct.pack(
            "<BBi", RecordTypeEnumeration.MemberPrimitiveTyped, PrimitiveTypeEnumeration.Int32, 250
        )
        source = original_record + b"\x0b"
        store = DNBFRecordStore(":memory:", source_bytes=source)
        store.add_raw_record(
            record_type=RecordTypeEnumeration.MemberPrimitiveTyped,
            offset=0,
            raw=original_record,
        )
        store.add_raw_record(record_type=RecordTypeEnumeration.MessageEnd, offset=len(original_record), raw=b"\x0b")

        manifest_path = export_record_store_to_yaml(store, self.export_root)
        record = _load_record_file(manifest_path, 0)
        record["rebuild"] = "decoded"
        record["decoded"]["fields"]["value"] = 250
        _write_record_file(manifest_path, 0, record)

        self.assertEqual(rebuild_yaml_export(self.export_root), replacement_record + b"\x0b")

    def test_special_primitive_values_can_be_edited(self) -> None:
        original_record = struct.pack(
            "<BBq", RecordTypeEnumeration.MemberPrimitiveTyped, PrimitiveTypeEnumeration.DateTime, 123456789
        )
        replacement_record = struct.pack(
            "<BBq", RecordTypeEnumeration.MemberPrimitiveTyped, PrimitiveTypeEnumeration.DateTime, 987654321
        )
        source = original_record + b"\x0b"
        store = DNBFRecordStore(":memory:", source_bytes=source)
        store.add_raw_record(record_type=RecordTypeEnumeration.MemberPrimitiveTyped, offset=0, raw=original_record)
        store.add_raw_record(record_type=RecordTypeEnumeration.MessageEnd, offset=len(original_record), raw=b"\x0b")

        manifest_path = export_record_store_to_yaml(store, self.export_root)
        record = _load_record_file(manifest_path, 0)
        record["rebuild"] = "decoded"
        record["decoded"]["fields"]["value"] = 987654321
        _write_record_file(manifest_path, 0, record)

        self.assertEqual(rebuild_yaml_export(self.export_root), replacement_record + b"\x0b")

    def test_supported_decoded_array_single_primitive_can_be_edited(self) -> None:
        original_record = (
            struct.pack("<BiiB", RecordTypeEnumeration.ArraySinglePrimitive, 55, 3, PrimitiveTypeEnumeration.Int16)
            + struct.pack("<hhh", 1, 2, 3)
        )
        replacement_record = (
            struct.pack("<BiiB", RecordTypeEnumeration.ArraySinglePrimitive, 55, 3, PrimitiveTypeEnumeration.Int16)
            + struct.pack("<hhh", 1, 9, 3)
        )
        source = original_record + b"\x0b"
        store = DNBFRecordStore(":memory:", source_bytes=source)
        store.add_raw_record(
            record_type=RecordTypeEnumeration.ArraySinglePrimitive,
            offset=0,
            raw=original_record,
            object_id=55,
        )
        store.add_raw_record(record_type=RecordTypeEnumeration.MessageEnd, offset=len(original_record), raw=b"\x0b")

        manifest_path = export_record_store_to_yaml(store, self.export_root)
        record = _load_record_file(manifest_path, 0)
        record["rebuild"] = "decoded"
        record["decoded"]["fields"]["values"][1] = 9
        _write_record_file(manifest_path, 0, record)

        self.assertEqual(rebuild_yaml_export(self.export_root), replacement_record + b"\x0b")

    def test_class_with_members_and_types_primitive_members_can_be_edited(self) -> None:
        original_record = _class_with_members_and_types_record(age=30, score=1.5)
        replacement_record = _class_with_members_and_types_record(age=31, score=2.5)
        source = original_record + b"\x0b"
        store = DNBFRecordStore(":memory:", source_bytes=source)
        store.add_raw_record(
            record_type=RecordTypeEnumeration.ClassWithMembersAndTypes,
            offset=0,
            raw=original_record,
            object_id=1,
            library_id=2,
        )
        store.add_raw_record(record_type=RecordTypeEnumeration.MessageEnd, offset=len(original_record), raw=b"\x0b")

        manifest_path = export_record_store_to_yaml(store, self.export_root)
        record = _load_record_file(manifest_path, 0)
        record["rebuild"] = "decoded"
        members = record["decoded"]["fields"]["members"]
        members[0]["value"] = 31
        members[2]["value"] = 2.5
        _write_record_file(manifest_path, 0, record)

        self.assertEqual(rebuild_yaml_export(self.export_root), replacement_record + b"\x0b")

    def test_class_member_sidecar_files_can_be_edited(self) -> None:
        original_record = _class_with_members_and_types_record(age=30, score=1.5)
        replacement_record = _class_with_members_and_types_record(age=32, score=1.5)
        source = original_record + b"\x0b"
        store = DNBFRecordStore(":memory:", source_bytes=source)
        store.add_raw_record(
            record_type=RecordTypeEnumeration.ClassWithMembersAndTypes,
            offset=0,
            raw=original_record,
            object_id=1,
            library_id=2,
        )
        store.add_raw_record(record_type=RecordTypeEnumeration.MessageEnd, offset=len(original_record), raw=b"\x0b")

        manifest_path = export_record_store_to_yaml(store, self.export_root)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        record_path = manifest_path.parent / manifest["records"][0]["path"]
        record = json.loads(record_path.read_text(encoding="utf-8"))
        member_file = record["decoded"]["fields"]["members"]["files"][0]
        member_path = manifest_path.parent / member_file["path"]
        member = json.loads(member_path.read_text(encoding="utf-8"))
        member["value"] = 32
        member_path.write_text(json.dumps(member, indent=2) + "\n", encoding="utf-8")
        record["rebuild"] = "decoded"
        record_path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")

        self.assertEqual(rebuild_yaml_export(self.export_root), replacement_record + b"\x0b")

    def test_member_reference_sidecar_includes_target_record_path(self) -> None:
        target_record = BinaryObjectString(5, "life log").to_bytes()
        owner_record = _class_with_reference_member_record(ref_id=5)
        source = target_record + owner_record + b"\x0b"
        store = DNBFRecordStore(":memory:", source_bytes=source)
        store.add_raw_record(
            record_type=RecordTypeEnumeration.BinaryObjectString,
            offset=0,
            raw=target_record,
            object_id=5,
        )
        store.add_raw_record(
            record_type=RecordTypeEnumeration.ClassWithMembersAndTypes,
            offset=len(target_record),
            raw=owner_record,
            object_id=1,
            library_id=2,
        )
        store.add_raw_record(
            record_type=RecordTypeEnumeration.MessageEnd,
            offset=len(target_record) + len(owner_record),
            raw=b"\x0b",
        )

        manifest_path = export_record_store_to_yaml(store, self.export_root)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        owner_record_path = manifest_path.parent / manifest["records"][1]["path"]
        owner = json.loads(owner_record_path.read_text(encoding="utf-8"))
        member_file = owner["decoded"]["fields"]["members"]["files"][0]
        member = json.loads((manifest_path.parent / member_file["path"]).read_text(encoding="utf-8"))

        self.assertEqual(member["ref_id"], 5)
        self.assertEqual(member["ref_path"], manifest["records"][0]["path"])

    def test_binary_export_scans_class_records_when_full_reader_is_unavailable(self) -> None:
        source = (
            struct.pack("<Biiii", RecordTypeEnumeration.SerializedStreamHeader, 1, -1, 1, 0)
            + _class_with_members_and_types_record(age=30, score=1.5)
            + b"\x0b"
        )
        source_path = self.export_root / "sample.bin"
        source_path.write_bytes(source)
        package_path = self.export_root / "package"

        manifest_path = export_dnbf_to_yaml(source_path, package_path)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        self.assertEqual([record["record_type"] for record in manifest["records"]],
                         ["SerializedStreamHeader", "ClassWithMembersAndTypes", "MessageEnd"])
        self.assertEqual(rebuild_yaml_export(package_path), source)

    def test_decoded_export_errors_do_not_block_lossless_raw_rebuild(self) -> None:
        binary_array_with_unknown_class_metadata = (
            struct.pack(
                "<BiBiiB",
                RecordTypeEnumeration.BinaryArray,
                10,
                0,
                1,
                1,
                BinaryTypeEnumeration.Object,
            )
            + struct.pack("<Bii", RecordTypeEnumeration.ClassWithId, 20, 999)
        )
        source = binary_array_with_unknown_class_metadata + b"\x0b"
        store = DNBFRecordStore(":memory:", source_bytes=source)
        store.add_raw_record(
            record_type=RecordTypeEnumeration.BinaryArray,
            offset=0,
            raw=binary_array_with_unknown_class_metadata,
            object_id=10,
        )
        store.add_raw_record(
            record_type=RecordTypeEnumeration.MessageEnd,
            offset=len(binary_array_with_unknown_class_metadata),
            raw=b"\x0b",
        )

        manifest_path = export_record_store_to_yaml(store, self.export_root)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        self.assertEqual(manifest["records"][0]["record_type"], "BinaryArray")
        self.assertIn("decoded_error", _load_record_file(manifest_path, 0))
        self.assertEqual(rebuild_yaml_export(self.export_root), source)

    def test_class_with_id_primitive_members_can_be_edited_using_prior_metadata(self) -> None:
        metadata_record = _class_with_members_and_types_record(age=30, score=1.5)
        instance_record = _class_with_id_record(object_id=2, metadata_id=1, age=40, score=3.5)
        replacement_instance = _class_with_id_record(object_id=2, metadata_id=1, age=41, score=4.5)
        source = metadata_record + instance_record + b"\x0b"
        store = DNBFRecordStore(":memory:", source_bytes=source)
        store.add_raw_record(
            record_type=RecordTypeEnumeration.ClassWithMembersAndTypes,
            offset=0,
            raw=metadata_record,
            object_id=1,
            library_id=2,
        )
        store.add_raw_record(
            record_type=RecordTypeEnumeration.ClassWithId,
            offset=len(metadata_record),
            raw=instance_record,
            object_id=2,
            metadata_id=1,
        )
        store.add_raw_record(
            record_type=RecordTypeEnumeration.MessageEnd,
            offset=len(metadata_record) + len(instance_record),
            raw=b"\x0b",
        )

        manifest_path = export_record_store_to_yaml(store, self.export_root)
        record = _load_record_file(manifest_path, 1)
        record["rebuild"] = "decoded"
        members = record["decoded"]["fields"]["members"]
        members[0]["value"] = 41
        members[2]["value"] = 4.5
        _write_record_file(manifest_path, 1, record)

        self.assertEqual(rebuild_yaml_export(self.export_root), metadata_record + replacement_instance + b"\x0b")

    def test_simple_records_can_be_rebuilt_from_decoded_fields(self) -> None:
        header = struct.pack("<Biiii", RecordTypeEnumeration.SerializedStreamHeader, 1, -1, 1, 0)
        library = struct.pack("<Bi", RecordTypeEnumeration.BinaryLibrary, 2) + _lp("Old.Assembly")
        reference = struct.pack("<Bi", RecordTypeEnumeration.MemberReference, 42)
        nulls = struct.pack("BB", RecordTypeEnumeration.ObjectNullMultiple256, 3)
        source = header + library + reference + nulls + b"\x0b"
        store = DNBFRecordStore(":memory:", source_bytes=source)
        offset = 0
        for record_type, raw in (
            (RecordTypeEnumeration.SerializedStreamHeader, header),
            (RecordTypeEnumeration.BinaryLibrary, library),
            (RecordTypeEnumeration.MemberReference, reference),
            (RecordTypeEnumeration.ObjectNullMultiple256, nulls),
            (RecordTypeEnumeration.MessageEnd, b"\x0b"),
        ):
            store.add_raw_record(record_type=record_type, offset=offset, raw=raw)
            offset += len(raw)

        manifest_path = export_record_store_to_yaml(store, self.export_root)
        records = [_load_record_file(manifest_path, index) for index in range(5)]
        for record in records:
            record["rebuild"] = "decoded"
        records[0]["decoded"]["fields"]["root_id"] = 9
        records[1]["decoded"]["fields"]["library_name"] = "New.Assembly"
        records[2]["decoded"]["fields"]["ref_id"] = 99
        records[3]["decoded"]["fields"]["count"] = 4
        for index, record in enumerate(records):
            _write_record_file(manifest_path, index, record)

        expected = (
            struct.pack("<Biiii", RecordTypeEnumeration.SerializedStreamHeader, 9, -1, 1, 0)
            + struct.pack("<Bi", RecordTypeEnumeration.BinaryLibrary, 2)
            + _lp("New.Assembly")
            + struct.pack("<Bi", RecordTypeEnumeration.MemberReference, 99)
            + struct.pack("BB", RecordTypeEnumeration.ObjectNullMultiple256, 4)
            + b"\x0b"
        )
        self.assertEqual(rebuild_yaml_export(self.export_root), expected)

    def test_array_single_string_items_can_be_edited(self) -> None:
        original_record = (
            struct.pack("<Bii", RecordTypeEnumeration.ArraySingleString, 77, 3)
            + BinaryObjectString(10, "old").to_bytes()
            + struct.pack("<Bi", RecordTypeEnumeration.MemberReference, 11)
            + struct.pack("B", RecordTypeEnumeration.ObjectNull)
        )
        replacement_record = (
            struct.pack("<Bii", RecordTypeEnumeration.ArraySingleString, 77, 3)
            + BinaryObjectString(10, "newer").to_bytes()
            + struct.pack("<Bi", RecordTypeEnumeration.MemberReference, 12)
            + struct.pack("B", RecordTypeEnumeration.ObjectNull)
        )
        source = original_record + b"\x0b"
        store = DNBFRecordStore(":memory:", source_bytes=source)
        store.add_raw_record(
            record_type=RecordTypeEnumeration.ArraySingleString,
            offset=0,
            raw=original_record,
            object_id=77,
        )
        store.add_raw_record(record_type=RecordTypeEnumeration.MessageEnd, offset=len(original_record), raw=b"\x0b")

        manifest_path = export_record_store_to_yaml(store, self.export_root)
        record = _load_record_file(manifest_path, 0)
        record["rebuild"] = "decoded"
        items = record["decoded"]["fields"]["items"]
        items[0]["value"] = "newer"
        items[1]["ref_id"] = 12
        _write_record_file(manifest_path, 0, record)

        self.assertEqual(rebuild_yaml_export(self.export_root), replacement_record + b"\x0b")

    def test_class_string_member_can_be_edited(self) -> None:
        original_record = _class_with_members_and_types_record(age=30, name="Alice", score=1.5)
        replacement_record = _class_with_members_and_types_record(age=30, name="Charlotte", score=1.5)
        source = original_record + b"\x0b"
        store = DNBFRecordStore(":memory:", source_bytes=source)
        store.add_raw_record(
            record_type=RecordTypeEnumeration.ClassWithMembersAndTypes,
            offset=0,
            raw=original_record,
            object_id=1,
            library_id=2,
        )
        store.add_raw_record(record_type=RecordTypeEnumeration.MessageEnd, offset=len(original_record), raw=b"\x0b")

        manifest_path = export_record_store_to_yaml(store, self.export_root)
        record = _load_record_file(manifest_path, 0)
        record["rebuild"] = "decoded"
        members = record["decoded"]["fields"]["members"]
        members[1]["value"] = "Charlotte"
        _write_record_file(manifest_path, 0, record)

        self.assertEqual(rebuild_yaml_export(self.export_root), replacement_record + b"\x0b")

    def test_binary_array_primitive_values_can_be_edited(self) -> None:
        original_record = (
            struct.pack(
                "<BiBi",
                RecordTypeEnumeration.BinaryArray,
                80,
                0,  # BinaryArrayTypeEnumeration.Single
                1,
            )
            + struct.pack("<i", 3)
            + struct.pack("BB", BinaryTypeEnumeration.Primitive, PrimitiveTypeEnumeration.Int32)
            + struct.pack("<iii", 4, 5, 6)
        )
        replacement_record = (
            struct.pack("<BiBi", RecordTypeEnumeration.BinaryArray, 80, 0, 1)
            + struct.pack("<i", 3)
            + struct.pack("BB", BinaryTypeEnumeration.Primitive, PrimitiveTypeEnumeration.Int32)
            + struct.pack("<iii", 4, 50, 6)
        )
        source = original_record + b"\x0b"
        store = DNBFRecordStore(":memory:", source_bytes=source)
        store.add_raw_record(record_type=RecordTypeEnumeration.BinaryArray, offset=0, raw=original_record, object_id=80)
        store.add_raw_record(record_type=RecordTypeEnumeration.MessageEnd, offset=len(original_record), raw=b"\x0b")

        manifest_path = export_record_store_to_yaml(store, self.export_root)
        record = _load_record_file(manifest_path, 0)
        record["rebuild"] = "decoded"
        record["decoded"]["fields"]["values"][1] = 50
        _write_record_file(manifest_path, 0, record)

        self.assertEqual(rebuild_yaml_export(self.export_root), replacement_record + b"\x0b")

    def test_binary_array_record_items_can_be_edited(self) -> None:
        original_record = (
            struct.pack("<BiBi", RecordTypeEnumeration.BinaryArray, 81, 0, 1)
            + struct.pack("<i", 2)
            + struct.pack("B", BinaryTypeEnumeration.String)
            + BinaryObjectString(20, "red").to_bytes()
            + struct.pack("<Bi", RecordTypeEnumeration.MemberReference, 21)
        )
        replacement_record = (
            struct.pack("<BiBi", RecordTypeEnumeration.BinaryArray, 81, 0, 1)
            + struct.pack("<i", 2)
            + struct.pack("B", BinaryTypeEnumeration.String)
            + BinaryObjectString(20, "blue").to_bytes()
            + struct.pack("<Bi", RecordTypeEnumeration.MemberReference, 22)
        )
        source = original_record + b"\x0b"
        store = DNBFRecordStore(":memory:", source_bytes=source)
        store.add_raw_record(record_type=RecordTypeEnumeration.BinaryArray, offset=0, raw=original_record, object_id=81)
        store.add_raw_record(record_type=RecordTypeEnumeration.MessageEnd, offset=len(original_record), raw=b"\x0b")

        manifest_path = export_record_store_to_yaml(store, self.export_root)
        record = _load_record_file(manifest_path, 0)
        record["rebuild"] = "decoded"
        items = record["decoded"]["fields"]["items"]
        items[0]["value"] = "blue"
        items[1]["ref_id"] = 22
        _write_record_file(manifest_path, 0, record)

        self.assertEqual(rebuild_yaml_export(self.export_root), replacement_record + b"\x0b")

    def test_method_call_inline_fields_can_be_edited(self) -> None:
        flags = 0x00000002 | 0x00000020
        original_record = (
            struct.pack("<BI", RecordTypeEnumeration.MethodCall, flags)
            + _value_with_code("String", "OldMethod")
            + _value_with_code("String", "Old.Type")
            + _value_with_code("String", "ctx")
            + struct.pack("<i", 2)
            + _value_with_code("Int32", 7)
            + _value_with_code("String", "arg")
        )
        replacement_record = (
            struct.pack("<BI", RecordTypeEnumeration.MethodCall, flags)
            + _value_with_code("String", "NewMethod")
            + _value_with_code("String", "New.Type")
            + _value_with_code("String", "ctx2")
            + struct.pack("<i", 2)
            + _value_with_code("Int32", 8)
            + _value_with_code("String", "arg2")
        )
        source = original_record + b"\x0b"
        store = DNBFRecordStore(":memory:", source_bytes=source)
        store.add_raw_record(record_type=RecordTypeEnumeration.MethodCall, offset=0, raw=original_record)
        store.add_raw_record(record_type=RecordTypeEnumeration.MessageEnd, offset=len(original_record), raw=b"\x0b")

        manifest_path = export_record_store_to_yaml(store, self.export_root)
        record = _load_record_file(manifest_path, 0)
        record["rebuild"] = "decoded"
        fields = record["decoded"]["fields"]
        fields["method_name"] = "NewMethod"
        fields["type_name"] = "New.Type"
        fields["call_context"] = "ctx2"
        fields["args"][0]["value"] = 8
        fields["args"][1]["value"] = "arg2"
        _write_record_file(manifest_path, 0, record)

        self.assertEqual(rebuild_yaml_export(self.export_root), replacement_record + b"\x0b")

    def test_method_return_inline_fields_can_be_edited(self) -> None:
        flags = 0x00000800 | 0x00000020
        original_record = (
            struct.pack("<BI", RecordTypeEnumeration.MethodReturn, flags)
            + _value_with_code("Int32", 200)
            + _value_with_code("String", "ctx")
        )
        replacement_record = (
            struct.pack("<BI", RecordTypeEnumeration.MethodReturn, flags)
            + _value_with_code("Int32", 404)
            + _value_with_code("String", "ctx2")
        )
        source = original_record + b"\x0b"
        store = DNBFRecordStore(":memory:", source_bytes=source)
        store.add_raw_record(record_type=RecordTypeEnumeration.MethodReturn, offset=0, raw=original_record)
        store.add_raw_record(record_type=RecordTypeEnumeration.MessageEnd, offset=len(original_record), raw=b"\x0b")

        manifest_path = export_record_store_to_yaml(store, self.export_root)
        record = _load_record_file(manifest_path, 0)
        record["rebuild"] = "decoded"
        fields = record["decoded"]["fields"]
        fields["return_value"]["value"] = 404
        fields["call_context"] = "ctx2"
        _write_record_file(manifest_path, 0, record)

        self.assertEqual(rebuild_yaml_export(self.export_root), replacement_record + b"\x0b")


def _load_record_file(manifest_path: Path, index: int) -> dict:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    record = json.loads((manifest_path.parent / manifest["records"][index]["path"]).read_text(encoding="utf-8"))
    fields = record.get("decoded", {}).get("fields", {})
    members = fields.get("members")
    if isinstance(members, dict) and isinstance(members.get("files"), list):
        fields["members"] = [
            json.loads((manifest_path.parent / item["path"]).read_text(encoding="utf-8"))
            for item in sorted(members["files"], key=lambda value: value["index"])
        ]
    for key in ("values", "items", "args"):
        value = fields.get(key)
        if isinstance(value, dict) and isinstance(value.get("path"), str):
            fields[key] = json.loads((manifest_path.parent / value["path"]).read_text(encoding="utf-8"))
    return_value = fields.get("return_value")
    if isinstance(return_value, dict) and isinstance(return_value.get("path"), str):
        fields["return_value"] = json.loads((manifest_path.parent / return_value["path"]).read_text(encoding="utf-8"))
    return record


def _write_record_file(manifest_path: Path, index: int, record: dict) -> None:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    record_path = manifest_path.parent / manifest["records"][index]["path"]
    record_path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")


def _lp(value: str) -> bytes:
    encoded = value.encode("utf-8")
    if len(encoded) > 0x7F:
        raise ValueError("test helper only supports short strings")
    return bytes([len(encoded)]) + encoded


def _class_with_members_and_types_record(*, age: int, score: float, name: str = "Alice") -> bytes:
    result = bytearray()
    result += struct.pack("B", RecordTypeEnumeration.ClassWithMembersAndTypes)
    result += struct.pack("<i", 1)
    result += _lp("Person")
    result += struct.pack("<i", 3)
    result += _lp("Age")
    result += _lp("Name")
    result += _lp("Score")
    result += bytes(
        [
            BinaryTypeEnumeration.Primitive,
            BinaryTypeEnumeration.String,
            BinaryTypeEnumeration.Primitive,
        ]
    )
    result += bytes([PrimitiveTypeEnumeration.Int32, PrimitiveTypeEnumeration.Single])
    result += struct.pack("<i", 2)
    result += struct.pack("<i", age)
    result += BinaryObjectString(99, name).to_bytes()
    result += struct.pack("<f", score)
    return bytes(result)


def _class_with_id_record(*, object_id: int, metadata_id: int, age: int, score: float) -> bytes:
    result = bytearray()
    result += struct.pack("B", RecordTypeEnumeration.ClassWithId)
    result += struct.pack("<i", object_id)
    result += struct.pack("<i", metadata_id)
    result += struct.pack("<i", age)
    result += BinaryObjectString(100 + object_id, "Bob").to_bytes()
    result += struct.pack("<f", score)
    return bytes(result)


def _class_with_reference_member_record(*, ref_id: int) -> bytes:
    result = bytearray()
    result += struct.pack("B", RecordTypeEnumeration.ClassWithMembersAndTypes)
    result += struct.pack("<i", 1)
    result += _lp("Life")
    result += struct.pack("<i", 1)
    result += _lp("<LifeLog>k__BackingField")
    result += bytes([BinaryTypeEnumeration.Class])
    result += _lp("LifeLog")
    result += struct.pack("<i", 2)
    result += struct.pack("<i", 2)
    result += struct.pack("<Bi", RecordTypeEnumeration.MemberReference, ref_id)
    return bytes(result)


def _value_with_code(primitive_type: str, value) -> bytes:
    primitive = PrimitiveTypeEnumeration[primitive_type]
    result = bytearray([primitive])
    if primitive == PrimitiveTypeEnumeration.String:
        result += _lp(value)
    elif primitive == PrimitiveTypeEnumeration.Int32:
        result += struct.pack("<i", value)
    else:
        raise ValueError(f"unsupported test primitive: {primitive_type}")
    return bytes(result)


if __name__ == "__main__":
    unittest.main()
