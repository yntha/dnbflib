from __future__ import annotations

import unittest

from dnbflib import DNBFRecordStore, DNBFWriter, RecordTypeEnumeration


class RecordStoreTests(unittest.TestCase):
    def test_stores_raw_records_and_reconstructs_bytes(self) -> None:
        store = DNBFRecordStore(":memory:")
        store.add_raw_record(record_type=RecordTypeEnumeration.SerializedStreamHeader, offset=0, raw=b"\x00header")
        store.add_raw_record(record_type=RecordTypeEnumeration.MessageEnd, offset=7, raw=b"\x0b")

        self.assertEqual(store.count(), 2)
        self.assertEqual(store.get_by_sequence(1).offset, 0)
        self.assertEqual(store.get_by_sequence(2).end_offset, 8)
        self.assertEqual(store.to_bytes(), b"\x00header\x0b")
        self.assertEqual(DNBFWriter.from_record_store(store).to_bytes(), b"\x00header\x0b")


if __name__ == "__main__":
    unittest.main()
