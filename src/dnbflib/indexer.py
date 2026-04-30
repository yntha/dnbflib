from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

from dnbflib.records import Record, RecordTypeEnumeration


@dataclass(frozen=True)
class StoredRecord:
    """A record row stored in a DNBF SQLite database."""

    sequence: int
    record_type: RecordTypeEnumeration
    offset: int
    size: int
    raw: bytes
    object_id: int | None = None
    library_id: int | None = None
    reference_id: int | None = None
    metadata_id: int | None = None
    repr_text: str | None = None

    @property
    def end_offset(self) -> int:
        return self.offset + self.size


class DNBFRecordStore:
    """SQLite-backed storage for all records in a BinaryFormatter stream."""

    SCHEMA_VERSION = 1

    def __init__(
        self,
        db_path: str | Path,
        *,
        source_path: str | Path | None = None,
        source_bytes: bytes | None = None,
        recreate: bool = False,
    ) -> None:
        self.db_path = Path(db_path)
        self._is_memory = str(db_path) == ":memory:"
        if not self._is_memory:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            if recreate and self.db_path.exists():
                self.db_path.unlink()

        self.db = sqlite3.connect(":memory:" if self._is_memory else self.db_path)
        self.db.row_factory = sqlite3.Row
        self._create_schema()

        self.set_metadata("schema_version", str(self.SCHEMA_VERSION))
        if source_path is not None:
            self.set_metadata("source_path", str(source_path))
        if source_bytes is not None:
            self.set_metadata("source_sha256", sha256(source_bytes).hexdigest())
            self.set_metadata("source_size", str(len(source_bytes)))

    @classmethod
    def for_binary_path(
        cls,
        dnbf_path: str | Path,
        *,
        db_path: str | Path | None = None,
        recreate: bool = False,
    ) -> DNBFRecordStore:
        """Create a store for a source binary, defaulting to a content-hash database name."""
        source_path = Path(dnbf_path)
        source_bytes = source_path.read_bytes()
        if db_path is None:
            db_path = source_path.with_name(f"{source_path.stem}.{sha256(source_bytes).hexdigest()[:16]}.dnbf.sqlite3")

        return cls(db_path, source_path=source_path, source_bytes=source_bytes, recreate=recreate)

    def close(self) -> None:
        self.db.close()

    def commit(self) -> None:
        self.db.commit()

    def clear_records(self) -> None:
        self.db.execute("DELETE FROM records")
        self.db.commit()

    def set_metadata(self, key: str, value: str) -> None:
        self.db.execute(
            """
            INSERT INTO metadata (key, value)
            VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )
        self.db.commit()

    def get_metadata(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM metadata WHERE key = ?", (key,)).fetchone()
        if row is None:
            return None
        return str(row["value"])

    def add_record(self, record: Record, *, offset: int, size: int, raw: bytes) -> StoredRecord:
        """Persist a parsed record and its original bytes."""
        object_id = self._extract_object_id(record)
        library_id = self._extract_int_attr(record, "library_id")
        reference_id = self._extract_int_attr(record, "ref_id")
        metadata_id = self._extract_int_attr(record, "metadata_id")
        record_type = self._record_type(record)
        repr_text = repr(record)

        cursor = self.db.execute(
            """
            INSERT INTO records (
                offset,
                size,
                record_type,
                record_type_name,
                object_id,
                library_id,
                reference_id,
                metadata_id,
                raw,
                repr_text
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                offset,
                size,
                int(record_type),
                record_type.name,
                object_id,
                library_id,
                reference_id,
                metadata_id,
                raw,
                repr_text,
            ),
        )
        self.db.commit()

        return StoredRecord(
            sequence=int(cursor.lastrowid),
            record_type=record_type,
            offset=offset,
            size=size,
            raw=raw,
            object_id=object_id,
            library_id=library_id,
            reference_id=reference_id,
            metadata_id=metadata_id,
            repr_text=repr_text,
        )

    def add_raw_record(
        self,
        *,
        record_type: RecordTypeEnumeration | int,
        offset: int,
        raw: bytes,
        object_id: int | None = None,
        library_id: int | None = None,
        reference_id: int | None = None,
        metadata_id: int | None = None,
        repr_text: str | None = None,
    ) -> StoredRecord:
        """Persist a record when only its raw bytes and metadata are known."""
        record_type_enum = RecordTypeEnumeration(record_type)
        cursor = self.db.execute(
            """
            INSERT INTO records (
                offset,
                size,
                record_type,
                record_type_name,
                object_id,
                library_id,
                reference_id,
                metadata_id,
                raw,
                repr_text
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                offset,
                len(raw),
                int(record_type_enum),
                record_type_enum.name,
                object_id,
                library_id,
                reference_id,
                metadata_id,
                raw,
                repr_text,
            ),
        )
        self.db.commit()

        return StoredRecord(
            sequence=int(cursor.lastrowid),
            record_type=record_type_enum,
            offset=offset,
            size=len(raw),
            raw=raw,
            object_id=object_id,
            library_id=library_id,
            reference_id=reference_id,
            metadata_id=metadata_id,
            repr_text=repr_text,
        )

    def iter_records(self) -> Iterator[StoredRecord]:
        rows = self.db.execute(
            """
            SELECT sequence, record_type, offset, size, raw, object_id, library_id, reference_id, metadata_id, repr_text
            FROM records
            ORDER BY sequence
            """
        )
        for row in rows:
            yield self._stored_record_from_row(row)

    def count(self) -> int:
        row = self.db.execute("SELECT COUNT(*) AS count FROM records").fetchone()
        return int(row["count"])

    def get_by_sequence(self, sequence: int) -> StoredRecord:
        row = self.db.execute(
            """
            SELECT sequence, record_type, offset, size, raw, object_id, library_id, reference_id, metadata_id, repr_text
            FROM records
            WHERE sequence = ?
            """,
            (sequence,),
        ).fetchone()
        if row is None:
            raise KeyError(f"No record found for sequence {sequence}")
        return self._stored_record_from_row(row)

    def get_by_object_id(self, object_id: int) -> StoredRecord:
        row = self.db.execute(
            """
            SELECT sequence, record_type, offset, size, raw, object_id, library_id, reference_id, metadata_id, repr_text
            FROM records
            WHERE object_id = ?
            ORDER BY sequence
            LIMIT 1
            """,
            (object_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"No record found for object_id {object_id}")
        return self._stored_record_from_row(row)

    def get_object_offset(self, object_id: int) -> int:
        return self.get_by_object_id(object_id).offset

    def to_bytes(self) -> bytes:
        """Reconstruct the stream by concatenating stored raw records in sequence order."""
        return b"".join(record.raw for record in self.iter_records())

    def _create_schema(self) -> None:
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS records (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                offset INTEGER NOT NULL,
                size INTEGER NOT NULL,
                record_type INTEGER NOT NULL,
                record_type_name TEXT NOT NULL,
                object_id INTEGER,
                library_id INTEGER,
                reference_id INTEGER,
                metadata_id INTEGER,
                raw BLOB NOT NULL,
                repr_text TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_records_offset ON records(offset);
            CREATE INDEX IF NOT EXISTS idx_records_object_id ON records(object_id);
            CREATE INDEX IF NOT EXISTS idx_records_library_id ON records(library_id);
            CREATE INDEX IF NOT EXISTS idx_records_reference_id ON records(reference_id);
            """
        )
        self.db.commit()

    def _stored_record_from_row(self, row: sqlite3.Row) -> StoredRecord:
        return StoredRecord(
            sequence=int(row["sequence"]),
            record_type=RecordTypeEnumeration(int(row["record_type"])),
            offset=int(row["offset"]),
            size=int(row["size"]),
            raw=bytes(row["raw"]),
            object_id=self._nullable_int(row["object_id"]),
            library_id=self._nullable_int(row["library_id"]),
            reference_id=self._nullable_int(row["reference_id"]),
            metadata_id=self._nullable_int(row["metadata_id"]),
            repr_text=row["repr_text"],
        )

    @staticmethod
    def _nullable_int(value: Any) -> int | None:
        if value is None:
            return None
        return int(value)

    @staticmethod
    def _extract_int_attr(record: Record, attr: str) -> int | None:
        value = getattr(record, attr, None)
        if isinstance(value, int):
            return value
        return None

    @classmethod
    def _extract_object_id(cls, record: Record) -> int | None:
        direct = cls._extract_int_attr(record, "object_id")
        if direct is not None:
            return direct

        class_info = getattr(record, "class_info", None)
        class_object_id = getattr(class_info, "object_id", None)
        if isinstance(class_object_id, int):
            return class_object_id

        array_info = getattr(record, "array_info", None)
        array_object_id = getattr(array_info, "object_id", None)
        if isinstance(array_object_id, int):
            return array_object_id

        return None

    @staticmethod
    def _record_type(record: Record) -> RecordTypeEnumeration:
        record_type = getattr(record, "record_type", None)
        if record_type is None:
            raise ValueError("Record must have a record_type attribute")
        return RecordTypeEnumeration(record_type)

