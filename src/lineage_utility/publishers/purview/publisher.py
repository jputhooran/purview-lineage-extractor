"""Microsoft Purview publisher orchestration and read-back verification."""

from __future__ import annotations

import json
import logging
from typing import Any, Mapping
from urllib.parse import quote, urlencode

from ...contracts import PublishResult
from ...domain import LineageGraph
from .auth import create_token_provider
from .client import AtlasClient, RetryPolicy
from .mapper import EntityBatch, map_graph
from .type_definitions import (
    attribute_definition,
    parse_process_type_definitions,
)

LOGGER = logging.getLogger(__name__)


class PurviewPublishError(RuntimeError):
    """Raised when Purview publication or verification fails."""


def _boolean(
    value: Any,
    *,
    name: str,
    default: bool,
) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ValueError(f"Purview '{name}' must be true or false.")
    return value


class PurviewPublisher:
    plugin_name = "purview"

    def __init__(
        self,
        *,
        instance_name: str,
        client: AtlasClient,
        process_type_definitions: Mapping[str, Mapping[str, Any]],
        manage_types: bool = True,
        allow_process_fallback: bool = True,
    ) -> None:
        self.instance_name = instance_name
        self._client = client
        self._process_type_definitions = dict(process_type_definitions)
        self._manage_types = manage_types
        self._allow_process_fallback = allow_process_fallback

    def _ensure_process_type(self, requested_type: str) -> str:
        if requested_type == "Process":
            return requested_type
        definition = self._process_type_definitions.get(requested_type)
        if definition is None:
            if self._allow_process_fallback:
                LOGGER.warning(
                    "No Purview definition for process type '%s'; using Process.",
                    requested_type,
                )
                return "Process"
            raise PurviewPublishError(
                f"No Purview process type definition for '{requested_type}'."
            )

        type_path = quote(requested_type, safe="")
        existing = self._client.get(f"types/typedef/name/{type_path}")
        attributes = definition["attributes"]
        if existing.status_code == 200:
            present = {
                item["name"]
                for item in existing.body.get("attributeDefs", [])
            }
            missing = [
                item for item in attributes if item["name"] not in present
            ]
            if not missing:
                return requested_type
            if not self._manage_types:
                raise PurviewPublishError(
                    f"Purview type '{requested_type}' is missing attributes: "
                    f"{', '.join(item['name'] for item in missing)}"
                )
            updated = dict(existing.body)
            updated["attributeDefs"] = [
                *existing.body.get("attributeDefs", []),
                *[
                    attribute_definition(
                        item["name"],
                        item["type_name"],
                    )
                    for item in missing
                ],
            ]
            response = self._client.put(
                "types/typedefs", {"entityDefs": [updated]}
            )
            if response.status_code not in (200, 201):
                return self._type_failure(
                    requested_type,
                    "update",
                    response.status_code,
                    response.body,
                )
            return requested_type

        if existing.status_code != 404:
            return self._type_failure(
                requested_type,
                "read",
                existing.status_code,
                existing.body,
            )
        if not self._manage_types:
            raise PurviewPublishError(
                f"Purview type '{requested_type}' does not exist and type "
                "management is disabled."
            )
        payload = {
            "entityDefs": [
                {
                    "category": "ENTITY",
                    "name": requested_type,
                    "superTypes": ["Process"],
                    "description": definition["description"],
                    "attributeDefs": [
                        attribute_definition(
                            item["name"],
                            item["type_name"],
                        )
                        for item in attributes
                    ],
                }
            ]
        }
        response = self._client.post("types/typedefs", payload)
        if response.status_code in (200, 201, 409):
            return requested_type
        return self._type_failure(
            requested_type,
            "create",
            response.status_code,
            response.body,
        )

    def _type_failure(
        self,
        requested_type: str,
        operation: str,
        status_code: int,
        body: Mapping[str, Any],
    ) -> str:
        if self._allow_process_fallback:
            LOGGER.warning(
                "Could not %s Purview type '%s' (HTTP %s); using Process.",
                operation,
                requested_type,
                status_code,
            )
            return "Process"
        raise PurviewPublishError(
            f"Could not {operation} Purview type '{requested_type}' "
            f"(HTTP {status_code}): {json.dumps(body)[:500]}"
        )

    def _entity_guid(
        self,
        type_name: str,
        qualified_name: str,
    ) -> str | None:
        query = urlencode({"attr:qualifiedName": qualified_name})
        response = self._client.get(
            "entity/uniqueAttribute/type/"
            f"{quote(type_name, safe='')}?{query}"
        )
        if response.status_code == 200:
            guid = response.body.get("entity", {}).get("guid")
            return str(guid) if guid else None
        if response.status_code != 404:
            raise PurviewPublishError(
                f"Could not resolve '{qualified_name}' as {type_name} "
                f"(HTTP {response.status_code})."
            )
        return None

    def _bind_existing_processes(
        self,
        batch: EntityBatch,
        process_type: str,
    ) -> None:
        entities = {
            entity["guid"]: entity
            for entity in batch.entities
            if entity["typeName"] == process_type
        }
        for process in batch.processes:
            guid = self._entity_guid(
                process_type, process.qualified_name
            )
            if not guid:
                for legacy_identity in process.legacy_qualified_names:
                    guid = self._entity_guid(
                        process_type, legacy_identity
                    )
                    if guid:
                        LOGGER.info(
                            "Migrating process identity '%s' to '%s'.",
                            legacy_identity,
                            process.qualified_name,
                        )
                        break
            if guid:
                entities[process.placeholder_guid]["guid"] = guid
                process.guid = guid

    def _verify(
        self,
        batch: EntityBatch,
    ) -> tuple[dict[str, str], list[dict[str, int]]]:
        process_guids: dict[str, str] = {}
        graph_details = []
        for process in batch.processes:
            if not process.guid:
                raise PurviewPublishError(
                    f"No GUID was returned for process '{process.name}'."
                )
            response = self._client.get(
                f"entity/guid/{quote(process.guid, safe='')}"
            )
            if response.status_code != 200:
                raise PurviewPublishError(
                    f"Could not read back process '{process.name}' "
                    f"(HTTP {response.status_code})."
                )
            entity = response.body.get("entity", {})
            attributes = entity.get("attributes", {})
            relationships = entity.get("relationshipAttributes", {})
            raw_mapping = attributes.get("columnMapping")
            pair_count = 0
            if raw_mapping:
                try:
                    pair_count = sum(
                        len(group["ColumnMapping"])
                        for group in json.loads(raw_mapping)
                    )
                except (KeyError, TypeError, json.JSONDecodeError) as exc:
                    raise PurviewPublishError(
                        f"Process '{process.name}' has invalid columnMapping."
                    ) from exc
            if pair_count != process.expected_column_pairs:
                raise PurviewPublishError(
                    f"Process '{process.name}' has {pair_count} column pairs; "
                    f"expected {process.expected_column_pairs}."
                )
            inputs = (
                attributes.get("inputs")
                or relationships.get("inputs")
                or []
            )
            outputs = (
                attributes.get("outputs")
                or relationships.get("outputs")
                or []
            )
            if (
                len(inputs) != process.expected_inputs
                or len(outputs) != process.expected_outputs
            ):
                raise PurviewPublishError(
                    f"Process '{process.name}' has {len(inputs)} inputs and "
                    f"{len(outputs)} outputs; expected "
                    f"{process.expected_inputs} and "
                    f"{process.expected_outputs}."
                )
            nodes = 1 + len(
                {
                    item.get("guid")
                    for item in [*inputs, *outputs]
                    if item.get("guid")
                }
            )
            graph_details.append(
                {
                    "nodes": nodes,
                    "edges": len(inputs) + len(outputs),
                    "column_pairs": pair_count,
                }
            )
            process_guids[process.qualified_name] = process.guid
        return process_guids, graph_details

    def publish(self, graph: LineageGraph) -> PublishResult:
        requested_types = {item.process_type for item in graph.processes}
        if len(requested_types) != 1:
            raise PurviewPublishError(
                "One lineage graph cannot mix Purview process types."
            )
        requested_type = next(iter(requested_types))
        process_type = self._ensure_process_type(requested_type)
        definition = self._process_type_definitions.get(process_type)
        declared_attributes = {
            item["name"]
            for item in definition.get("attributes", [])
        } if definition else set()
        has_columns = bool(
            graph.field_mappings
            and definition
            and "columnMapping" in declared_attributes
        )
        attempts = [
            (
                "mssql_table",
                "mssql_column" if has_columns else None,
                has_columns,
            ),
            ("DataSet", None, False),
        ]
        failures = []
        for dataset_type, column_type, with_columns in attempts:
            batch = map_graph(
                graph,
                dataset_type=dataset_type,
                column_type=column_type,
                process_type=process_type,
                process_type_definition=definition,
                with_columns=with_columns,
            )
            self._bind_existing_processes(batch, process_type)
            response = self._client.post(
                "entity/bulk", {"entities": batch.entities}
            )
            if response.status_code not in (200, 201):
                failures.append(
                    {
                        "dataset_type": dataset_type,
                        "status_code": response.status_code,
                        "body": response.body,
                    }
                )
                continue
            assignments = response.body.get("guidAssignments", {})
            for process in batch.processes:
                process.guid = process.guid or assignments.get(
                    process.placeholder_guid
                )
            process_guids, graphs = self._verify(batch)
            return PublishResult(
                publisher=self.instance_name,
                artifact_qualified_name=graph.artifact_qualified_name,
                success=True,
                process_guids=process_guids,
                details={
                    "account": self._client.account,
                    "dataset_type": dataset_type,
                    "column_level": with_columns,
                    "entity_count": len(batch.entities),
                    "graphs": graphs,
                },
            )
        raise PurviewPublishError(
            "Purview rejected both column/table and DataSet fallback "
            f"publication attempts: {json.dumps(failures)[:1000]}"
        )


def create_purview_publisher(
    instance_name: str,
    config: Mapping[str, Any],
) -> PurviewPublisher:
    account = config.get("account")
    if not isinstance(account, str) or not account.strip():
        raise ValueError(
            f"Purview publisher '{instance_name}' requires 'account'."
        )
    auth = config.get("auth") or {}
    if not isinstance(auth, Mapping):
        raise ValueError("Purview publisher 'auth' must be an object.")
    retry_config = config.get("retry") or {}
    if not isinstance(retry_config, Mapping):
        raise ValueError("Purview publisher 'retry' must be an object.")
    retry_policy = RetryPolicy(
        max_attempts=int(retry_config.get("max_attempts", 5)),
        initial_delay_seconds=float(
            retry_config.get("initial_delay_seconds", 1.0)
        ),
        maximum_delay_seconds=float(
            retry_config.get("maximum_delay_seconds", 30.0)
        ),
        jitter_ratio=float(retry_config.get("jitter_ratio", 0.2)),
    )
    definitions = parse_process_type_definitions(
        config.get("process_type_definitions")
    )
    client = AtlasClient(
        account=account,
        token_provider=create_token_provider(auth),
        timeout_seconds=int(config.get("timeout_seconds", 90)),
        retry_policy=retry_policy,
    )
    return PurviewPublisher(
        instance_name=instance_name,
        client=client,
        process_type_definitions=definitions,
        manage_types=_boolean(
            config.get("manage_types"),
            name="manage_types",
            default=True,
        ),
        allow_process_fallback=_boolean(
            config.get("allow_process_fallback"),
            name="allow_process_fallback",
            default=True,
        ),
    )
