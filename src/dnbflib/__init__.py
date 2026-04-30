__all__ = [
    "AmbiguousMemberError",
    "AmbiguousObjectError",
    "BinaryObjectString",
    "BinaryTypeEnumeration",
    "DNBFDocument",
    "DNBFDocumentError",
    "DNBFMemberNode",
    "DNBFObjectNode",
    "DNBFRecordStore",
    "DNBFWriter",
    "MemberNotFoundError",
    "ObjectNotFoundError",
    "PrimitiveTypeEnumeration",
    "RecordTypeEnumeration",
    "StoredRecord",
    "export_dnbf_to_yaml",
    "export_record_store_to_yaml",
    "rebuild_yaml_export",
]


def __getattr__(name: str):
    """Lazy load module attributes for better performance and to avoid circular imports."""
    if name in {"DNBFRecordStore", "StoredRecord"}:
        from dnbflib.indexer import DNBFRecordStore, StoredRecord  # noqa: PLC0415

        return {
            "DNBFRecordStore": DNBFRecordStore,
            "StoredRecord": StoredRecord,
        }[name]

    if name == "DNBFWriter":
        from dnbflib.writer import DNBFWriter  # noqa: PLC0415

        return DNBFWriter

    if name in {
        "BinaryObjectString",
        "BinaryTypeEnumeration",
        "PrimitiveTypeEnumeration",
        "RecordTypeEnumeration",
    }:
        from dnbflib.records import (  # noqa: PLC0415
            BinaryObjectString,
            BinaryTypeEnumeration,
            PrimitiveTypeEnumeration,
            RecordTypeEnumeration,
        )

        return {
            "BinaryObjectString": BinaryObjectString,
            "BinaryTypeEnumeration": BinaryTypeEnumeration,
            "PrimitiveTypeEnumeration": PrimitiveTypeEnumeration,
            "RecordTypeEnumeration": RecordTypeEnumeration,
        }[name]

    if name in {
        "DNBFDocument",
        "DNBFDocumentError",
        "DNBFMemberNode",
        "DNBFObjectNode",
        "ObjectNotFoundError",
        "AmbiguousObjectError",
        "MemberNotFoundError",
        "AmbiguousMemberError",
    }:
        from dnbflib.document import (  # noqa: PLC0415
            AmbiguousMemberError,
            AmbiguousObjectError,
            DNBFDocument,
            DNBFDocumentError,
            DNBFMemberNode,
            DNBFObjectNode,
            MemberNotFoundError,
            ObjectNotFoundError,
        )

        return {
            "DNBFDocument": DNBFDocument,
            "DNBFDocumentError": DNBFDocumentError,
            "DNBFMemberNode": DNBFMemberNode,
            "DNBFObjectNode": DNBFObjectNode,
            "ObjectNotFoundError": ObjectNotFoundError,
            "AmbiguousObjectError": AmbiguousObjectError,
            "MemberNotFoundError": MemberNotFoundError,
            "AmbiguousMemberError": AmbiguousMemberError,
        }[name]

    if name in {"export_dnbf_to_yaml", "export_record_store_to_yaml", "rebuild_yaml_export"}:
        from dnbflib.yaml_export import (  # noqa: PLC0415
            export_dnbf_to_yaml,
            export_record_store_to_yaml,
            rebuild_yaml_export,
        )

        return {
            "export_dnbf_to_yaml": export_dnbf_to_yaml,
            "export_record_store_to_yaml": export_record_store_to_yaml,
            "rebuild_yaml_export": rebuild_yaml_export,
        }[name]

    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
