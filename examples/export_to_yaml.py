from __future__ import annotations

import argparse
import sys
from pathlib import Path

try:
    from dnbflib import export_dnbf_to_yaml, rebuild_yaml_export
except ModuleNotFoundError:
    repo_src = Path(__file__).resolve().parents[1] / "src"
    if not repo_src.exists():
        raise
    sys.path.insert(0, str(repo_src))
    from dnbflib import export_dnbf_to_yaml, rebuild_yaml_export


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export a .NET BinaryFormatter / NRBF binary to a lossless YAML package."
    )
    parser.add_argument("input", type=Path, help="Path to the input DNBF/NRBF binary.")
    parser.add_argument("export_dir", type=Path, help="Directory where manifest.yaml and raw sidecars are written.")
    parser.add_argument(
        "--database",
        type=Path,
        default=None,
        help="Optional SQLite database path for the parsed record store.",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Rebuild the export and compare it against the input bytes.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    manifest_path = export_dnbf_to_yaml(
        args.input,
        args.export_dir,
        database_path=args.database,
        recreate_database=args.database is not None,
    )
    print(f"wrote manifest: {manifest_path}")

    if not args.verify:
        return

    source = args.input.read_bytes()
    rebuilt = rebuild_yaml_export(args.export_dir)
    if rebuilt != source:
        raise RuntimeError("export rebuild did not match the input bytes")

    print(f"verified lossless rebuild: {len(rebuilt)} bytes")


if __name__ == "__main__":
    main()
