# Plugin authoring

Plugins translate ETL-specific metadata into the canonical
`LineageGraph`, or publish that graph to an external catalog. The utility
discovers third-party plugins through standard Python entry points.

## Extractor

Implement the `Extractor` protocol:

```python
from pathlib import Path
from typing import Any, Mapping, Sequence

from lineage_utility.contracts import ExtractionTarget
from lineage_utility.domain import LineageGraph


class AcmeExtractor:
    plugin_name = "acme-etl"

    def discover(
        self,
        source: Mapping[str, Any],
        *,
        base_dir: Path,
    ) -> Sequence[ExtractionTarget]:
        ...

    def extract(self, target: ExtractionTarget) -> LineageGraph:
        ...
```

`discover` must return deterministic, independently executable targets.
One malformed target should not prevent other targets in the same job from
running when `continue_on_error` is enabled. `extract` must return a valid
canonical graph; model construction enforces asset, process, edge, field,
and mapping references.

Register the no-argument factory in the plugin package:

```toml
[project.entry-points."lineage_utility.extractors"]
acme-etl = "acme_lineage:AcmeExtractor"
```

## Publisher

Implement the `Publisher` protocol. The factory receives the configured
publisher instance name and its plugin-specific settings:

```python
from typing import Any, Mapping

from lineage_utility.contracts import PublishResult
from lineage_utility.domain import LineageGraph


class AcmePublisher:
    plugin_name = "acme-catalog"

    def __init__(self, instance_name: str, config: Mapping[str, Any]):
        self.instance_name = instance_name

    def publish(self, graph: LineageGraph) -> PublishResult:
        ...


def create_publisher(
    instance_name: str,
    config: Mapping[str, Any],
) -> AcmePublisher:
    return AcmePublisher(instance_name, config)
```

```toml
[project.entry-points."lineage_utility.publishers"]
acme-catalog = "acme_lineage:create_publisher"
```

`publish` should only return success after remote read-back validation. Raise
an explicit exception for partial or failed writes so the runner records the
target as failed and does not advance incremental state.

## Validation

Install the plugin into the same environment, then run:

```powershell
python -m lineage_utility plugins
python -m lineage_utility validate --config .\lineage.yml
python -m lineage_utility plan --config .\lineage.yml --job acme-job
```

Use globally unique qualified names. Changing an existing qualified-name
rule creates new catalog identities and should be handled as a migration.

