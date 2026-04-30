from __future__ import annotations

import mmap
from dataclasses import dataclass

from dnbflib.records import RecordTypeEnumeration
from dnbflib.yaml_export import _scan_record_end


@dataclass(frozen=True)
class IndexedRecord:
    """Offset-only metadata for a BinaryFormatter record."""

    sequence: int
    record_type: RecordTypeEnumeration
    offset: int
    size: int
    object_id: int | None = None
    library_id: int | None = None
    reference_id: int | None = None
    metadata_id: int | None = None

    @property
    def end_offset(self) -> int:
        return self.offset + self.size


class DNBFScannerError(Exception):
    """Raised when record boundary scanning fails."""


def scan_records(source: bytes | mmap.mmap) -> list[IndexedRecord]:
    """Scan a BinaryFormatter stream into offset-only record metadata."""
    records: list[IndexedRecord] = []
    class_defs: dict[int, dict] = {}
    offset = 0
    sequence = 1

    while offset < len(source):
        record_type = RecordTypeEnumeration(source[offset])
        end_offset, metadata = _scan_record_end(source, offset, class_defs)
        records.append(
            IndexedRecord(
                sequence=sequence,
                record_type=record_type,
                offset=offset,
                size=end_offset - offset,
                object_id=metadata.get("object_id"),
                library_id=metadata.get("library_id"),
                reference_id=metadata.get("reference_id"),
                metadata_id=metadata.get("metadata_id"),
            )
        )
        sequence += 1
        offset = end_offset
        if record_type == RecordTypeEnumeration.MessageEnd:
            break

    if offset != len(source):
        raise DNBFScannerError(f"scanner stopped at {offset}, but source length is {len(source)}")

    return records
