from __future__ import annotations

import mmap
from collections.abc import Callable
from pathlib import Path
from typing import Any, BinaryIO

from dnbflib.decoded import decode_supported_record, encode_class_member_value, encode_supported_record
from dnbflib.indexer import DNBFRecordStore, StoredRecord
from dnbflib.records import (
    BinaryObjectString,
    ClassWithId,
    MemberReference,
    ObjectNull,
    PrimitiveTypeEnumeration,
    RecordTypeEnumeration,
    encode_primitive_value,
)
from dnbflib.scanner import IndexedRecord, scan_records

_UNDECODED = object()
_ARRAY_RECORD_TYPES = {
    RecordTypeEnumeration.ArraySinglePrimitive,
    RecordTypeEnumeration.ArraySingleObject,
    RecordTypeEnumeration.ArraySingleString,
    RecordTypeEnumeration.BinaryArray,
}


class DNBFDocumentError(Exception):
    """Base error for object graph ops."""


class ObjectNotFoundError(DNBFDocumentError, LookupError):
    """Raised when an object cant be found."""


class AmbiguousObjectError(DNBFDocumentError, LookupError):
    """Raised when a lookup expected one object but found multiple."""


class MemberNotFoundError(DNBFDocumentError, LookupError):
    """Raised when a member cant be found on an object."""


class AmbiguousMemberError(DNBFDocumentError, LookupError):
    """Raised when a member lookup matches more than one member."""


class DNBFDocument:
    """Editable object-graph view over a DNBF stream."""

    def __init__(
        self,
        source: bytes | mmap.mmap,
        records: list[IndexedRecord],
        *,
        source_file: BinaryIO | None = None,
        source_map: mmap.mmap | None = None,
    ) -> None:
        self._source = source
        self._source_file = source_file
        self._source_map = source_map
        self._records = records
        self._entries: list[dict[str, Any]] = []
        self._objects_by_id: dict[int, DNBFObjectNode | DNBFArrayNode] = {}
        self._dirty_sequences: set[int] = set()
        self._decode_context: dict[int, dict[str, Any]] = {}
        self._insertions_before_sequence: dict[int, list[bytes]] = {}
        self._pending_object_entries: list[dict[str, Any]] = []
        self._pending_raw_by_sequence: dict[int, bytes] = {}
        self._reserved_object_ids: set[int] = set()
        self._load_graph()

    @classmethod
    def open(cls, path: str | Path) -> DNBFDocument:
        """Open a DNBF stream as an editable object graph using an mmap-backed source."""
        source_file = Path(path).open("rb")
        try:
            source_map = mmap.mmap(source_file.fileno(), 0, access=mmap.ACCESS_READ)
        except Exception:
            source_file.close()
            raise

        try:
            return cls(source_map, scan_records(source_map), source_file=source_file, source_map=source_map)
        except Exception:
            source_map.close()
            source_file.close()
            raise

    @classmethod
    def from_record_store(cls, record_store: DNBFRecordStore) -> DNBFDocument:
        """Create a document from an existing record store."""
        source_bytes = record_store.to_bytes()
        records = [
            IndexedRecord(
                sequence=record.sequence,
                record_type=record.record_type,
                offset=record.offset,
                size=record.size,
                object_id=record.object_id,
                library_id=record.library_id,
                reference_id=record.reference_id,
                metadata_id=record.metadata_id,
            )
            for record in record_store.iter_records()
        ]
        return cls(source_bytes, records)

    def close(self) -> None:
        """Close any mmap/file handles owned by this document."""
        if self._source_map is not None:
            self._source_map.close()
            self._source_map = None
        if self._source_file is not None:
            self._source_file.close()
            self._source_file = None

    def __enter__(self) -> DNBFDocument:
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def objects(self, class_name: str | None = None) -> list[DNBFObjectNode]:
        """Return object nodes, optionally filtered by class name."""
        nodes = [node for node in self._objects_by_id.values() if isinstance(node, DNBFObjectNode)]
        if class_name is None:
            return nodes
        return [node for node in nodes if node.matches_class(class_name)]

    def object(self, object_id: int) -> DNBFObjectNode | DNBFArrayNode:
        """Return an object or array node by DNBF object id."""
        try:
            return self._objects_by_id[int(object_id)]
        except KeyError as exc:
            raise ObjectNotFoundError(f"object_id {object_id} was not found") from exc

    def find_class(self, class_name: str) -> DNBFObjectNode:
        """Return the only object matching ``class_name``; raise if ambiguous."""
        return self.one(class_name=class_name)

    def one(
        self,
        *,
        class_name: str | None = None,
        where: Callable[[DNBFObjectNode], bool] | None = None,
    ) -> DNBFObjectNode:
        """Return exactly one object matching the filters."""
        nodes = self.objects(class_name)
        if where is not None:
            nodes = [node for node in nodes if where(node)]

        if not nodes:
            description = class_name or "object"
            raise ObjectNotFoundError(f"no {description!r} object matched")
        if len(nodes) > 1:
            matches = ", ".join(f"object_id={node.object_id} class={node.class_name!r}" for node in nodes[:10])
            raise AmbiguousObjectError(f"found {len(nodes)} matching objects: {matches}")
        return nodes[0]

    def to_bytes(self) -> bytes:
        """Rebuild the current stream bytes, including in-memory object edits."""
        replacements = {
            entry["record"].sequence: self._entry_to_bytes(entry)
            for entry in self._entries
            if entry["record"].sequence in self._dirty_sequences
        }
        chunks: list[bytes] = []
        for record in self._records:
            chunks.extend(self._insertions_before_sequence.get(record.sequence, []))
            chunks.append(replacements.get(record.sequence, self._record_raw(record)))
        return b"".join(chunks)

    def write(self, path: str | Path) -> None:
        """Write rebuilt bytes to ``path``."""
        replacements = {
            entry["record"].sequence: self._entry_to_bytes(entry)
            for entry in self._entries
            if entry["record"].sequence in self._dirty_sequences
        }
        with Path(path).open("wb") as output:
            for record in self._records:
                for inserted in self._insertions_before_sequence.get(record.sequence, []):
                    output.write(inserted)
                output.write(replacements.get(record.sequence, self._record_raw(record)))

    def _load_graph(self) -> None:
        for record in self._records:
            entry = {
                "record": record,
                "decoded": _UNDECODED,
            }
            self._entries.append(entry)

            if record.object_id is not None:
                self._objects_by_id[int(record.object_id)] = self._node_for_entry(entry, int(record.object_id))

    def _node_for_entry(self, entry: dict[str, Any], object_id: int) -> DNBFObjectNode | DNBFArrayNode:
        record: IndexedRecord = entry["record"]
        if record.record_type in _ARRAY_RECORD_TYPES:
            return DNBFArrayNode(self, entry, object_id)
        return DNBFObjectNode(self, entry, object_id)

    def _mark_dirty(self, entry: dict[str, Any]) -> None:
        record: IndexedRecord = entry["record"]
        self._dirty_sequences.add(record.sequence)

    def _next_object_id(self) -> int:
        used_ids = set(self._objects_by_id) | self._reserved_object_ids
        for entry in self._pending_object_entries:
            object_id = entry["record"].object_id
            if object_id is not None:
                used_ids.add(int(object_id))
        for entry in self._entries:
            decoded = entry.get("decoded")
            if decoded is not _UNDECODED:
                _collect_decoded_object_ids(decoded, used_ids)
        if not used_ids:
            return 1
        return max(used_ids) + 1

    def _allocate_object_id(self) -> int:
        object_id = self._next_object_id()
        self._reserved_object_ids.add(object_id)
        return object_id

    def _message_end_sequence(self) -> int:
        for record in reversed(self._records):
            if record.record_type == RecordTypeEnumeration.MessageEnd:
                return record.sequence
        raise DNBFDocumentError("unable to insert records because the stream has no MessageEnd record")

    def _append_object_record(
        self,
        *,
        record_type: RecordTypeEnumeration,
        raw: bytes,
        object_id: int,
        decoded: dict[str, Any] | None = None,
        metadata_id: int | None = None,
    ) -> DNBFObjectNode:
        sequence = max(record.sequence for record in self._records) + len(self._pending_object_entries) + 1
        insertion_sequence = self._message_end_sequence()
        record = IndexedRecord(
            sequence=sequence,
            record_type=record_type,
            offset=-1,
            size=len(raw),
            object_id=object_id,
            metadata_id=metadata_id,
        )
        entry = {"record": record, "decoded": decoded}
        self._entries.append(entry)
        self._pending_object_entries.append(entry)
        self._pending_raw_by_sequence[sequence] = raw
        self._insertions_before_sequence.setdefault(insertion_sequence, []).append(raw)
        self._reserved_object_ids.add(object_id)

        node = DNBFObjectNode(self, entry, object_id)
        self._objects_by_id[object_id] = node
        return node

    def _create_instance_from_template(
        self,
        template: DNBFObjectNode,
        values: dict[str, Any] | None = None,
    ) -> DNBFObjectNode:
        values = dict(values or {})
        fields = template._fields
        members = fields.get("members")
        if not isinstance(members, list):
            raise DNBFDocumentError(f"{template!r} does not expose decoded class members")

        metadata_id = _template_metadata_id(template)
        if metadata_id is None:
            raise DNBFDocumentError(f"{template!r} cannot be used as an instance template")

        _validate_member_values(template, members, values)
        object_id = self._allocate_object_id()
        member_bytes, decoded_members = self._encode_new_instance_members(members, values)

        raw = ClassWithId(object_id=object_id, metadata_id=metadata_id, member_bytes=member_bytes).to_bytes()
        decoded_fields: dict[str, Any] = {
            "object_id": object_id,
            "metadata_id": metadata_id,
            "members": decoded_members,
        }
        class_name = template.class_name or _class_name_for_metadata(self, metadata_id)
        if class_name is not None:
            decoded_fields["class_name"] = class_name

        return self._append_object_record(
            record_type=RecordTypeEnumeration.ClassWithId,
            raw=raw,
            object_id=object_id,
            decoded={
                "editable": any(member.get("editable") for member in decoded_members),
                "type": "ClassWithId",
                "fields": decoded_fields,
            },
            metadata_id=metadata_id,
        )

    def _encode_new_instance_members(
        self,
        members: list[Any],
        values: dict[str, Any],
    ) -> tuple[bytes, list[dict[str, Any]]]:
        payload = bytearray()
        decoded_members: list[dict[str, Any]] = []
        cursor = 9
        for member in members:
            if not isinstance(member, dict):
                raise DNBFDocumentError("template contains an invalid member entry")
            value_supplied, value = _pop_member_value(values, member.get("name", ""))
            encoded, decoded_member = self._encode_new_instance_member(member, value_supplied, value)
            decoded_member["value_offset"] = cursor
            decoded_member["value_size"] = len(encoded)
            payload += encoded
            cursor += len(encoded)
            decoded_members.append(decoded_member)
        return bytes(payload), decoded_members

    def _encode_new_instance_member(
        self,
        member: dict[str, Any],
        value_supplied: bool,
        value: Any,
    ) -> tuple[bytes, dict[str, Any]]:
        name = str(member.get("name", ""))
        binary_type = str(member.get("binary_type", ""))
        decoded_member: dict[str, Any] = {
            "name": name,
            "binary_type": binary_type,
            "editable": True,
        }

        primitive_type_name = member.get("primitive_type")
        if primitive_type_name is not None:
            primitive_type = _primitive_type_from_field(primitive_type_name)
            member_value = value if value_supplied else member.get("value")
            encoded = encode_primitive_value(member_value, primitive_type)
            decoded_member["primitive_type"] = primitive_type.name
            decoded_member["value"] = member_value
            return encoded, decoded_member

        if not value_supplied:
            value = _default_reference_value(member)

        if value is None:
            decoded_member["record_type"] = "ObjectNull"
            return ObjectNull().to_bytes(), decoded_member

        if isinstance(value, str) and binary_type == "String":
            object_id = self._allocate_object_id()
            decoded_member["record_type"] = "BinaryObjectString"
            decoded_member["object_id"] = object_id
            decoded_member["value"] = value
            return BinaryObjectString(object_id, value).to_bytes(), decoded_member

        ref_id = _reference_id_from_value(value)
        if ref_id is not None:
            decoded_member["record_type"] = "MemberReference"
            decoded_member["ref_id"] = ref_id
            return MemberReference(ref_id).to_bytes(), decoded_member

        record_type = member.get("record_type")
        raise DNBFDocumentError(
            f"member {name!r} cannot be encoded for a new instance "
            f"(binary_type={binary_type!r}, record_type={record_type!r})"
        )

    def _entry_to_bytes(self, entry: dict[str, Any]) -> bytes:
        decoded = self._ensure_decoded(entry)
        if not isinstance(decoded, dict):
            return self._record_raw(entry["record"])

        fields = decoded.get("fields")
        if not isinstance(fields, dict):
            return self._record_raw(entry["record"])

        record: IndexedRecord = entry["record"]
        if record.record_type in _ARRAY_RECORD_TYPES:
            return encode_supported_record(decoded)

        members = fields.get("members")
        if not isinstance(members, list):
            return self._record_raw(entry["record"])

        raw = bytearray(self._record_raw(entry["record"]))
        replacements = []
        for member in members:
            if not isinstance(member, dict) or not member.get("editable"):
                continue
            encoded = encode_class_member_value(member)
            replacements.append((int(member["value_offset"]), int(member["value_size"]), encoded))

        for value_offset, value_size, encoded in sorted(replacements, reverse=True):
            raw[value_offset:value_offset + value_size] = encoded

        return bytes(raw)

    def _ensure_decoded(self, entry: dict[str, Any]) -> dict[str, Any] | None:
        decoded = entry.get("decoded")
        if decoded is not _UNDECODED:
            return decoded

        target_sequence = entry["record"].sequence
        for candidate in self._entries:
            if candidate["record"].sequence > target_sequence:
                break
            if candidate.get("decoded") is not _UNDECODED:
                continue

            try:
                candidate["decoded"] = decode_supported_record(
                    self._decode_record_view(candidate["record"]),
                    self._decode_context,
                )
            except Exception:
                candidate["decoded"] = None

            object_id = _entry_object_id(candidate["record"], candidate["decoded"])
            if object_id is not None and object_id not in self._objects_by_id:
                self._objects_by_id[object_id] = self._node_for_entry(candidate, object_id)

        decoded = entry.get("decoded")
        if decoded is _UNDECODED:
            return None
        return decoded

    def _record_raw(self, record: IndexedRecord) -> bytes:
        if record.offset < 0:
            try:
                return self._pending_raw_by_sequence[record.sequence]
            except KeyError as exc:
                raise DNBFDocumentError(f"inserted record {record.sequence} has no raw bytes!!") from exc
        return self._source[record.offset:record.end_offset]

    def _decode_record_view(self, record: IndexedRecord) -> StoredRecord:
        return StoredRecord(
            sequence=record.sequence,
            record_type=record.record_type,
            offset=record.offset,
            size=record.size,
            raw=self._record_raw(record),
            object_id=record.object_id,
            library_id=record.library_id,
            reference_id=record.reference_id,
            metadata_id=record.metadata_id,
        )


class DNBFObjectNode:
    """A decoded object instance in a ``DNBFDocument``."""

    def __init__(self, document: DNBFDocument, entry: dict[str, Any], object_id: int) -> None:
        self.document = document
        self._entry = entry
        self.object_id = object_id

    @property
    def record(self) -> IndexedRecord:
        return self._entry["record"]

    @property
    def record_type(self) -> str:
        return self.record.record_type.name

    @property
    def class_name(self) -> str | None:
        fields = self._fields
        class_name = fields.get("class_name")
        if isinstance(class_name, str):
            return class_name
        decoded = self._decoded
        if isinstance(decoded, dict):
            decoded_type = decoded.get("type")
            if isinstance(decoded_type, str):
                return decoded_type
        return None

    @property
    def _decoded(self) -> dict[str, Any] | None:
        return self.document._ensure_decoded(self._entry)

    @property
    def _fields(self) -> dict[str, Any]:
        decoded = self._decoded
        if not isinstance(decoded, dict):
            return {}
        fields = decoded.get("fields")
        if not isinstance(fields, dict):
            return {}
        return fields

    def matches_class(self, class_name: str) -> bool:
        actual = self.class_name
        if actual is None:
            return False
        if actual == class_name:
            return True
        return actual.lower() == class_name.lower() or actual.lower().endswith(f".{class_name.lower()}")

    def members(self) -> list[DNBFMemberNode]:
        """Return decoded members for this object."""
        members = self._fields.get("members")
        if not isinstance(members, list):
            return []
        return [DNBFMemberNode(self, member) for member in members if isinstance(member, dict)]

    def member(self, name: str) -> DNBFMemberNode:
        """Find a member by exact name, backing-field name, or case-insensitive alias."""
        matches = [member for member in self.members() if member.matches(name)]
        if not matches:
            raise MemberNotFoundError(f"{self!r} has no member {name!r}")
        if len(matches) > 1:
            names = ", ".join(member.name for member in matches)
            raise AmbiguousMemberError(f"{self!r} member {name!r} matched: {names}")
        return matches[0]

    def preview(self) -> dict[str, Any]:
        """Return a compact mapping of member names to scalar values or references."""
        result: dict[str, Any] = {}
        for member in self.members():
            if member.is_reference:
                result[member.display_name] = {"ref_id": member.ref_id}
            elif member.is_editable:
                result[member.display_name] = member.value
        return result

    def new_instance(self, values: dict[str, Any] | None = None, **kwargs: Any) -> DNBFObjectNode:
        """Create a new instance using this object's class metadata as the template."""
        merged_values = dict(values or {})
        merged_values.update(kwargs)
        return self.document._create_instance_from_template(self, merged_values)

    def __repr__(self) -> str:
        return f"<DNBFObjectNode object_id={self.object_id} class_name={self.class_name!r}>"


class DNBFArrayNode:
    """A decoded array instance in a ``DNBFDocument``."""

    def __init__(self, document: DNBFDocument, entry: dict[str, Any], object_id: int) -> None:
        self.document = document
        self._entry = entry
        self.object_id = object_id

    @property
    def record(self) -> IndexedRecord:
        return self._entry["record"]

    @property
    def record_type(self) -> str:
        return self.record.record_type.name

    @property
    def _decoded(self) -> dict[str, Any] | None:
        return self.document._ensure_decoded(self._entry)

    @property
    def _fields(self) -> dict[str, Any]:
        decoded = self._decoded
        if not isinstance(decoded, dict):
            return {}
        fields = decoded.get("fields")
        if not isinstance(fields, dict):
            return {}
        return fields

    def __len__(self) -> int:
        return len(self._items())

    def __getitem__(self, index: int) -> Any:
        return self._items()[index]

    def __setitem__(self, index: int, value: Any) -> None:
        fields = self._fields
        values = fields.get("values")
        if isinstance(values, list):
            values[index] = value
            self.document._mark_dirty(self._entry)
            return

        items = fields.get("items")
        if isinstance(items, list):
            items[index] = _array_item_from_value(self.document, items[index], value)
            self.document._mark_dirty(self._entry)
            return

        raise DNBFDocumentError(f"{self!r} does not expose editable array items")

    def __iter__(self):
        return iter(self._items())

    def items(self) -> list[Any]:
        """Return decoded array values with references followed where possible."""
        return self._items()

    def to_list(self) -> list[Any]:
        """Return decoded array values with references followed where possible."""
        return self._items()

    def _items(self) -> list[Any]:
        fields = self._fields
        values = fields.get("values")
        if isinstance(values, list):
            return list(values)

        items = fields.get("items")
        if isinstance(items, list):
            return [_array_item_value(self.document, item) for item in items]

        raise DNBFDocumentError(f"{self!r} does not expose decoded array items")

    def __repr__(self) -> str:
        return f"<DNBFArrayNode object_id={self.object_id} record_type={self.record_type!r}>"


class DNBFMemberNode:
    """A decoded member on a ``DNBFObjectNode``."""

    def __init__(self, owner: DNBFObjectNode, member: dict[str, Any]) -> None:
        self.owner = owner
        self._member = member

    @property
    def name(self) -> str:
        return str(self._member.get("name", ""))

    @property
    def display_name(self) -> str:
        return _display_member_name(self.name)

    @property
    def is_editable(self) -> bool:
        return bool(self._member.get("editable"))

    @property
    def is_reference(self) -> bool:
        return self._member.get("record_type") == "MemberReference" and self.ref_id is not None

    @property
    def ref_id(self) -> int | None:
        value = self._member.get("ref_id")
        if value is None:
            return None
        return int(value)

    @property
    def value(self) -> Any:
        if self.is_reference:
            return self.ref_id
        return self._member.get("value")

    def matches(self, name: str) -> bool:
        wanted = _member_name_aliases(name)
        available = _member_name_aliases(self.name)
        return not wanted.isdisjoint(available)

    def deref(self) -> DNBFObjectNode | DNBFArrayNode:
        """Follow a ``MemberReference`` and return the referenced object or array node."""
        if self.ref_id is None:
            raise ObjectNotFoundError(f"member {self.name!r} is not a reference")
        return self.owner.document.object(self.ref_id)

    def set(self, value: Any) -> None:
        """Set this member's decoded value and mark the owning record dirty."""
        if not self.is_editable:
            raise DNBFDocumentError(f"member {self.name!r} is not editable")

        if self.is_reference:
            ref_id = _reference_id_from_value(value)
            self._member["ref_id"] = ref_id if ref_id is not None else int(value)
        else:
            self._member["value"] = value

        self.owner.document._mark_dirty(self.owner._entry)

    def __repr__(self) -> str:
        return f"<DNBFMemberNode {self.owner.object_id}.{self.name}>"


def _entry_object_id(record: IndexedRecord, decoded: dict[str, Any] | None) -> int | None:
    if record.object_id is not None:
        return int(record.object_id)
    if not isinstance(decoded, dict):
        return None
    fields = decoded.get("fields")
    if not isinstance(fields, dict):
        return None
    object_id = fields.get("object_id")
    if object_id is None:
        return None
    return int(object_id)


def _collect_decoded_object_ids(decoded: Any, used_ids: set[int]) -> None:
    if isinstance(decoded, dict):
        for key, value in decoded.items():
            if key == "object_id" and value is not None:
                used_ids.add(int(value))
            else:
                _collect_decoded_object_ids(value, used_ids)
    elif isinstance(decoded, list):
        for item in decoded:
            _collect_decoded_object_ids(item, used_ids)


def _template_metadata_id(template: DNBFObjectNode) -> int | None:
    fields = template._fields
    metadata_id = fields.get("metadata_id")
    if metadata_id is not None:
        return int(metadata_id)

    record_type = template.record.record_type
    if record_type in {
        RecordTypeEnumeration.ClassWithMembersAndTypes,
        RecordTypeEnumeration.SystemClassWithMembersAndTypes,
    }:
        object_id = fields.get("object_id", template.object_id)
        return int(object_id)
    return None


def _class_name_for_metadata(document: DNBFDocument, metadata_id: int) -> str | None:
    metadata_node = document._objects_by_id.get(metadata_id)
    if metadata_node is None:
        return None
    return metadata_node.class_name


def _pop_member_value(values: dict[str, Any], name: Any) -> tuple[bool, Any]:
    aliases = _member_name_aliases(str(name))
    for key in list(values):
        if str(key) in aliases or str(key).lower() in aliases:
            return True, values.pop(key)
    return False, None


def _validate_member_values(template: DNBFObjectNode, members: list[Any], values: dict[str, Any]) -> None:
    available: set[str] = set()
    for member in members:
        if isinstance(member, dict):
            available.update(_member_name_aliases(str(member.get("name", ""))))

    unknown = [str(key) for key in values if str(key) not in available and str(key).lower() not in available]
    if unknown:
        names = ", ".join(unknown)
        raise MemberNotFoundError(f"{template!r} has no members matching: {names}")


def _primitive_type_from_field(value: Any) -> PrimitiveTypeEnumeration:
    if isinstance(value, PrimitiveTypeEnumeration):
        return value
    if isinstance(value, int):
        return PrimitiveTypeEnumeration(value)
    if isinstance(value, str):
        try:
            return PrimitiveTypeEnumeration[value]
        except KeyError as exc:
            raise DNBFDocumentError(f"unknown primitive type: {value!r}") from exc
    raise DNBFDocumentError(f"invalid primitive type: {value!r}")


def _default_reference_value(member: dict[str, Any]) -> Any:
    if member.get("record_type") == "MemberReference":
        return member.get("ref_id")
    if member.get("record_type") == "BinaryObjectString":
        return member.get("value")
    return None


def _reference_id_from_value(value: Any) -> int | None:
    if isinstance(value, (DNBFObjectNode, DNBFArrayNode)):
        return value.object_id
    if isinstance(value, int) and not isinstance(value, bool):
        return int(value)
    return None


def _array_item_value(document: DNBFDocument, item: Any) -> Any:
    if not isinstance(item, dict):
        return item

    record_type = item.get("record_type")
    if record_type == "MemberReference" and item.get("ref_id") is not None:
        return document.object(int(item["ref_id"]))
    if record_type == "BinaryObjectString":
        return item.get("value")
    if record_type == "ObjectNull":
        return None
    return item


def _array_item_from_value(document: DNBFDocument, current_item: Any, value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)

    if value is None:
        return {"record_type": "ObjectNull", "editable": False}

    if isinstance(value, str):
        object_id = None
        if isinstance(current_item, dict) and current_item.get("record_type") == "BinaryObjectString":
            object_id = current_item.get("object_id")
        if object_id is None:
            object_id = document._allocate_object_id()
        return {
            "record_type": "BinaryObjectString",
            "editable": True,
            "object_id": int(object_id),
            "value": value,
        }

    ref_id = _reference_id_from_value(value)
    if ref_id is not None:
        return {
            "record_type": "MemberReference",
            "editable": True,
            "ref_id": ref_id,
        }

    raise DNBFDocumentError(f"array item cannot be set to {value!r}")


def _member_name_aliases(name: str) -> set[str]:
    display = _display_member_name(name)
    return {
        name,
        name.lower(),
        display,
        display.lower(),
        f"<{display}>k__BackingField",
        f"<{display}>k__BackingField".lower(),
    }


def _display_member_name(name: str) -> str:
    prefix = "<"
    suffix = ">k__BackingField"
    if name.startswith(prefix) and name.endswith(suffix):
        return name[len(prefix):-len(suffix)]
    return name
