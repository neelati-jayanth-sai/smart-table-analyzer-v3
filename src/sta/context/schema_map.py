from pydantic import BaseModel


class ColumnInfo(BaseModel):
    name: str
    type: str
    field_id: int | None = None


IDENTIFIER_NAMES = {"id", "uuid", "guid"}


def is_identifier_like(name: str) -> bool:
    n = name.lower()
    return n in IDENTIFIER_NAMES or n.endswith("_id") or n.endswith("_uuid") or n.endswith("_guid")


def classify_column(column: ColumnInfo) -> str:
    name = column.name.lower()
    typ = column.type.lower()
    if is_identifier_like(name):
        return "identifier-like"
    # Nested type strings (list<string>, struct<street: string>, ...) contain
    # primitive tokens, so the complex check must run before primitive ones.
    if any(token in typ for token in ("struct", "list", "map", "array")):
        return "complex"
    if any(token in typ for token in ("timestamp", "date", "time")):
        return "temporal"
    if any(token in typ for token in ("int", "long", "float", "double", "decimal", "numeric", "real")):
        return "numeric"
    if any(token in typ for token in ("string", "varchar", "char", "text")):
        return "string"
    if "bool" in typ:
        return "boolean"
    if any(token in typ for token in ("binary", "bytes")):
        return "binary"
    return "other"


def group_columns(columns: list[ColumnInfo]) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {
        "identifier-like": [],
        "temporal": [],
        "numeric": [],
        "string": [],
        "boolean": [],
        "binary": [],
        "complex": [],
        "other": [],
    }
    for column in columns:
        groups[classify_column(column)].append(column.name)
    return {key: values for key, values in groups.items() if values}
