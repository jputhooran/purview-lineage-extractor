"""Registry for built-in and third-party lineage plugins."""

from __future__ import annotations

from importlib.metadata import entry_points
from typing import Any, Callable, Mapping

from ..contracts import Extractor, Publisher

ExtractorFactory = Callable[[], Extractor]
PublisherFactory = Callable[[str, Mapping[str, Any]], Publisher]


class PluginRegistry:
    def __init__(self) -> None:
        self._extractors: dict[str, ExtractorFactory] = {}
        self._publishers: dict[str, PublisherFactory] = {}

    def register_extractor(
        self,
        name: str,
        factory: ExtractorFactory,
    ) -> None:
        self._register(self._extractors, name, factory, "extractor")

    def register_publisher(
        self,
        name: str,
        factory: PublisherFactory,
    ) -> None:
        self._register(self._publishers, name, factory, "publisher")

    @staticmethod
    def _register(
        collection: dict[str, Any],
        name: str,
        factory: Any,
        kind: str,
    ) -> None:
        normalized = name.strip().casefold()
        if not normalized:
            raise ValueError(f"Plugin {kind} name cannot be empty.")
        if normalized in collection:
            raise ValueError(f"Duplicate {kind} plugin '{name}'.")
        collection[normalized] = factory

    def create_extractor(self, name: str) -> Extractor:
        normalized = name.casefold()
        try:
            factory = self._extractors[normalized]
        except KeyError as exc:
            raise ValueError(
                f"Unknown extractor '{name}'. Available: "
                f"{', '.join(self.extractor_names)}"
            ) from exc
        extractor = factory()
        if extractor.plugin_name.casefold() != normalized:
            raise ValueError(
                f"Extractor factory '{name}' returned plugin "
                f"'{extractor.plugin_name}'."
            )
        return extractor

    def create_publisher(
        self,
        plugin_name: str,
        instance_name: str,
        config: Mapping[str, Any],
    ) -> Publisher:
        normalized = plugin_name.casefold()
        try:
            factory = self._publishers[normalized]
        except KeyError as exc:
            raise ValueError(
                f"Unknown publisher '{plugin_name}'. Available: "
                f"{', '.join(self.publisher_names)}"
            ) from exc
        return factory(instance_name, config)

    @property
    def extractor_names(self) -> tuple[str, ...]:
        return tuple(sorted(self._extractors))

    @property
    def publisher_names(self) -> tuple[str, ...]:
        return tuple(sorted(self._publishers))

    def load_entry_point_plugins(self) -> None:
        """Load external factories from standard Python entry-point groups."""
        for item in entry_points(group="lineage_utility.extractors"):
            self.register_extractor(item.name, item.load())
        for item in entry_points(group="lineage_utility.publishers"):
            self.register_publisher(item.name, item.load())

