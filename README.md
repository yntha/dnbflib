# dnbflib

`dnbflib` reads, indexes, edits, exports, and rebuilds .NET BinaryFormatter / NRBF
binary streams in Python.

## Warning
BinaryFormatter is unsafe for untrusted data. Use this library for inspection, migration, recovery, and editing of files you already trust. Do not accept arbitrary BinaryFormatter payloads from users or the network.

## Status

The current maintained APIs are:

- `DNBFDocument` for memory-conscious object graph traversal and edits.
- `export_dnbf_to_yaml` / `rebuild_yaml_export` for lossless editable YAML packages.
- `DNBFRecordStore` for SQLite-backed record storage and inspection.
- `DNBFWriter` for rebuilding streams from raw records or writable record objects.

-----

## Requirements

- Python 3.11 or newer.
- `pydatastreams`, installed automatically by package managers.

## Installation

From PyPI, once published:

```console
pip install dnbflib
```

From a local checkout:

```console
python -m pip install -e .
```

## Object traversal

```python
from dnbflib import DNBFDocument

with DNBFDocument.open("save.dat") as doc:
    life = doc.one(class_name="Life", where=lambda obj: obj.member("Name").value == "Alex")
    finances = life.member("Finances").deref()
    finances.member("BankBalance").set(123456)
    doc.write("edited.dat")
```

`DNBFDocument.open()` uses an mmap-backed source file and an offset index. Prefer
`doc.write(...)` over `doc.to_bytes()` for large files because it streams output chunks.

If more than one object matches a class name, use `one(..., where=...)` to disambiguate:

```python
with DNBFDocument.open("save.dat") as doc:
    life = doc.one(class_name="Life", where=lambda obj: obj.member("Name").value == "Alex")
```

## YAML export

```python
from dnbflib import export_dnbf_to_yaml, rebuild_yaml_export

export_dnbf_to_yaml("save.dat", "save_export")
rebuilt = rebuild_yaml_export("save_export")
```

The export is lossless by default. It writes a `manifest.yaml` index and nested per-record
directories containing `record.yaml`, `raw.bin`, and decoded sidecar files when useful.

The repository also includes a command-line export example:

```console
python examples/export_to_yaml.py save.dat save_export --verify
```

## Record Store

```python
from dnbflib import DNBFRecordStore, DNBFWriter, RecordTypeEnumeration

store = DNBFRecordStore(":memory:", source_bytes=b"\x0b")
store.add_raw_record(record_type=RecordTypeEnumeration.MessageEnd, offset=0, raw=b"\x0b")

rebuilt = DNBFWriter.from_record_store(store).to_bytes()
assert rebuilt == b"\x0b"
```

## Public API

The top-level `dnbflib` package exports:

- `DNBFDocument`, `DNBFObjectNode`, `DNBFMemberNode`
- `DNBFRecordStore`, `StoredRecord`
- `DNBFWriter`
- `export_dnbf_to_yaml`, `export_record_store_to_yaml`, `rebuild_yaml_export`
- `RecordTypeEnumeration`, `BinaryTypeEnumeration`, `PrimitiveTypeEnumeration`
- `BinaryObjectString`
- traversal errors such as `ObjectNotFoundError`, `AmbiguousObjectError`, `MemberNotFoundError`, and `AmbiguousMemberError`

Further details on this library are available in [docs/index.html](docs/index.html).

## License

`dnbflib` is distributed under the terms of the [MIT](https://spdx.org/licenses/MIT.html) license.
