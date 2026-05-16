from pathlib import Path
import unittest

import dnbflib
from dnbflib import DNBFDocument


class PublicApiTests(unittest.TestCase):
    def test_package_declares_inline_types(self) -> None:
        package_root = Path(dnbflib.__file__).parent

        self.assertTrue((package_root / "py.typed").is_file())

    def test_document_export_exposes_typed_methods_at_runtime(self) -> None:
        self.assertEqual(DNBFDocument.open.__annotations__["path"], "str | Path")
        self.assertEqual(DNBFDocument.open.__annotations__["return"], "DNBFDocument")


if __name__ == "__main__":
    unittest.main()
