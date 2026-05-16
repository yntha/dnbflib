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


class IncrementalRecordScanner:
    """Scan BinaryFormatter record boundaries one record at a time."""

    def __init__(self, source: bytes | mmap.mmap) -> None:
        self.source = source
        self.class_defs: dict[int, dict] = {}
        self.offset = 0
        self.sequence = 1
        self.complete = len(source) == 0

    def scan_next(self) -> IndexedRecord | None:
        """Scan and return the next record, or ``None`` after the stream is complete."""
        if self.complete:
            return None

        record_type = RecordTypeEnumeration(self.source[self.offset])
        end_offset, metadata = _scan_record_end(self.source, self.offset, self.class_defs)
        record = IndexedRecord(
            sequence=self.sequence,
            record_type=record_type,
            offset=self.offset,
            size=end_offset - self.offset,
            object_id=metadata.get("object_id"),
            library_id=metadata.get("library_id"),
            reference_id=metadata.get("reference_id"),
            metadata_id=metadata.get("metadata_id"),
        )

        self.sequence += 1
        self.offset = end_offset
        if record_type == RecordTypeEnumeration.MessageEnd:
            if self.offset != len(self.source):
                raise DNBFScannerError(f"scanner stopped at {self.offset}, but source length is {len(self.source)}")
            self.complete = True
        elif self.offset >= len(self.source):
            self.complete = True

        return record

    def scan_remaining(self) -> list[IndexedRecord]:
        """Scan all records remaining in the stream."""
        records: list[IndexedRecord] = []
        while not self.complete:
            record = self.scan_next()
            if record is not None:
                records.append(record)
        return records


def scan_records(source: bytes | mmap.mmap) -> list[IndexedRecord]:
    """Scan a BinaryFormatter stream into offset-only record metadata."""
    scanner = IncrementalRecordScanner(source)
    records = scanner.scan_remaining()

    if scanner.offset != len(source):
        raise DNBFScannerError(f"scanner stopped at {scanner.offset}, but source length is {len(source)}")

    return records
