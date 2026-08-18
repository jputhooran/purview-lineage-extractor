"""Purview custom process type definitions."""

from __future__ import annotations

from typing import Any, Mapping

SSIS_PROCESS_TYPE = "poc_ssis_package"
STORED_PROCEDURE_PROCESS_TYPE = "poc_sql_stored_procedure"

BUILTIN_PROCESS_TYPE_DEFINITIONS: dict[str, dict[str, Any]] = {
    SSIS_PROCESS_TYPE: {
        "description": "SSIS package and data-flow process for lineage.",
        "attributes": [
            {"name": "packageName", "type_name": "string"},
            {"name": "transformExpressions", "type_name": "string"},
            {"name": "packageFile", "type_name": "string"},
            {"name": "columnMapping", "type_name": "string"},
        ],
    },
    STORED_PROCEDURE_PROCESS_TYPE: {
        "description": "SQL stored-procedure process for lineage.",
        "attributes": [
            {"name": "procedureName", "type_name": "string"},
            {"name": "procedureSchema", "type_name": "string"},
            {"name": "databaseName", "type_name": "string"},
            {"name": "serverName", "type_name": "string"},
            {"name": "definitionHash", "type_name": "string"},
            {"name": "statementType", "type_name": "string"},
            {"name": "joinConditions", "type_name": "string"},
            {"name": "transformExpressions", "type_name": "string"},
            {"name": "columnMapping", "type_name": "string"},
        ],
    },
}


def attribute_definition(name: str, type_name: str) -> dict[str, Any]:
    return {
        "name": name,
        "typeName": type_name,
        "cardinality": "SINGLE",
        "isOptional": True,
        "isUnique": False,
        "isIndexable": False,
    }


def parse_process_type_definitions(
    configured: Mapping[str, Any] | None,
) -> dict[str, dict[str, Any]]:
    definitions = {
        name: {
            "description": value["description"],
            "attributes": [dict(item) for item in value["attributes"]],
        }
        for name, value in BUILTIN_PROCESS_TYPE_DEFINITIONS.items()
    }
    if configured is not None and not isinstance(configured, Mapping):
        raise ValueError(
            "Purview process_type_definitions must be an object."
        )
    for name, value in (configured or {}).items():
        if not isinstance(value, Mapping):
            raise ValueError(
                f"Purview process type '{name}' definition must be an object."
            )
        description = value.get("description")
        attributes = value.get("attributes")
        if not isinstance(description, str) or not description.strip():
            raise ValueError(
                f"Purview process type '{name}' requires a description."
            )
        if not isinstance(attributes, list):
            raise ValueError(
                f"Purview process type '{name}' attributes must be an array."
            )
        normalized_attributes = []
        for item in attributes:
            if not isinstance(item, Mapping):
                raise ValueError(
                    f"Purview process type '{name}' attributes must be objects."
                )
            attribute_name = item.get("name")
            type_name = item.get("type_name") or item.get("typeName")
            if not isinstance(attribute_name, str) or not attribute_name:
                raise ValueError(
                    f"Purview process type '{name}' has an unnamed attribute."
                )
            if not isinstance(type_name, str) or not type_name:
                raise ValueError(
                    f"Purview attribute '{name}.{attribute_name}' needs a type."
                )
            normalized_attributes.append(
                {"name": attribute_name, "type_name": type_name}
            )
        attribute_names = [item["name"] for item in normalized_attributes]
        if len(attribute_names) != len(set(attribute_names)):
            raise ValueError(
                f"Purview process type '{name}' has duplicate attributes."
            )
        reserved = {"qualifiedName", "name", "inputs", "outputs"}
        redefined = reserved.intersection(attribute_names)
        if redefined:
            raise ValueError(
                f"Purview process type '{name}' cannot redefine inherited "
                f"attributes: {', '.join(sorted(redefined))}"
            )
        key = str(name)
        if key in definitions:
            existing = {
                item["name"]: item["type_name"]
                for item in definitions[key]["attributes"]
            }
            conflicts = [
                item["name"]
                for item in normalized_attributes
                if item["name"] in existing
                and existing[item["name"]] != item["type_name"]
            ]
            if conflicts:
                raise ValueError(
                    f"Purview process type '{name}' changes built-in "
                    f"attribute types: {', '.join(sorted(conflicts))}"
                )
            definitions[key]["attributes"].extend(
                item
                for item in normalized_attributes
                if item["name"] not in existing
            )
        else:
            definitions[key] = {
                "description": description,
                "attributes": normalized_attributes,
            }
    return definitions
