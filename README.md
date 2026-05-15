# dnbflib

`dnbflib` reads, indexes, edits, exports, and rebuilds .NET BinaryFormatter / NRBF
binary streams in Python.

## Warning

BinaryFormatter is unsafe for untrusted data. Use this library for inspection,
migration, recovery, and editing of files you already trust. Do not accept arbitrary
BinaryFormatter payloads from users or the network.

## Features

- Memory-conscious object graph traversal with `DNBFDocument`.
- Editing of supported primitive, string, reference, object, and array values.
- Creation of new class instances from existing object templates.
- Creation and mutation of typed one-dimensional arrays.
- Lossless YAML export and rebuild with raw binary sidecars.
- SQLite-backed record storage through `DNBFRecordStore`.
- Stream rebuilding through `DNBFWriter`.

## Requirements

- Python 3.11 or newer.
- `pydatastreams`

## Installation

From PyPI, once published:

```console
pip install dnbflib
```

From a local checkout:

```console
python -m pip install -e .
```

## Documentation

Full usage documentation and tutorials are available in [docs/index.html](docs/index.html).

## License

`dnbflib` is distributed under the terms of the [MIT](https://spdx.org/licenses/MIT.html) license.
