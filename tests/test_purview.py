from __future__ import annotations

import io
import json
import urllib.error
from pathlib import Path
from typing import Any

from lineage_utility.domain.legacy import graph_from_legacy
from lineage_utility.extractors.sqlserver.parser import (
    StoredProcedureOptions,
    build_model_from_definition,
)
from lineage_utility.publishers.purview.client import (
    AtlasClient,
    AtlasResponse,
    RetryPolicy,
)
from lineage_utility.publishers.purview.mapper import map_graph
from lineage_utility.publishers.purview.publisher import PurviewPublisher
from lineage_utility.publishers.purview.type_definitions import (
    BUILTIN_PROCESS_TYPE_DEFINITIONS,
    STORED_PROCEDURE_PROCESS_TYPE,
    parse_process_type_definitions,
)

FIXTURE = (
    Path(__file__).with_name("fixtures") / "usp_LoadProcedureSales.sql"
)
STABLE_GUID = "21701f50-aa00-401a-b123-2aa1efd16403"


def stored_procedure_graph():
    model, _ = build_model_from_definition(
        StoredProcedureOptions(
            server=r"localhost\MSSQLSERVER2",
            database="SPLineageDW",
            schema="dbo",
            procedure="usp_LoadProcedureSales",
        ),
        FIXTURE.read_text(encoding="utf-8"),
        backend="sqlglot",
    )
    return graph_from_legacy(model)


def test_mapper_preserves_column_level_contract() -> None:
    graph = stored_procedure_graph()
    definition = BUILTIN_PROCESS_TYPE_DEFINITIONS[
        STORED_PROCEDURE_PROCESS_TYPE
    ]
    batch = map_graph(
        graph,
        dataset_type="mssql_table",
        column_type="mssql_column",
        process_type=STORED_PROCEDURE_PROCESS_TYPE,
        process_type_definition=definition,
        with_columns=True,
    )

    assert sum(
        item["typeName"] == "mssql_table" for item in batch.entities
    ) == 3
    assert sum(
        item["typeName"] == "mssql_column" for item in batch.entities
    ) == 14
    process = next(
        item
        for item in batch.entities
        if item["typeName"] == STORED_PROCEDURE_PROCESS_TYPE
    )
    mapping = json.loads(process["attributes"]["columnMapping"])
    assert sum(len(item["ColumnMapping"]) for item in mapping) == 7
    assert batch.processes[0].expected_column_pairs == 7


def test_table_fallback_explicitly_clears_stale_column_mapping() -> None:
    graph = stored_procedure_graph()
    definition = BUILTIN_PROCESS_TYPE_DEFINITIONS[
        STORED_PROCEDURE_PROCESS_TYPE
    ]
    batch = map_graph(
        graph,
        dataset_type="DataSet",
        column_type=None,
        process_type=STORED_PROCEDURE_PROCESS_TYPE,
        process_type_definition=definition,
        with_columns=False,
    )
    process = next(
        item
        for item in batch.entities
        if item["typeName"] == STORED_PROCEDURE_PROCESS_TYPE
    )

    assert process["attributes"]["columnMapping"] == "[]"
    assert batch.processes[0].expected_column_pairs == 0


class FakeAtlasClient:
    account = "test-purview"

    def __init__(
        self,
        *,
        existing_guid: str | None,
        bulk_statuses: list[int],
    ) -> None:
        self.existing_guid = existing_guid
        self.bulk_statuses = list(bulk_statuses)
        self.bulk_entities: list[list[dict[str, Any]]] = []
        self.latest_process: dict[str, Any] | None = None

    def get(self, path: str) -> AtlasResponse:
        if path.startswith("types/"):
            definition = BUILTIN_PROCESS_TYPE_DEFINITIONS[
                STORED_PROCEDURE_PROCESS_TYPE
            ]
            return AtlasResponse(
                200,
                {
                    "attributeDefs": [
                        {"name": item["name"]}
                        for item in definition["attributes"]
                    ]
                },
            )
        if path.startswith("entity/uniqueAttribute/"):
            if self.existing_guid:
                return AtlasResponse(
                    200,
                    {"entity": {"guid": self.existing_guid}},
                )
            return AtlasResponse(404, {})
        if path.startswith("entity/guid/"):
            assert self.latest_process is not None
            entity = {
                **self.latest_process,
                "guid": path.rsplit("/", 1)[-1],
            }
            return AtlasResponse(200, {"entity": entity})
        raise AssertionError(f"Unexpected GET {path}")

    def post(
        self,
        path: str,
        body: dict[str, Any],
    ) -> AtlasResponse:
        assert path == "entity/bulk"
        entities = body["entities"]
        self.bulk_entities.append(entities)
        status = self.bulk_statuses.pop(0)
        if status not in (200, 201):
            return AtlasResponse(status, {"error": "rejected"})
        process = next(
            item
            for item in entities
            if item["typeName"] == STORED_PROCEDURE_PROCESS_TYPE
        )
        self.latest_process = process
        assignments = (
            {}
            if self.existing_guid
            else {process["guid"]: STABLE_GUID}
        )
        return AtlasResponse(status, {"guidAssignments": assignments})


def publisher(client: FakeAtlasClient) -> PurviewPublisher:
    return PurviewPublisher(
        instance_name="test",
        client=client,
        process_type_definitions=BUILTIN_PROCESS_TYPE_DEFINITIONS,
    )


def test_publisher_reuses_stable_process_guid_and_verifies_graph() -> None:
    client = FakeAtlasClient(
        existing_guid=STABLE_GUID,
        bulk_statuses=[200],
    )

    result = publisher(client).publish(stored_procedure_graph())

    assert result.success
    assert next(iter(result.process_guids.values())) == STABLE_GUID
    assert result.details["graphs"] == [
        {"nodes": 4, "edges": 3, "column_pairs": 7}
    ]
    process = next(
        item
        for item in client.bulk_entities[0]
        if item["typeName"] == STORED_PROCEDURE_PROCESS_TYPE
    )
    assert process["guid"] == STABLE_GUID


def test_publisher_isolates_column_failure_with_dataset_fallback() -> None:
    client = FakeAtlasClient(
        existing_guid=None,
        bulk_statuses=[400, 200],
    )

    result = publisher(client).publish(stored_procedure_graph())

    assert result.success
    assert result.details["dataset_type"] == "DataSet"
    assert result.details["column_level"] is False
    assert result.details["graphs"][0]["column_pairs"] == 0


class TokenProvider:
    def __init__(self) -> None:
        self.calls = 0

    def get_token(self, scope: str) -> str:
        assert scope == "https://purview.azure.net/.default"
        self.calls += 1
        return "test-token"


class HttpResponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self) -> bytes:
        return b'{"ok":true}'


def test_atlas_client_retries_throttling(monkeypatch) -> None:
    provider = TokenProvider()
    requests = []
    sleeps = []

    def open_url(request, timeout):
        requests.append((request, timeout))
        if len(requests) == 1:
            raise urllib.error.HTTPError(
                request.full_url,
                429,
                "throttled",
                hdrs=None,
                fp=io.BytesIO(b'{"error":"busy"}'),
            )
        return HttpResponse()

    monkeypatch.setattr("urllib.request.urlopen", open_url)
    client = AtlasClient(
        account="test-purview",
        token_provider=provider,
        retry_policy=RetryPolicy(
            max_attempts=2,
            initial_delay_seconds=0,
            maximum_delay_seconds=0,
            jitter_ratio=0,
        ),
        sleep=sleeps.append,
    )

    response = client.get("types/typedef/name/test")

    assert response.status_code == 200
    assert response.body == {"ok": True}
    assert provider.calls == 2
    assert sleeps == [0]
    assert requests[1][0].get_header("Authorization") == "Bearer test-token"


def test_builtin_process_type_can_only_be_extended() -> None:
    definitions = parse_process_type_definitions(
        {
            STORED_PROCEDURE_PROCESS_TYPE: {
                "description": "Extension",
                "attributes": [
                    {"name": "orchestratorRunId", "type_name": "string"}
                ],
            }
        }
    )
    names = {
        item["name"]
        for item in definitions[STORED_PROCEDURE_PROCESS_TYPE][
            "attributes"
        ]
    }

    assert "columnMapping" in names
    assert "orchestratorRunId" in names
