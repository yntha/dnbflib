from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from dnbflib.indexer import DNBFRecordStore, StoredRecord
from dnbflib.records import Record


WritableRecord = Record | StoredRecord | bytes | bytearray


class DNBFWriter:
    """Write BinaryFormatter streams from parsed records or database-backed raw records."""

    def __init__(self, records: Iterable[WritableRecord] | None = None) -> None:
        self.records: list[WritableRecord] = list(records or [])

    @classmethod
    def from_record_store(cls, record_store: DNBFRecordStore) -> DNBFWriter:
        return cls(record_store.iter_records())

    def append(self, record: WritableRecord) -> None:
        self.records.append(record)

    def extend(self, records: Iterable[WritableRecord]) -> None:
        self.records.extend(records)

    def to_bytes(self) -> bytes:
        return b"".join(self._record_to_bytes(record) for record in self.records)

    def write_path(self, path: str | Path) -> None:
        Path(path).write_bytes(self.to_bytes())

    @staticmethod
    def _record_to_bytes(record: WritableRecord) -> bytes:
        if isinstance(record, StoredRecord):
            return record.raw

        if isinstance(record, bytes):
            return record

        if isinstance(record, bytearray):
            return bytes(record)

        return record.to_bytes()
