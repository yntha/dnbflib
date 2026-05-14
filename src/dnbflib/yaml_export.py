from __future__ import annotations

import json
import struct
from hashlib import sha256
from pathlib import Path
from typing import Any

from datastream import ByteOrder, DeserializingStream, SerializingStream

from dnbflib.indexer import DNBFRecordStore, StoredRecord
from dnbflib.records import (
    BinaryObjectString,
    PRIMITIVE_FORMATS,
    BinaryTypeEnumeration,
    PrimitiveTypeEnumeration,
    RecordTypeEnumeration,
)

EXPORT_FORMAT = "dnbflib.yaml_export"
EXPORT_VERSION = 1
BINARY_ARRAY_TYPE_NAMES = {
    0: "Single",
    1: "Jagged",
    2: "Rectangular",
    3: "SingleOffset",
    4: "JaggedOffset",
    5: "RectangularOffset",
}
BINARY_ARRAY_TYPE_VALUES = {value: key for key, value in BINARY_ARRAY_TYPE_NAMES.items()}
BINARY_ARRAY_TYPES_WITH_LOWER_BOUNDS = {3, 4, 5}
MESSAGE_FLAG_ARGS_INLINE = 0x00000002
MESSAGE_FLAG_CONTEXT_INLINE = 0x00000020
MESSAGE_FLAG_RETURN_VALUE_INLINE = 0x00000800


class DNBFYamlExportError(Exception):
    """Raised when a YAML export package cannot be read or rebuilt."""


def export_dnbf_to_yaml(
    input_path: str | Path,
    export_dir: str | Path,
    *,
    database_path: str | Path | None = None,
    recreate_database: bool = True,
) -> Path:
    """
    Parse a DNBF/NRBF binary and export a lossless YAML package with raw record sidecars.

    The generated ``manifest.yaml`` is YAML-compatible JSON so it remains dependency-free to rebuild.
    """
    source_path = Path(input_path)
    try:
        record_store = _scan_binary_to_record_store(source_path)
    except Exception as exc:
        return _export_unparsed_binary_to_yaml(source_path, export_dir, parser_error=exc)

    if database_path is not None:
        persisted_store = DNBFRecordStore(
            Path(database_path),
            source_path=source_path,
            source_bytes=source_path.read_bytes(),
            recreate=recreate_database,
        )
        for record in record_store.iter_records():
            persisted_store.add_raw_record(
                record_type=record.record_type,
                offset=record.offset,
                raw=record.raw,
                object_id=record.object_id,
                library_id=record.library_id,
                reference_id=record.reference_id,
                metadata_id=record.metadata_id,
                repr_text=record.repr_text,
            )
        record_store = persisted_store

    return export_record_store_to_yaml(record_store, export_dir)


def export_record_store_to_yaml(
    record_store: DNBFRecordStore,
    export_dir: str | Path,
    *,
    manifest_name: str = "manifest.yaml",
    raw_dir_name: str = "raw",
    records_dir_name: str = "records",
) -> Path:
    """
    Export a ``DNBFRecordStore`` into a lossless YAML package.

    Rebuilds use sidecar bytes by default. Each record gets its own editable
    ``record.yaml`` file; set that record's ``rebuild`` value to ``decoded`` to
    rebuild from supported decoded fields.
    """
    root = Path(export_dir)
    records_root = root / records_dir_name
    records_root.mkdir(parents=True, exist_ok=True)

    pending_records = []
    object_paths: dict[int, str] = {}
    decode_context: dict[int, dict[str, Any]] = {}
    for stored_record in record_store.iter_records():
        record_entry = _record_to_manifest_entry(stored_record, "", decode_context)
        record_dir_relative = _record_relative_dir(record_entry, records_dir_name)
        record_path_relative = f"{record_dir_relative}/record.yaml"
        if record_entry.get("object_id") is not None:
            object_paths[int(record_entry["object_id"])] = record_path_relative
        pending_records.append((stored_record, record_entry, record_dir_relative, record_path_relative))

    records = []
    for stored_record, record_entry, record_dir_relative, record_path_relative in pending_records:
        record_dir = root / record_dir_relative
        record_dir.mkdir(parents=True, exist_ok=True)

        raw_path = record_dir / f"{raw_dir_name}.bin"
        raw_path.write_bytes(stored_record.raw)

        record_path = record_dir / "record.yaml"
        raw_path_relative = f"{record_dir_relative}/{raw_dir_name}.bin"
        record_entry["raw"]["path"] = raw_path_relative
        _annotate_reference_paths(record_entry, object_paths)
        _externalize_decoded_files(record_entry, record_dir, record_dir_relative)
        record_path.write_text(_dump_yaml_compatible_json(record_entry), encoding="utf-8")
        records.append(_record_index_entry(record_entry, record_path_relative))

    manifest = {
        "format": EXPORT_FORMAT,
        "version": EXPORT_VERSION,
        "note": "This file is an index. Edit per-record records/*/record.yaml files.",
        "source": {
            "path": record_store.get_metadata("source_path"),
            "sha256": record_store.get_metadata("source_sha256"),
            "size": _metadata_int(record_store.get_metadata("source_size")),
        },
        "records": records,
    }

    manifest_path = root / manifest_name
    manifest_path.write_text(_dump_yaml_compatible_json(manifest), encoding="utf-8")
    return manifest_path


def rebuild_yaml_export(
    export_dir: str | Path,
    output_path: str | Path | None = None,
    *,
    manifest_name: str = "manifest.yaml",
) -> bytes:
    """
    Rebuild a DNBF/NRBF binary from a YAML export package.

    Records rebuild from raw sidecars unless their manifest entry has ``rebuild: decoded`` and the record
    type is supported by the decoded-field rebuilder.
    """
    root = Path(export_dir)
    manifest = _load_manifest(root / manifest_name)
    record_entries = [_load_record_entry(root, entry) for entry in manifest["records"]]
    chunks = [_record_entry_to_bytes(root, entry) for entry in record_entries]
    rebuilt = b"".join(chunks)

    output_sha256 = sha256(rebuilt).hexdigest()
    source_sha256 = manifest.get("source", {}).get("sha256")
    if source_sha256 is not None and output_sha256 != source_sha256:
        changed = any(entry.get("rebuild") == "decoded" for entry in record_entries)
        if not changed:
            raise DNBFYamlExportError(
                "rebuilt bytes do not match the source hash, and no decoded records were requested"
            )

    if output_path is not None:
        Path(output_path).write_bytes(rebuilt)

    return rebuilt


def _record_to_manifest_entry(
    stored_record: StoredRecord,
    raw_path: str,
    decode_context: dict[int, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if decode_context is None:
        decode_context = {}

    decoded_error = None
    try:
        decoded = _decode_supported_record(stored_record, decode_context)
    except Exception as exc:
        decoded = None
        decoded_error = {
            "type": type(exc).__name__,
            "message": str(exc),
        }

    entry: dict[str, Any] = {
        "sequence": stored_record.sequence,
        "record_type": stored_record.record_type.name,
        "record_type_id": int(stored_record.record_type),
        "offset": stored_record.offset,
        "size": stored_record.size,
        "object_id": stored_record.object_id,
        "library_id": stored_record.library_id,
        "reference_id": stored_record.reference_id,
        "metadata_id": stored_record.metadata_id,
        "rebuild": "raw",
        "raw": {
            "path": raw_path,
            "sha256": sha256(stored_record.raw).hexdigest(),
            "size": len(stored_record.raw),
        },
    }

    if decoded is not None:
        entry["decoded"] = decoded
    elif decoded_error is not None:
        entry["decoded_error"] = decoded_error

    if stored_record.repr_text:
        entry["repr"] = stored_record.repr_text

    return entry


def _record_index_entry(record_entry: dict[str, Any], record_path: str) -> dict[str, Any]:
    return {
        "sequence": record_entry["sequence"],
        "record_type": record_entry["record_type"],
        "record_type_id": record_entry["record_type_id"],
        "offset": record_entry["offset"],
        "size": record_entry["size"],
        "object_id": record_entry["object_id"],
        "library_id": record_entry["library_id"],
        "reference_id": record_entry["reference_id"],
        "metadata_id": record_entry["metadata_id"],
        "path": record_path,
    }


def _load_record_entry(root: Path, manifest_entry: dict[str, Any]) -> dict[str, Any]:
    record_path = manifest_entry.get("path")
    if record_path is None:
        return manifest_entry

    path = root / str(record_path)
    entry = json.loads(path.read_text(encoding="utf-8"))
    if entry.get("sequence") != manifest_entry.get("sequence"):
        raise DNBFYamlExportError(f"record sequence mismatch: {path}")
    if entry.get("record_type") != manifest_entry.get("record_type"):
        raise DNBFYamlExportError(f"record type mismatch: {path}")
    return _hydrate_decoded_files(root, entry)


def _annotate_reference_paths(value: Any, object_paths: dict[int, str]) -> None:
    if isinstance(value, dict):
        ref_id = value.get("ref_id")
        if ref_id is not None:
            try:
                ref_path = object_paths.get(int(ref_id))
            except (TypeError, ValueError):
                ref_path = None
            if ref_path is not None:
                value["ref_path"] = ref_path

        for child in value.values():
            _annotate_reference_paths(child, object_paths)
    elif isinstance(value, list):
        for child in value:
            _annotate_reference_paths(child, object_paths)


def _externalize_decoded_files(entry: dict[str, Any], record_dir: Path, record_dir_relative: str) -> None:
    decoded = entry.get("decoded")
    if not isinstance(decoded, dict):
        return

    fields = decoded.get("fields")
    if not isinstance(fields, dict):
        return

    decoded_dir = record_dir / "decoded"

    members = fields.get("members")
    if isinstance(members, list):
        member_dir = decoded_dir / "members"
        member_dir.mkdir(parents=True, exist_ok=True)
        files = []
        for index, member in enumerate(members):
            name = str(member.get("name", "member")) if isinstance(member, dict) else "member"
            member_name = f"{index:03d}_{_safe_path_part(name)}.yaml"
            member_path = member_dir / member_name
            member_path.write_text(_dump_yaml_compatible_json(member), encoding="utf-8")
            files.append(
                {
                    "index": index,
                    "name": name,
                    "path": f"{record_dir_relative}/decoded/members/{member_name}",
                }
            )
        fields["members"] = {
            "path": f"{record_dir_relative}/decoded/members",
            "count": len(members),
            "files": files,
        }

    for key in ("values", "items", "args"):
        value = fields.get(key)
        if isinstance(value, list):
            decoded_dir.mkdir(parents=True, exist_ok=True)
            collection_path = decoded_dir / f"{key}.yaml"
            collection_path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            fields[key] = {
                "path": f"{record_dir_relative}/decoded/{key}.yaml",
                "count": len(value),
            }

    return_value = fields.get("return_value")
    if isinstance(return_value, dict):
        decoded_dir.mkdir(parents=True, exist_ok=True)
        return_value_path = decoded_dir / "return_value.yaml"
        return_value_path.write_text(_dump_yaml_compatible_json(return_value), encoding="utf-8")
        fields["return_value"] = {
            "path": f"{record_dir_relative}/decoded/return_value.yaml",
        }


def _hydrate_decoded_files(root: Path, entry: dict[str, Any]) -> dict[str, Any]:
    decoded = entry.get("decoded")
    if not isinstance(decoded, dict):
        return entry

    fields = decoded.get("fields")
    if not isinstance(fields, dict):
        return entry

    members = fields.get("members")
    if isinstance(members, dict) and isinstance(members.get("files"), list):
        loaded_members = []
        for member_file in sorted(members["files"], key=lambda item: int(item["index"])):
            loaded_members.append(json.loads((root / str(member_file["path"])).read_text(encoding="utf-8")))
        fields["members"] = loaded_members

    for key in ("values", "items", "args"):
        value = fields.get(key)
        if isinstance(value, dict) and isinstance(value.get("path"), str):
            fields[key] = json.loads((root / value["path"]).read_text(encoding="utf-8"))

    return_value = fields.get("return_value")
    if isinstance(return_value, dict) and isinstance(return_value.get("path"), str):
        fields["return_value"] = json.loads((root / return_value["path"]).read_text(encoding="utf-8"))

    return entry


def _record_entry_to_bytes(root: Path, entry: dict[str, Any]) -> bytes:
    if entry.get("rebuild") == "decoded":
        return _rebuild_decoded_record(root, entry)

    return _raw_entry_to_bytes(root, entry)


def _raw_entry_to_bytes(root: Path, entry: dict[str, Any]) -> bytes:
    raw_info = entry.get("raw")
    if not isinstance(raw_info, dict):
        raise DNBFYamlExportError(f"record {entry.get('sequence')} is missing raw sidecar metadata")

    raw_path = root / str(raw_info["path"])
    raw = raw_path.read_bytes()
    expected_sha256 = raw_info.get("sha256")
    if expected_sha256 is not None and sha256(raw).hexdigest() != expected_sha256:
        raise DNBFYamlExportError(f"raw sidecar hash mismatch: {raw_path}")

    expected_size = raw_info.get("size")
    if expected_size is not None and len(raw) != int(expected_size):
        raise DNBFYamlExportError(f"raw sidecar size mismatch: {raw_path}")

    return raw


def _export_unparsed_binary_to_yaml(
    source_path: Path,
    export_dir: str | Path,
    *,
    parser_error: Exception,
    manifest_name: str = "manifest.yaml",
    raw_dir_name: str = "raw",
    records_dir_name: str = "records",
) -> Path:
    root = Path(export_dir)
    record_entry = {
        "sequence": 1,
        "record_type": "UnparsedStream",
        "record_type_id": None,
        "offset": 0,
        "size": source_path.stat().st_size,
        "object_id": None,
        "library_id": None,
        "reference_id": None,
        "metadata_id": None,
        "rebuild": "raw",
        "raw": {},
    }
    record_dir_relative = _record_relative_dir(record_entry, records_dir_name)
    record_dir = root / record_dir_relative
    record_dir.mkdir(parents=True, exist_ok=True)

    source_bytes = source_path.read_bytes()
    raw_path = record_dir / f"{raw_dir_name}.bin"
    raw_path.write_bytes(source_bytes)
    record_path = record_dir / "record.yaml"
    record_path_relative = f"{record_dir_relative}/record.yaml"
    raw_path_relative = f"{record_dir_relative}/{raw_dir_name}.bin"
    record_entry["size"] = len(source_bytes)
    record_entry["raw"] = {
        "path": raw_path_relative,
        "sha256": sha256(source_bytes).hexdigest(),
        "size": len(source_bytes),
    }
    record_path.write_text(_dump_yaml_compatible_json(record_entry), encoding="utf-8")

    manifest = {
        "format": EXPORT_FORMAT,
        "version": EXPORT_VERSION,
        "note": "This file is an index. The parser could not split records, so rebuild uses the raw stream.",
        "source": {
            "path": str(source_path),
            "sha256": sha256(source_bytes).hexdigest(),
            "size": len(source_bytes),
        },
        "parser_error": {
            "type": type(parser_error).__name__,
            "message": str(parser_error),
        },
        "records": [_record_index_entry(record_entry, record_path_relative)],
    }

    manifest_path = root / manifest_name
    manifest_path.write_text(_dump_yaml_compatible_json(manifest), encoding="utf-8")
    return manifest_path


def _decode_supported_record(
    stored_record: StoredRecord,
    decode_context: dict[int, dict[str, Any]],
) -> dict[str, Any] | None:
    if stored_record.record_type == RecordTypeEnumeration.BinaryObjectString:
        return _decode_binary_object_string(stored_record.raw)

    if stored_record.record_type in (
        RecordTypeEnumeration.ClassWithMembersAndTypes,
        RecordTypeEnumeration.SystemClassWithMembersAndTypes,
    ):
        return _decode_class_with_members_and_types(stored_record.raw, decode_context)

    if stored_record.record_type == RecordTypeEnumeration.ClassWithId:
        return _decode_class_with_id(stored_record.raw, decode_context)

    if stored_record.record_type == RecordTypeEnumeration.MemberPrimitiveTyped:
        return _decode_member_primitive_typed(stored_record.raw)

    if stored_record.record_type == RecordTypeEnumeration.ArraySinglePrimitive:
        return _decode_array_single_primitive(stored_record.raw)

    if stored_record.record_type == RecordTypeEnumeration.ArraySingleString:
        return _decode_array_single_record(stored_record.raw, "ArraySingleString")

    if stored_record.record_type == RecordTypeEnumeration.ArraySingleObject:
        return _decode_array_single_record(stored_record.raw, "ArraySingleObject")

    if stored_record.record_type == RecordTypeEnumeration.BinaryArray:
        return _decode_binary_array(stored_record.raw)

    if stored_record.record_type == RecordTypeEnumeration.MethodCall:
        return _decode_method_call(stored_record.raw)

    if stored_record.record_type == RecordTypeEnumeration.MethodReturn:
        return _decode_method_return(stored_record.raw)

    if stored_record.record_type == RecordTypeEnumeration.SerializedStreamHeader and len(stored_record.raw) == 17:
        _, root_id, header_id, major_version, minor_version = struct.unpack("<Biiii", stored_record.raw)
        return {
            "editable": True,
            "type": "SerializedStreamHeader",
            "fields": {
                "root_id": root_id,
                "header_id": header_id,
                "major_version": major_version,
                "minor_version": minor_version,
            },
        }

    if stored_record.record_type == RecordTypeEnumeration.BinaryLibrary:
        return _decode_binary_library(stored_record.raw)

    if stored_record.record_type == RecordTypeEnumeration.MemberReference and len(stored_record.raw) == 5:
        _, ref_id = struct.unpack("<Bi", stored_record.raw)
        return {
            "editable": True,
            "type": "MemberReference",
            "fields": {"ref_id": ref_id},
        }

    if stored_record.record_type == RecordTypeEnumeration.ObjectNull:
        return {
            "editable": False,
            "type": "ObjectNull",
            "fields": {},
        }

    if stored_record.record_type == RecordTypeEnumeration.ObjectNullMultiple256 and len(stored_record.raw) == 2:
        return {
            "editable": True,
            "type": "ObjectNullMultiple256",
            "fields": {"count": stored_record.raw[1]},
        }

    if stored_record.record_type == RecordTypeEnumeration.ObjectNullMultiple and len(stored_record.raw) == 5:
        _, count = struct.unpack("<Bi", stored_record.raw)
        return {
            "editable": True,
            "type": "ObjectNullMultiple",
            "fields": {"count": count},
        }

    if stored_record.record_type == RecordTypeEnumeration.MessageEnd:
        return {
            "editable": False,
            "type": "MessageEnd",
            "fields": {},
        }

    return None


def _decode_binary_object_string(raw: bytes) -> dict[str, Any] | None:
    if len(raw) < 6 or raw[0] != RecordTypeEnumeration.BinaryObjectString:
        return None

    object_id = struct.unpack("<i", raw[1:5])[0]
    try:
        value, size = _decode_length_prefixed_string(raw, 5)
    except ValueError:
        return None

    if 5 + size != len(raw):
        return None

    return {
        "editable": True,
        "type": "BinaryObjectString",
        "fields": {
            "object_id": object_id,
            "value": value,
        },
    }


def _decode_binary_library(raw: bytes) -> dict[str, Any] | None:
    if len(raw) < 6 or raw[0] != RecordTypeEnumeration.BinaryLibrary:
        return None

    library_id = _read_int32(raw, 1)
    try:
        library_name, size = _decode_length_prefixed_string(raw, 5)
    except ValueError:
        return None

    if 5 + size != len(raw):
        return None

    return {
        "editable": True,
        "type": "BinaryLibrary",
        "fields": {
            "library_id": library_id,
            "library_name": library_name,
        },
    }


def _decode_member_primitive_typed(raw: bytes) -> dict[str, Any] | None:
    if len(raw) < 2 or raw[0] != RecordTypeEnumeration.MemberPrimitiveTyped:
        return None

    try:
        primitive_type = PrimitiveTypeEnumeration(raw[1])
    except ValueError:
        return None

    value, consumed = _decode_primitive_value(raw, 2, primitive_type)
    if value is _UNSUPPORTED or consumed is None:
        return None

    if 2 + consumed != len(raw):
        return None

    return {
        "editable": True,
        "type": "MemberPrimitiveTyped",
        "fields": {
            "primitive_type": primitive_type.name,
            "value": value,
        },
    }


def _decode_array_single_primitive(raw: bytes) -> dict[str, Any] | None:
    if len(raw) < 10 or raw[0] != RecordTypeEnumeration.ArraySinglePrimitive:
        return None

    object_id, length = struct.unpack("<ii", raw[1:9])
    if length < 0:
        return None

    try:
        primitive_type = PrimitiveTypeEnumeration(raw[9])
    except ValueError:
        return None

    values = []
    offset = 10
    for _ in range(length):
        value, consumed = _decode_primitive_value(raw, offset, primitive_type)
        if value is _UNSUPPORTED or consumed is None:
            return None
        values.append(value)
        offset += consumed

    if offset != len(raw):
        return None

    return {
        "editable": True,
        "type": "ArraySinglePrimitive",
        "fields": {
            "object_id": object_id,
            "primitive_type": primitive_type.name,
            "values": values,
        },
    }


def _decode_array_single_record(raw: bytes, decoded_type: str) -> dict[str, Any] | None:
    if decoded_type == "ArraySingleString":
        expected_record_type = RecordTypeEnumeration.ArraySingleString
    elif decoded_type == "ArraySingleObject":
        expected_record_type = RecordTypeEnumeration.ArraySingleObject
    else:
        return None

    if len(raw) < 9 or raw[0] != expected_record_type:
        return None

    object_id = _read_int32(raw, 1)
    length = _read_int32(raw, 5)
    if length < 0:
        return None

    try:
        end_offset, items = _decode_record_items(raw, 9, length, {})
    except (IndexError, struct.error, ValueError):
        return None

    if end_offset != len(raw):
        return None

    return {
        "editable": True,
        "type": decoded_type,
        "fields": {
            "object_id": object_id,
            "items": items,
        },
    }


def _decode_binary_array(raw: bytes) -> dict[str, Any] | None:
    if len(raw) < 12 or raw[0] != RecordTypeEnumeration.BinaryArray:
        return None

    try:
        header = _parse_binary_array_header(raw, 0)
    except (IndexError, struct.error, ValueError):
        return None

    cursor = header["members_offset"]
    item_count = _product(header["lengths"])
    fields: dict[str, Any] = {
        "object_id": header["object_id"],
        "binary_array_type": BINARY_ARRAY_TYPE_NAMES[header["binary_array_type"]],
        "rank": header["rank"],
        "lengths": header["lengths"],
        "lower_bounds": header["lower_bounds"],
        "type_enum": header["type_enum"].name,
        "additional_type_info": _additional_type_info_to_manifest(header["additional_type_info"]),
    }

    if header["type_enum"] in (BinaryTypeEnumeration.Primitive, BinaryTypeEnumeration.PrimitiveArray):
        primitive_type = header["additional_type_info"]
        values = []
        for _ in range(item_count):
            value, consumed = _decode_primitive_value(raw, cursor, primitive_type)
            if value is _UNSUPPORTED or consumed is None:
                return None
            values.append(value)
            cursor += consumed
        fields["values"] = values
    else:
        try:
            cursor, items = _decode_record_items(raw, cursor, item_count, {})
        except (IndexError, struct.error, ValueError):
            return None
        fields["items"] = items

    if cursor != len(raw):
        return None

    return {
        "editable": True,
        "type": "BinaryArray",
        "fields": fields,
    }


def _decode_method_call(raw: bytes) -> dict[str, Any] | None:
    if len(raw) < 5 or raw[0] != RecordTypeEnumeration.MethodCall:
        return None

    try:
        flags = struct.unpack("<I", raw[1:5])[0]
        cursor = 5
        method_name, cursor = _decode_string_value_with_code(raw, cursor)
        type_name, cursor = _decode_string_value_with_code(raw, cursor)
        fields: dict[str, Any] = {
            "message_flags": flags,
            "method_name": method_name,
            "type_name": type_name,
        }
        if flags & MESSAGE_FLAG_CONTEXT_INLINE:
            fields["call_context"], cursor = _decode_string_value_with_code(raw, cursor)
        if flags & MESSAGE_FLAG_ARGS_INLINE:
            fields["args"], cursor = _decode_array_of_value_with_code(raw, cursor)
    except (IndexError, struct.error, ValueError):
        return None

    if cursor != len(raw):
        return None

    return {
        "editable": True,
        "type": "MethodCall",
        "fields": fields,
    }


def _decode_method_return(raw: bytes) -> dict[str, Any] | None:
    if len(raw) < 5 or raw[0] != RecordTypeEnumeration.MethodReturn:
        return None

    try:
        flags = struct.unpack("<I", raw[1:5])[0]
        cursor = 5
        fields: dict[str, Any] = {"message_flags": flags}
        if flags & MESSAGE_FLAG_RETURN_VALUE_INLINE:
            fields["return_value"], cursor = _decode_value_with_code(raw, cursor)
        if flags & MESSAGE_FLAG_CONTEXT_INLINE:
            fields["call_context"], cursor = _decode_string_value_with_code(raw, cursor)
        if flags & MESSAGE_FLAG_ARGS_INLINE:
            fields["args"], cursor = _decode_array_of_value_with_code(raw, cursor)
    except (IndexError, struct.error, ValueError):
        return None

    if cursor != len(raw):
        return None

    return {
        "editable": True,
        "type": "MethodReturn",
        "fields": fields,
    }


def _decode_class_with_members_and_types(
    raw: bytes,
    decode_context: dict[int, dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    if not raw or raw[0] not in (
        RecordTypeEnumeration.ClassWithMembersAndTypes,
        RecordTypeEnumeration.SystemClassWithMembersAndTypes,
    ):
        return None

    if decode_context is None:
        decode_context = {}

    try:
        parsed = _parse_class_with_members_and_types(raw, 0, decode_context)
    except (IndexError, struct.error, ValueError):
        return None

    member_entries = []
    for member in parsed["members"]:
        entry = {
            "name": member["name"],
            "binary_type": member["binary_type"].name,
            "editable": False,
        }
        additional_info = member.get("additional_info")
        if additional_info is not None:
            entry["additional_type_info"] = _additional_type_info_to_manifest(additional_info)
        primitive_type = member.get("primitive_type")
        if primitive_type is not None:
            entry.update(
                {
                    "editable": True,
                    "primitive_type": primitive_type.name,
                    "value": member["value"],
                    "value_offset": member["value_offset"],
                    "value_size": member["value_size"],
                }
            )
        elif member.get("editable"):
            entry.update(
                {
                    "editable": True,
                    "record_type": member["record_type"],
                    "value_offset": member["value_offset"],
                    "value_size": member["value_size"],
                }
            )
            for key in ("object_id", "value", "ref_id", "count"):
                if key in member:
                    entry[key] = member[key]
        member_entries.append(entry)

    fields = {
        "object_id": parsed["object_id"],
        "class_name": parsed["class_name"],
        "members": member_entries,
    }
    if "library_id" in parsed:
        fields["library_id"] = parsed["library_id"]

    return {
        "editable": any(member["editable"] for member in member_entries),
        "type": RecordTypeEnumeration(raw[0]).name,
        "fields": fields,
    }


def _decode_class_with_id(
    raw: bytes,
    decode_context: dict[int, dict[str, Any]],
) -> dict[str, Any] | None:
    if len(raw) < 9 or raw[0] != RecordTypeEnumeration.ClassWithId:
        return None

    object_id = _read_int32(raw, 1)
    metadata_id = _read_int32(raw, 5)
    class_def = decode_context.get(metadata_id)
    if class_def is None:
        return None

    try:
        end_offset, members = _parse_class_members(
            raw,
            9,
            class_def["member_names"],
            class_def["binary_types"],
            class_def["additional_infos"],
            decode_context,
        )
    except (IndexError, struct.error, ValueError):
        return None

    if end_offset != len(raw):
        return None

    member_entries = []
    for member in members:
        entry = {
            "name": member["name"],
            "binary_type": member["binary_type"].name,
            "editable": False,
        }
        additional_info = member.get("additional_info")
        if additional_info is not None:
            entry["additional_type_info"] = _additional_type_info_to_manifest(additional_info)
        primitive_type = member.get("primitive_type")
        if primitive_type is not None:
            entry.update(
                {
                    "editable": True,
                    "primitive_type": primitive_type.name,
                    "value": member["value"],
                    "value_offset": member["value_offset"],
                    "value_size": member["value_size"],
                }
            )
        elif member.get("editable"):
            entry.update(
                {
                    "editable": True,
                    "record_type": member["record_type"],
                    "value_offset": member["value_offset"],
                    "value_size": member["value_size"],
                }
            )
            for key in ("object_id", "value", "ref_id", "count"):
                if key in member:
                    entry[key] = member[key]
        member_entries.append(entry)

    return {
        "editable": any(member["editable"] for member in member_entries),
        "type": "ClassWithId",
        "fields": {
            "object_id": object_id,
            "metadata_id": metadata_id,
            "members": member_entries,
        },
    }


def _rebuild_decoded_record(root: Path, entry: dict[str, Any]) -> bytes:
    decoded = entry.get("decoded")
    if not isinstance(decoded, dict):
        raise DNBFYamlExportError(f"record {entry.get('sequence')} requested decoded rebuild without decoded data")

    decoded_type = decoded.get("type")
    fields = decoded.get("fields")
    if not isinstance(fields, dict):
        raise DNBFYamlExportError(f"record {entry.get('sequence')} decoded data is missing fields")

    if decoded_type == "BinaryObjectString":
        object_id = int(fields["object_id"])
        value = str(fields["value"])
        return BinaryObjectString(object_id, value).to_bytes()

    if decoded_type == "SerializedStreamHeader":
        return struct.pack(
            "<Biiii",
            RecordTypeEnumeration.SerializedStreamHeader.value,
            int(fields["root_id"]),
            int(fields["header_id"]),
            int(fields["major_version"]),
            int(fields["minor_version"]),
        )

    if decoded_type == "BinaryLibrary":
        return (
            struct.pack("<Bi", RecordTypeEnumeration.BinaryLibrary.value, int(fields["library_id"]))
            + _encode_length_prefixed_string(str(fields["library_name"]))
        )

    if decoded_type == "MemberReference":
        return struct.pack("<Bi", RecordTypeEnumeration.MemberReference.value, int(fields["ref_id"]))

    if decoded_type == "ObjectNull":
        return struct.pack("B", RecordTypeEnumeration.ObjectNull.value)

    if decoded_type == "ObjectNullMultiple256":
        count = int(fields["count"])
        if not 0 <= count <= 255:
            raise DNBFYamlExportError("ObjectNullMultiple256 count must be between 0 and 255")
        return struct.pack("BB", RecordTypeEnumeration.ObjectNullMultiple256.value, count)

    if decoded_type == "ObjectNullMultiple":
        count = int(fields["count"])
        if not 0 <= count <= 0x7FFFFFFF:
            raise DNBFYamlExportError("ObjectNullMultiple count must be between 0 and 2147483647")
        return struct.pack("<Bi", RecordTypeEnumeration.ObjectNullMultiple.value, count)

    if decoded_type == "MessageEnd":
        return struct.pack("B", RecordTypeEnumeration.MessageEnd.value)

    if decoded_type in {"ClassWithMembersAndTypes", "SystemClassWithMembersAndTypes", "ClassWithId"}:
        raw = bytearray(_raw_entry_to_bytes(root, entry))
        members = fields.get("members")
        if not isinstance(members, list):
            raise DNBFYamlExportError(f"{decoded_type} decoded fields.members must be a list")

        replacements = []
        for member in members:
            if not isinstance(member, dict) or not member.get("editable"):
                continue
            value_offset = int(member["value_offset"])
            value_size = int(member["value_size"])
            encoded = _encode_class_member_value(member)
            replacements.append((value_offset, value_size, encoded))

        for value_offset, value_size, encoded in sorted(replacements, reverse=True):
            raw[value_offset:value_offset + value_size] = encoded

        return bytes(raw)

    if decoded_type == "MemberPrimitiveTyped":
        primitive_type = _primitive_type_from_field(fields["primitive_type"])
        return (
            struct.pack("BB", RecordTypeEnumeration.MemberPrimitiveTyped.value, primitive_type.value)
            + _encode_primitive_value(fields["value"], primitive_type)
        )

    if decoded_type == "ArraySinglePrimitive":
        primitive_type = _primitive_type_from_field(fields["primitive_type"])
        values = fields.get("values")
        if not isinstance(values, list):
            raise DNBFYamlExportError("ArraySinglePrimitive decoded fields.values must be a list")

        result = bytearray()
        result += struct.pack("B", RecordTypeEnumeration.ArraySinglePrimitive.value)
        result += struct.pack("<ii", int(fields["object_id"]), len(values))
        result += struct.pack("B", primitive_type.value)
        for value in values:
            result += _encode_primitive_value(value, primitive_type)
        return bytes(result)

    if decoded_type in {"ArraySingleString", "ArraySingleObject"}:
        items = fields.get("items")
        if not isinstance(items, list):
            raise DNBFYamlExportError(f"{decoded_type} decoded fields.items must be a list")

        record_type = getattr(RecordTypeEnumeration, decoded_type)
        result = bytearray()
        result += struct.pack("B", record_type.value)
        result += struct.pack("<ii", int(fields["object_id"]), len(items))
        for item in items:
            result += _encode_record_item(item)
        return bytes(result)

    if decoded_type == "BinaryArray":
        return _rebuild_binary_array(fields)

    if decoded_type == "MethodCall":
        return _rebuild_method_call(fields)

    if decoded_type == "MethodReturn":
        return _rebuild_method_return(fields)

    raise DNBFYamlExportError(f"decoded rebuild is not supported for {decoded_type!r}")


class _UnsupportedPrimitive:
    pass


_UNSUPPORTED = _UnsupportedPrimitive()


def _decode_primitive_value(
    raw: bytes,
    offset: int,
    primitive_type: PrimitiveTypeEnumeration,
) -> tuple[Any | _UnsupportedPrimitive, int | None]:
    stream = _deserializing_stream(raw, offset)
    start = stream.tell()

    if primitive_type == PrimitiveTypeEnumeration.Null:
        return None, 0

    if primitive_type == PrimitiveTypeEnumeration.String:
        try:
            value, consumed = _decode_length_prefixed_string(raw, offset)
        except ValueError:
            return _UNSUPPORTED, None
        return value, consumed

    if primitive_type == PrimitiveTypeEnumeration.Decimal:
        try:
            value, consumed = _decode_length_prefixed_string(raw, offset)
        except ValueError:
            return _UNSUPPORTED, None
        return value, consumed

    if primitive_type in (PrimitiveTypeEnumeration.TimeSpan, PrimitiveTypeEnumeration.DateTime):
        if stream.remaining() < 8:
            return _UNSUPPORTED, None
        return stream.read_int64(), stream.tell() - start

    try:
        value = _read_datastream_primitive(stream, primitive_type)
    except (KeyError, ValueError):
        return _UNSUPPORTED, None

    return _primitive_value_to_manifest(value, primitive_type), stream.tell() - start


def _primitive_value_to_manifest(value: Any, primitive_type: PrimitiveTypeEnumeration) -> Any:
    if primitive_type == PrimitiveTypeEnumeration.Boolean:
        return bool(value)
    if primitive_type == PrimitiveTypeEnumeration.Single:
        return float(value)
    if primitive_type == PrimitiveTypeEnumeration.Double:
        return float(value)
    return value


def _encode_primitive_value(value: Any, primitive_type: PrimitiveTypeEnumeration) -> bytes:
    stream = _serializing_stream()

    if primitive_type == PrimitiveTypeEnumeration.Null:
        return b""

    if primitive_type in (PrimitiveTypeEnumeration.String, PrimitiveTypeEnumeration.Decimal):
        return _encode_length_prefixed_string(str(value))

    if primitive_type in (PrimitiveTypeEnumeration.TimeSpan, PrimitiveTypeEnumeration.DateTime):
        stream.write_int64(int(value))
        return bytes(stream)

    try:
        _write_datastream_primitive(stream, primitive_type, value)
        return bytes(stream)
    except (KeyError, ValueError, OverflowError) as exc:
        raise DNBFYamlExportError(f"invalid value for {primitive_type.name}: {value!r}") from exc


def _primitive_type_from_field(value: Any) -> PrimitiveTypeEnumeration:
    if isinstance(value, int):
        return PrimitiveTypeEnumeration(value)
    if isinstance(value, str):
        try:
            return PrimitiveTypeEnumeration[value]
        except KeyError as exc:
            raise DNBFYamlExportError(f"unknown primitive type: {value!r}") from exc
    raise DNBFYamlExportError(f"invalid primitive type field: {value!r}")


def _encode_class_member_value(member: dict[str, Any]) -> bytes:
    member_record_type = member.get("record_type")
    if member_record_type == "BinaryObjectString":
        return BinaryObjectString(int(member["object_id"]), str(member["value"])).to_bytes()
    if member_record_type == "MemberReference":
        return struct.pack("<Bi", RecordTypeEnumeration.MemberReference.value, int(member["ref_id"]))
    if member_record_type in {"ObjectNull", "ObjectNullMultiple256", "ObjectNullMultiple"}:
        return _encode_record_item(member)

    primitive_type = _primitive_type_from_field(member["primitive_type"])
    encoded = _encode_primitive_value(member["value"], primitive_type)
    expected_size = int(member["value_size"])
    if len(encoded) != expected_size:
        raise DNBFYamlExportError(
            f"encoded value size changed for member {member.get('name')!r}: {len(encoded)} != {expected_size}"
        )
    return encoded


def _decode_record_items(
    data: bytes,
    offset: int,
    item_count: int,
    class_defs: dict[int, dict[str, Any]],
) -> tuple[int, list[dict[str, Any]]]:
    cursor = offset
    items = []
    remaining_nulls = 0
    for _ in range(item_count):
        if remaining_nulls:
            items.append({"record_type": "ObjectNull", "editable": False, "from_null_run": True})
            remaining_nulls -= 1
            continue

        item_offset = cursor
        record_type = RecordTypeEnumeration(data[cursor])
        end, _ = _scan_record_end(data, cursor, class_defs)
        item_raw = data[cursor:end]
        item = _decode_record_item(record_type, item_raw, item_offset, end - item_offset)
        items.append(item)
        if record_type == RecordTypeEnumeration.ObjectNullMultiple256:
            remaining_nulls = data[cursor + 1] - 1
        elif record_type == RecordTypeEnumeration.ObjectNullMultiple:
            remaining_nulls = _read_int32(data, cursor + 1) - 1
        cursor = end

    return cursor, items


def _decode_record_item(
    record_type: RecordTypeEnumeration,
    raw: bytes,
    value_offset: int,
    value_size: int,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "record_type": record_type.name,
        "editable": False,
        "value_offset": value_offset,
        "value_size": value_size,
    }

    if record_type == RecordTypeEnumeration.BinaryObjectString:
        decoded = _decode_binary_object_string(raw)
        if decoded is not None:
            fields = decoded["fields"]
            item.update(
                {
                    "editable": True,
                    "object_id": fields["object_id"],
                    "value": fields["value"],
                }
            )
        return item

    if record_type == RecordTypeEnumeration.MemberReference and len(raw) == 5:
        item.update({"editable": True, "ref_id": _read_int32(raw, 1)})
        return item

    if record_type == RecordTypeEnumeration.ObjectNull:
        return item

    if record_type == RecordTypeEnumeration.ObjectNullMultiple256 and len(raw) == 2:
        item.update({"editable": True, "count": raw[1]})
        return item

    if record_type == RecordTypeEnumeration.ObjectNullMultiple and len(raw) == 5:
        item.update({"editable": True, "count": _read_int32(raw, 1)})
        return item

    item["raw_hex"] = raw.hex()
    return item


def _encode_record_item(item: dict[str, Any]) -> bytes:
    record_type_name = item.get("record_type")
    if record_type_name == "BinaryObjectString":
        return BinaryObjectString(int(item["object_id"]), str(item["value"])).to_bytes()
    if record_type_name == "MemberReference":
        return struct.pack("<Bi", RecordTypeEnumeration.MemberReference.value, int(item["ref_id"]))
    if record_type_name == "ObjectNull":
        return struct.pack("B", RecordTypeEnumeration.ObjectNull.value)
    if record_type_name == "ObjectNullMultiple256":
        count = int(item["count"])
        if not 0 <= count <= 255:
            raise DNBFYamlExportError("ObjectNullMultiple256 count must be between 0 and 255")
        return struct.pack("BB", RecordTypeEnumeration.ObjectNullMultiple256.value, count)
    if record_type_name == "ObjectNullMultiple":
        count = int(item["count"])
        if not 0 <= count <= 0x7FFFFFFF:
            raise DNBFYamlExportError("ObjectNullMultiple count must be between 0 and 2147483647")
        return struct.pack("<Bi", RecordTypeEnumeration.ObjectNullMultiple.value, count)

    raw_hex = item.get("raw_hex")
    if not isinstance(raw_hex, str):
        raise DNBFYamlExportError(f"cannot rebuild array item of type {record_type_name!r}")
    return bytes.fromhex(raw_hex)


def _parse_binary_array_header(data: bytes, offset: int) -> dict[str, Any]:
    cursor = offset
    record_type = RecordTypeEnumeration(data[cursor])
    if record_type != RecordTypeEnumeration.BinaryArray:
        raise DNBFYamlExportError(f"expected BinaryArray at offset {offset}")
    cursor += 1

    object_id = _read_int32(data, cursor)
    cursor += 4
    binary_array_type = data[cursor]
    if binary_array_type not in BINARY_ARRAY_TYPE_NAMES:
        raise DNBFYamlExportError(f"unknown BinaryArrayTypeEnumeration: {binary_array_type}")
    cursor += 1

    rank = _read_int32(data, cursor)
    if rank < 0:
        raise DNBFYamlExportError("BinaryArray rank cannot be negative")
    cursor += 4

    lengths = []
    for _ in range(rank):
        length = _read_int32(data, cursor)
        if length < 0:
            raise DNBFYamlExportError("BinaryArray lengths cannot be negative")
        lengths.append(length)
        cursor += 4

    lower_bounds = []
    if binary_array_type in BINARY_ARRAY_TYPES_WITH_LOWER_BOUNDS:
        for _ in range(rank):
            lower_bounds.append(_read_int32(data, cursor))
            cursor += 4

    type_enum = BinaryTypeEnumeration(data[cursor])
    cursor += 1
    additional_type_info, cursor = _parse_binary_type_additional_info(data, cursor, type_enum)

    return {
        "object_id": object_id,
        "binary_array_type": binary_array_type,
        "rank": rank,
        "lengths": lengths,
        "lower_bounds": lower_bounds,
        "type_enum": type_enum,
        "additional_type_info": additional_type_info,
        "members_offset": cursor,
    }


def _parse_binary_type_additional_info(
    data: bytes,
    offset: int,
    type_enum: BinaryTypeEnumeration,
) -> tuple[Any, int]:
    cursor = offset
    if type_enum in (BinaryTypeEnumeration.Primitive, BinaryTypeEnumeration.PrimitiveArray):
        return PrimitiveTypeEnumeration(data[cursor]), cursor + 1
    if type_enum == BinaryTypeEnumeration.SystemClass:
        value, consumed = _decode_length_prefixed_string(data, cursor)
        return value, cursor + consumed
    if type_enum == BinaryTypeEnumeration.Class:
        type_name, consumed = _decode_length_prefixed_string(data, cursor)
        library_id = _read_int32(data, cursor + consumed)
        return {"type_name": type_name, "library_id": library_id}, cursor + consumed + 4
    return None, cursor


def _additional_type_info_to_manifest(value: Any) -> Any:
    if isinstance(value, PrimitiveTypeEnumeration):
        return value.name
    return value


def _rebuild_binary_array(fields: dict[str, Any]) -> bytes:
    binary_array_type = _binary_array_type_from_field(fields["binary_array_type"])
    type_enum = _binary_type_from_field(fields["type_enum"])
    lengths = [int(value) for value in fields["lengths"]]
    lower_bounds = [int(value) for value in fields.get("lower_bounds", [])]
    expected_count = _product(lengths)

    result = bytearray()
    result += struct.pack("B", RecordTypeEnumeration.BinaryArray.value)
    result += struct.pack("<i", int(fields["object_id"]))
    result += struct.pack("B", binary_array_type)
    result += struct.pack("<i", len(lengths))
    for length in lengths:
        if length < 0:
            raise DNBFYamlExportError("BinaryArray lengths cannot be negative")
        result += struct.pack("<i", length)

    if binary_array_type in BINARY_ARRAY_TYPES_WITH_LOWER_BOUNDS:
        if len(lower_bounds) != len(lengths):
            raise DNBFYamlExportError("BinaryArray lower_bounds length must match rank")
        for lower_bound in lower_bounds:
            result += struct.pack("<i", lower_bound)

    result += struct.pack("B", type_enum.value)
    additional_type_info = fields.get("additional_type_info")
    result += _encode_binary_type_additional_info(additional_type_info, type_enum)

    if type_enum in (BinaryTypeEnumeration.Primitive, BinaryTypeEnumeration.PrimitiveArray):
        primitive_type = _primitive_type_from_field(additional_type_info)
        values = fields.get("values")
        if not isinstance(values, list):
            raise DNBFYamlExportError("BinaryArray primitive fields.values must be a list")
        if len(values) != expected_count:
            raise DNBFYamlExportError(f"BinaryArray expected {expected_count} values, got {len(values)}")
        for value in values:
            result += _encode_primitive_value(value, primitive_type)
    else:
        items = fields.get("items")
        if not isinstance(items, list):
            raise DNBFYamlExportError("BinaryArray object fields.items must be a list")
        if len(items) != expected_count:
            raise DNBFYamlExportError(f"BinaryArray expected {expected_count} items, got {len(items)}")
        for item in items:
            result += _encode_record_item(item)

    return bytes(result)


def _encode_binary_type_additional_info(value: Any, type_enum: BinaryTypeEnumeration) -> bytes:
    if type_enum in (BinaryTypeEnumeration.Primitive, BinaryTypeEnumeration.PrimitiveArray):
        return struct.pack("B", _primitive_type_from_field(value).value)
    if type_enum == BinaryTypeEnumeration.SystemClass:
        return _encode_length_prefixed_string(str(value))
    if type_enum == BinaryTypeEnumeration.Class:
        if not isinstance(value, dict):
            raise DNBFYamlExportError("Class additional_type_info must be a mapping")
        return _encode_length_prefixed_string(str(value["type_name"])) + struct.pack("<i", int(value["library_id"]))
    return b""


def _binary_array_type_from_field(value: Any) -> int:
    if isinstance(value, int):
        if value not in BINARY_ARRAY_TYPE_NAMES:
            raise DNBFYamlExportError(f"unknown BinaryArrayTypeEnumeration: {value}")
        return value
    if isinstance(value, str):
        try:
            return BINARY_ARRAY_TYPE_VALUES[value]
        except KeyError as exc:
            raise DNBFYamlExportError(f"unknown BinaryArrayTypeEnumeration: {value!r}") from exc
    raise DNBFYamlExportError(f"invalid BinaryArrayTypeEnumeration field: {value!r}")


def _binary_type_from_field(value: Any) -> BinaryTypeEnumeration:
    if isinstance(value, int):
        return BinaryTypeEnumeration(value)
    if isinstance(value, str):
        try:
            return BinaryTypeEnumeration[value]
        except KeyError as exc:
            raise DNBFYamlExportError(f"unknown BinaryTypeEnumeration: {value!r}") from exc
    raise DNBFYamlExportError(f"invalid BinaryTypeEnumeration field: {value!r}")


def _decode_string_value_with_code(data: bytes, offset: int) -> tuple[str, int]:
    value, cursor = _decode_value_with_code(data, offset)
    if value["primitive_type"] != "String":
        raise DNBFYamlExportError(f"expected String value-with-code at offset {offset}")
    return str(value["value"]), cursor


def _decode_array_of_value_with_code(data: bytes, offset: int) -> tuple[list[dict[str, Any]], int]:
    count = _read_int32(data, offset)
    if count < 0:
        raise DNBFYamlExportError("ArrayOfValueWithCode count cannot be negative")

    cursor = offset + 4
    values = []
    for _ in range(count):
        value, cursor = _decode_value_with_code(data, cursor)
        values.append(value)
    return values, cursor


def _decode_value_with_code(data: bytes, offset: int) -> tuple[dict[str, Any], int]:
    primitive_type = PrimitiveTypeEnumeration(data[offset])
    cursor = offset + 1
    if primitive_type == PrimitiveTypeEnumeration.Null:
        return {"primitive_type": primitive_type.name, "value": None}, cursor
    if primitive_type == PrimitiveTypeEnumeration.String:
        value, consumed = _decode_length_prefixed_string(data, cursor)
        return {"primitive_type": primitive_type.name, "value": value}, cursor + consumed

    value, consumed = _decode_primitive_value(data, cursor, primitive_type)
    if value is _UNSUPPORTED or consumed is None:
        raise DNBFYamlExportError(f"unsupported ValueWithCode primitive type: {primitive_type.name}")

    return {"primitive_type": primitive_type.name, "value": value}, cursor + consumed


def _rebuild_method_call(fields: dict[str, Any]) -> bytes:
    flags = int(fields["message_flags"])
    result = bytearray()
    result += struct.pack("<BI", RecordTypeEnumeration.MethodCall.value, flags)
    result += _encode_value_with_code({"primitive_type": "String", "value": fields["method_name"]})
    result += _encode_value_with_code({"primitive_type": "String", "value": fields["type_name"]})
    if flags & MESSAGE_FLAG_CONTEXT_INLINE:
        result += _encode_value_with_code({"primitive_type": "String", "value": fields["call_context"]})
    if flags & MESSAGE_FLAG_ARGS_INLINE:
        result += _encode_array_of_value_with_code(fields.get("args"))
    return bytes(result)


def _rebuild_method_return(fields: dict[str, Any]) -> bytes:
    flags = int(fields["message_flags"])
    result = bytearray()
    result += struct.pack("<BI", RecordTypeEnumeration.MethodReturn.value, flags)
    if flags & MESSAGE_FLAG_RETURN_VALUE_INLINE:
        result += _encode_value_with_code(fields["return_value"])
    if flags & MESSAGE_FLAG_CONTEXT_INLINE:
        result += _encode_value_with_code({"primitive_type": "String", "value": fields["call_context"]})
    if flags & MESSAGE_FLAG_ARGS_INLINE:
        result += _encode_array_of_value_with_code(fields.get("args"))
    return bytes(result)


def _encode_array_of_value_with_code(values: Any) -> bytes:
    if not isinstance(values, list):
        raise DNBFYamlExportError("ArrayOfValueWithCode must be a list")

    result = bytearray()
    result += struct.pack("<i", len(values))
    for value in values:
        result += _encode_value_with_code(value)
    return bytes(result)


def _encode_value_with_code(value: dict[str, Any]) -> bytes:
    if not isinstance(value, dict):
        raise DNBFYamlExportError("ValueWithCode must be a mapping")

    primitive_type = _primitive_type_from_field(value["primitive_type"])
    result = bytearray()
    result += struct.pack("B", primitive_type.value)
    if primitive_type == PrimitiveTypeEnumeration.Null:
        return bytes(result)
    if primitive_type == PrimitiveTypeEnumeration.String:
        result += _encode_length_prefixed_string(str(value["value"]))
        return bytes(result)
    result += _encode_primitive_value(value["value"], primitive_type)
    return bytes(result)


def _scan_binary_to_record_store(source_path: Path) -> DNBFRecordStore:
    source_bytes = source_path.read_bytes()
    store = DNBFRecordStore(":memory:", source_path=source_path, source_bytes=source_bytes)
    class_defs: dict[int, dict[str, Any]] = {}
    offset = 0

    while offset < len(source_bytes):
        record_type = RecordTypeEnumeration(source_bytes[offset])
        end_offset, metadata = _scan_record_end(source_bytes, offset, class_defs)
        store.add_raw_record(
            record_type=record_type,
            offset=offset,
            raw=source_bytes[offset:end_offset],
            object_id=metadata.get("object_id"),
            library_id=metadata.get("library_id"),
            reference_id=metadata.get("reference_id"),
            metadata_id=metadata.get("metadata_id"),
        )
        offset = end_offset
        if record_type == RecordTypeEnumeration.MessageEnd:
            break

    if offset != len(source_bytes):
        raise DNBFYamlExportError(f"scanner stopped at {offset}, but source length is {len(source_bytes)}")

    return store


def _scan_record_end(
    data: bytes,
    offset: int,
    class_defs: dict[int, dict[str, Any]],
) -> tuple[int, dict[str, int | None]]:
    record_type = RecordTypeEnumeration(data[offset])
    metadata: dict[str, int | None] = {
        "object_id": None,
        "library_id": None,
        "reference_id": None,
        "metadata_id": None,
    }

    if record_type == RecordTypeEnumeration.SerializedStreamHeader:
        return offset + 17, metadata

    if record_type == RecordTypeEnumeration.MessageEnd:
        return offset + 1, metadata

    if record_type == RecordTypeEnumeration.BinaryLibrary:
        library_id = _read_int32(data, offset + 1)
        _, consumed = _decode_length_prefixed_string(data, offset + 5)
        metadata["library_id"] = library_id
        return offset + 5 + consumed, metadata

    if record_type == RecordTypeEnumeration.BinaryObjectString:
        object_id = _read_int32(data, offset + 1)
        _, consumed = _decode_length_prefixed_string(data, offset + 5)
        metadata["object_id"] = object_id
        return offset + 5 + consumed, metadata

    if record_type == RecordTypeEnumeration.MemberReference:
        metadata["reference_id"] = _read_int32(data, offset + 1)
        return offset + 5, metadata

    if record_type == RecordTypeEnumeration.ObjectNull:
        return offset + 1, metadata

    if record_type == RecordTypeEnumeration.ObjectNullMultiple256:
        return offset + 2, metadata

    if record_type == RecordTypeEnumeration.ObjectNullMultiple:
        return offset + 5, metadata

    if record_type == RecordTypeEnumeration.MemberPrimitiveTyped:
        primitive_type = PrimitiveTypeEnumeration(data[offset + 1])
        _, consumed = _decode_primitive_value(data, offset + 2, primitive_type)
        if consumed is None:
            raise DNBFYamlExportError(f"unsupported MemberPrimitiveTyped at offset {offset}")
        return offset + 2 + consumed, metadata

    if record_type == RecordTypeEnumeration.ArraySinglePrimitive:
        object_id = _read_int32(data, offset + 1)
        length = _read_int32(data, offset + 5)
        if length < 0:
            raise DNBFYamlExportError(f"ArraySinglePrimitive length cannot be negative at offset {offset}")
        primitive_type = PrimitiveTypeEnumeration(data[offset + 9])
        size = _primitive_size(primitive_type)
        metadata["object_id"] = object_id
        return offset + 10 + (length * size), metadata

    if record_type == RecordTypeEnumeration.ArraySingleObject:
        object_id = _read_int32(data, offset + 1)
        length = _read_int32(data, offset + 5)
        if length < 0:
            raise DNBFYamlExportError(f"ArraySingleObject length cannot be negative at offset {offset}")
        end = _skip_record_items(data, offset + 9, length, class_defs)
        metadata["object_id"] = object_id
        return end, metadata

    if record_type == RecordTypeEnumeration.ArraySingleString:
        object_id = _read_int32(data, offset + 1)
        length = _read_int32(data, offset + 5)
        if length < 0:
            raise DNBFYamlExportError(f"ArraySingleString length cannot be negative at offset {offset}")
        end = _skip_record_items(data, offset + 9, length, class_defs)
        metadata["object_id"] = object_id
        return end, metadata

    if record_type == RecordTypeEnumeration.BinaryArray:
        header = _parse_binary_array_header(data, offset)
        item_count = _product(header["lengths"])
        if header["type_enum"] in (BinaryTypeEnumeration.Primitive, BinaryTypeEnumeration.PrimitiveArray):
            end = header["members_offset"] + item_count * _primitive_size(header["additional_type_info"])
        else:
            end = _skip_record_items(data, header["members_offset"], item_count, class_defs)
        metadata["object_id"] = header["object_id"]
        return end, metadata

    if record_type == RecordTypeEnumeration.MethodCall:
        decoded = _decode_method_call_from_stream(data, offset)
        return decoded["end_offset"], metadata

    if record_type == RecordTypeEnumeration.MethodReturn:
        decoded = _decode_method_return_from_stream(data, offset)
        return decoded["end_offset"], metadata

    if record_type == RecordTypeEnumeration.ClassWithMembersAndTypes:
        parsed = _parse_class_with_members_and_types(data, offset, class_defs)
        metadata["object_id"] = parsed["object_id"]
        metadata["library_id"] = parsed["library_id"]
        return parsed["end_offset"], metadata

    if record_type == RecordTypeEnumeration.SystemClassWithMembersAndTypes:
        parsed = _parse_class_with_members_and_types(data, offset, class_defs)
        metadata["object_id"] = parsed["object_id"]
        return parsed["end_offset"], metadata

    if record_type == RecordTypeEnumeration.ClassWithId:
        object_id = _read_int32(data, offset + 1)
        metadata_id = _read_int32(data, offset + 5)
        class_def = class_defs.get(metadata_id)
        if class_def is None:
            raise DNBFYamlExportError(f"ClassWithId {object_id} references unknown metadata id {metadata_id}")
        end = _parse_class_members(data, offset + 9, class_def["member_names"], class_def["binary_types"],
                                   class_def["additional_infos"], class_defs)[0]
        metadata["object_id"] = object_id
        metadata["metadata_id"] = metadata_id
        return end, metadata

    raise DNBFYamlExportError(f"scanner does not yet support {record_type.name} at offset {offset}")


def _decode_method_call_from_stream(data: bytes, offset: int) -> dict[str, Any]:
    if data[offset] != RecordTypeEnumeration.MethodCall:
        raise DNBFYamlExportError(f"expected MethodCall at offset {offset}")
    flags = struct.unpack("<I", data[offset + 1:offset + 5])[0]
    cursor = offset + 5
    _, cursor = _decode_string_value_with_code(data, cursor)
    _, cursor = _decode_string_value_with_code(data, cursor)
    if flags & MESSAGE_FLAG_CONTEXT_INLINE:
        _, cursor = _decode_string_value_with_code(data, cursor)
    if flags & MESSAGE_FLAG_ARGS_INLINE:
        _, cursor = _decode_array_of_value_with_code(data, cursor)
    return {"end_offset": cursor}


def _decode_method_return_from_stream(data: bytes, offset: int) -> dict[str, Any]:
    if data[offset] != RecordTypeEnumeration.MethodReturn:
        raise DNBFYamlExportError(f"expected MethodReturn at offset {offset}")
    flags = struct.unpack("<I", data[offset + 1:offset + 5])[0]
    cursor = offset + 5
    if flags & MESSAGE_FLAG_RETURN_VALUE_INLINE:
        _, cursor = _decode_value_with_code(data, cursor)
    if flags & MESSAGE_FLAG_CONTEXT_INLINE:
        _, cursor = _decode_string_value_with_code(data, cursor)
    if flags & MESSAGE_FLAG_ARGS_INLINE:
        _, cursor = _decode_array_of_value_with_code(data, cursor)
    return {"end_offset": cursor}


def _parse_class_with_members_and_types(
    data: bytes,
    offset: int,
    class_defs: dict[int, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if class_defs is None:
        class_defs = {}

    record_type = RecordTypeEnumeration(data[offset])
    cursor = offset + 1
    object_id, class_name, member_names, cursor = _parse_class_info(data, cursor)
    binary_types, additional_infos, cursor = _parse_member_type_info(data, cursor, len(member_names))

    parsed: dict[str, Any] = {
        "record_type": record_type,
        "object_id": object_id,
        "class_name": class_name,
        "member_names": member_names,
        "binary_types": binary_types,
        "additional_infos": additional_infos,
    }

    if record_type == RecordTypeEnumeration.ClassWithMembersAndTypes:
        parsed["library_id"] = _read_int32(data, cursor)
        cursor += 4

    class_defs[object_id] = {
        "member_names": member_names,
        "binary_types": binary_types,
        "additional_infos": additional_infos,
    }

    cursor, members = _parse_class_members(data, cursor, member_names, binary_types, additional_infos, class_defs)
    parsed["members"] = members
    parsed["end_offset"] = cursor
    return parsed


def _parse_class_info(data: bytes, offset: int) -> tuple[int, str, list[str], int]:
    object_id = _read_int32(data, offset)
    class_name, consumed = _decode_length_prefixed_string(data, offset + 4)
    cursor = offset + 4 + consumed
    member_count = _read_int32(data, cursor)
    if member_count < 0:
        raise DNBFYamlExportError("class member count cannot be negative")
    cursor += 4

    member_names = []
    for _ in range(member_count):
        member_name, consumed = _decode_length_prefixed_string(data, cursor)
        member_names.append(member_name)
        cursor += consumed

    return object_id, class_name, member_names, cursor


def _parse_member_type_info(
    data: bytes,
    offset: int,
    member_count: int,
) -> tuple[list[BinaryTypeEnumeration], list[Any], int]:
    cursor = offset
    binary_types = [BinaryTypeEnumeration(data[cursor + index]) for index in range(member_count)]
    cursor += member_count

    additional_infos = []
    for binary_type in binary_types:
        if binary_type in (BinaryTypeEnumeration.Primitive, BinaryTypeEnumeration.PrimitiveArray):
            additional_infos.append(PrimitiveTypeEnumeration(data[cursor]))
            cursor += 1
        elif binary_type == BinaryTypeEnumeration.SystemClass:
            value, consumed = _decode_length_prefixed_string(data, cursor)
            additional_infos.append(value)
            cursor += consumed
        elif binary_type == BinaryTypeEnumeration.Class:
            type_name, consumed = _decode_length_prefixed_string(data, cursor)
            library_id = _read_int32(data, cursor + consumed)
            additional_infos.append({"type_name": type_name, "library_id": library_id})
            cursor += consumed + 4
        else:
            additional_infos.append(None)

    return binary_types, additional_infos, cursor


def _parse_class_members(
    data: bytes,
    offset: int,
    member_names: list[str],
    binary_types: list[BinaryTypeEnumeration],
    additional_infos: list[Any],
    class_defs: dict[int, dict[str, Any]],
) -> tuple[int, list[dict[str, Any]]]:
    cursor = offset
    members = []
    remaining_nulls = 0
    for member_name, binary_type, additional_info in zip(member_names, binary_types, additional_infos, strict=True):
        member: dict[str, Any] = {
            "name": member_name,
            "binary_type": binary_type,
            "offset": cursor,
        }
        if additional_info is not None:
            member["additional_info"] = additional_info
        if remaining_nulls:
            member["null_from_run"] = True
            remaining_nulls -= 1
        elif binary_type == BinaryTypeEnumeration.Primitive:
            primitive_type = additional_info
            value, consumed = _decode_primitive_value(data, cursor, primitive_type)
            if value is _UNSUPPORTED or consumed is None:
                raise DNBFYamlExportError(f"unsupported primitive member {member_name!r}")
            member.update(
                {
                    "primitive_type": primitive_type,
                    "value": value,
                    "value_offset": cursor,
                    "value_size": consumed,
                }
            )
            cursor += consumed
        else:
            member["record_type"] = RecordTypeEnumeration(data[cursor]).name
            record_type = RecordTypeEnumeration(data[cursor])
            cursor, _ = _scan_record_end(data, cursor, class_defs)
            member["value_offset"] = member["offset"]
            member["value_size"] = cursor - member["offset"]
            item = _decode_record_item(record_type, data[member["offset"]:cursor], member["offset"], member["value_size"])
            if item.get("editable"):
                member.update(item)
            if record_type == RecordTypeEnumeration.ObjectNullMultiple256:
                remaining_nulls = data[member["offset"] + 1] - 1
            elif record_type == RecordTypeEnumeration.ObjectNullMultiple:
                remaining_nulls = _read_int32(data, member["offset"] + 1) - 1

        member["end_offset"] = cursor
        members.append(member)

    return cursor, members


def _skip_record_items(
    data: bytes,
    offset: int,
    item_count: int,
    class_defs: dict[int, dict[str, Any]],
) -> int:
    cursor = offset
    remaining_nulls = 0
    for _ in range(item_count):
        if remaining_nulls:
            remaining_nulls -= 1
            continue

        record_type = RecordTypeEnumeration(data[cursor])
        end, _ = _scan_record_end(data, cursor, class_defs)
        if record_type == RecordTypeEnumeration.ObjectNull:
            remaining_nulls = 0
        elif record_type == RecordTypeEnumeration.ObjectNullMultiple256:
            remaining_nulls = data[cursor + 1] - 1
        elif record_type == RecordTypeEnumeration.ObjectNullMultiple:
            remaining_nulls = _read_int32(data, cursor + 1) - 1
        cursor = end

    return cursor


def _read_int32(data: bytes, offset: int) -> int:
    return struct.unpack_from("<i", data, offset)[0]


def _primitive_size(primitive_type: PrimitiveTypeEnumeration) -> int:
    if primitive_type not in PRIMITIVE_FORMATS:
        raise DNBFYamlExportError(f"unsupported primitive type: {primitive_type.name}")
    return PRIMITIVE_FORMATS[primitive_type][1]


def _product(values: list[int]) -> int:
    result = 1
    for value in values:
        result *= value
    return result


def _decode_length_prefixed_string(data: bytes, offset: int) -> tuple[str, int]:
    length, prefix_size = _decode_7bit_length(data, offset)
    value_offset = offset + prefix_size
    end_offset = value_offset + length
    if end_offset > len(data):
        raise ValueError("length-prefixed string extends beyond data")

    return bytes(data[value_offset:end_offset]).decode("utf-8"), prefix_size + length


def _encode_length_prefixed_string(value: str) -> bytes:
    encoded = value.encode("utf-8")
    return _encode_7bit_length(len(encoded)) + encoded


def _decode_7bit_length(data: bytes, offset: int) -> tuple[int, int]:
    result = 0
    shift = 0

    for bytes_read in range(1, 6):
        current_offset = offset + bytes_read - 1
        if current_offset >= len(data):
            raise ValueError("7-bit encoded length extends beyond data")

        byte = data[current_offset]
        result |= (byte & 0x7F) << shift
        if (byte & 0x80) == 0:
            if bytes_read == 5 and byte > 0x07:
                raise ValueError("7-bit encoded length exceeds Int32 range")
            return result, bytes_read

        shift += 7

    raise ValueError("7-bit encoded length uses more than 5 bytes")


def _encode_7bit_length(length: int) -> bytes:
    if not 0 <= length <= 0x7FFFFFFF:
        raise DNBFYamlExportError(f"length out of range: {length}")

    stream = _serializing_stream()
    remaining = length
    while remaining >= 0x80:
        stream.write_uint8((remaining & 0x7F) | 0x80)
        remaining >>= 7
    stream.write_uint8(remaining)
    return bytes(stream)


def _load_manifest(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("format") != EXPORT_FORMAT:
        raise DNBFYamlExportError(f"unsupported export format: {data.get('format')!r}")
    if data.get("version") != EXPORT_VERSION:
        raise DNBFYamlExportError(f"unsupported export version: {data.get('version')!r}")
    if not isinstance(data.get("records"), list):
        raise DNBFYamlExportError("export manifest is missing records")
    return data


def _dump_yaml_compatible_json(value: dict[str, Any]) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False) + "\n"


def _record_relative_dir(record_entry: dict[str, Any], records_dir_name: str) -> str:
    record_type = str(record_entry["record_type"])
    sequence_dir = _record_dir_name(int(record_entry["sequence"]), record_type)
    category = _record_category(record_type)
    parent = _record_parent_dir(record_entry, category)
    if parent is None:
        return f"{records_dir_name}/{category}/{sequence_dir}"
    return f"{records_dir_name}/{category}/{parent}/{sequence_dir}"


def _record_category(record_type: str) -> str:
    if record_type in {"SerializedStreamHeader", "MessageEnd"}:
        return "control"
    if record_type == "BinaryLibrary":
        return "libraries"
    if record_type in {
        "BinaryObjectString",
        "ClassWithId",
        "ClassWithMembers",
        "ClassWithMembersAndTypes",
        "SystemClassWithMembers",
        "SystemClassWithMembersAndTypes",
    }:
        return "objects"
    if record_type in {"BinaryArray", "ArraySingleObject", "ArraySinglePrimitive", "ArraySingleString"}:
        return "arrays"
    if record_type in {"MemberReference", "ObjectNull", "ObjectNullMultiple", "ObjectNullMultiple256"}:
        return "references"
    if record_type == "MemberPrimitiveTyped":
        return "primitives"
    if record_type in {"MethodCall", "MethodReturn"}:
        return "methods"
    if record_type == "UnparsedStream":
        return "unparsed"
    return "other"


def _record_parent_dir(record_entry: dict[str, Any], category: str) -> str | None:
    decoded = record_entry.get("decoded")
    fields = decoded.get("fields") if isinstance(decoded, dict) else None
    if not isinstance(fields, dict):
        fields = {}

    if category == "objects":
        object_id = _first_not_none(record_entry.get("object_id"), fields.get("object_id"))
        label = fields.get("class_name") or fields.get("value") or record_entry["record_type"]
        return _id_label_dir("object", object_id, str(label))

    if category == "arrays":
        object_id = _first_not_none(record_entry.get("object_id"), fields.get("object_id"))
        label = fields.get("primitive_type") or fields.get("type_enum") or record_entry["record_type"]
        return _id_label_dir("array", object_id, str(label))

    if category == "libraries":
        library_id = _first_not_none(record_entry.get("library_id"), fields.get("library_id"))
        label = fields.get("library_name") or record_entry["record_type"]
        return _id_label_dir("library", library_id, str(label))

    if category == "references" and record_entry["record_type"] == "MemberReference":
        reference_id = _first_not_none(record_entry.get("reference_id"), fields.get("ref_id"))
        return _id_label_dir("ref", reference_id, "MemberReference")

    return None


def _id_label_dir(prefix: str, identifier: Any, label: str) -> str:
    if identifier is None:
        id_part = "unknown"
    else:
        id_part = str(int(identifier))
        if not id_part.startswith("-"):
            id_part = id_part.zfill(6)
    return f"{prefix}_{id_part}_{_safe_path_part(label)}"


def _first_not_none(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return None


def _record_dir_name(sequence: int, record_type: str) -> str:
    return f"{sequence:06d}_{_safe_path_part(record_type)}"


def _safe_path_part(value: str, max_length: int = 80) -> str:
    safe = "".join(char if char.isalnum() or char in ("-", "_", ".") else "_" for char in value)
    safe = safe.strip("._")
    if not safe:
        safe = "value"
    return safe[:max_length]


def _metadata_int(value: str | None) -> int | None:
    if value is None:
        return None
    return int(value)


def _deserializing_stream(data: bytes, offset: int = 0) -> DeserializingStream:
    stream = DeserializingStream(data, byteorder=ByteOrder.LITTLE_ENDIAN)
    stream.seek(offset)
    return stream


def _serializing_stream() -> SerializingStream:
    return SerializingStream(byteorder=ByteOrder.LITTLE_ENDIAN)


def _read_datastream_primitive(stream: DeserializingStream, primitive_type: PrimitiveTypeEnumeration) -> Any:
    readers = {
        PrimitiveTypeEnumeration.Boolean: stream.read_bool,
        PrimitiveTypeEnumeration.Byte: stream.read_uint8,
        PrimitiveTypeEnumeration.Char: stream.read_uint16,
        PrimitiveTypeEnumeration.Double: stream.read_double,
        PrimitiveTypeEnumeration.Int16: stream.read_int16,
        PrimitiveTypeEnumeration.Int32: stream.read_int32,
        PrimitiveTypeEnumeration.Int64: stream.read_int64,
        PrimitiveTypeEnumeration.SByte: stream.read_int8,
        PrimitiveTypeEnumeration.Single: stream.read_float,
        PrimitiveTypeEnumeration.UInt16: stream.read_uint16,
        PrimitiveTypeEnumeration.UInt32: stream.read_uint32,
        PrimitiveTypeEnumeration.UInt64: stream.read_uint64,
    }
    return readers[primitive_type]()


def _write_datastream_primitive(
    stream: SerializingStream,
    primitive_type: PrimitiveTypeEnumeration,
    value: Any,
) -> None:
    writers = {
        PrimitiveTypeEnumeration.Boolean: lambda v: stream.write_bool(bool(v)),
        PrimitiveTypeEnumeration.Byte: lambda v: stream.write_uint8(int(v)),
        PrimitiveTypeEnumeration.Char: lambda v: stream.write_uint16(int(v)),
        PrimitiveTypeEnumeration.Double: lambda v: stream.write_double(float(v)),
        PrimitiveTypeEnumeration.Int16: lambda v: stream.write_int16(int(v)),
        PrimitiveTypeEnumeration.Int32: lambda v: stream.write_int32(int(v)),
        PrimitiveTypeEnumeration.Int64: lambda v: stream.write_int64(int(v)),
        PrimitiveTypeEnumeration.SByte: lambda v: stream.write_int8(int(v)),
        PrimitiveTypeEnumeration.Single: lambda v: stream.write_float(float(v)),
        PrimitiveTypeEnumeration.UInt16: lambda v: stream.write_uint16(int(v)),
        PrimitiveTypeEnumeration.UInt32: lambda v: stream.write_uint32(int(v)),
        PrimitiveTypeEnumeration.UInt64: lambda v: stream.write_uint64(int(v)),
    }
    writers[primitive_type](value)
