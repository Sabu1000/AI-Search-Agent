"""Connector factory registry."""

from __future__ import annotations

from collections.abc import Callable

from .errors import ContractViolationError
from .models import Provider
from .protocol import Connector

ConnectorFactory = Callable[[], Connector]


class ConnectorRegistry:
    def __init__(self) -> None:
        self._factories: dict[Provider, ConnectorFactory] = {}

    def register(self, provider: Provider, factory: ConnectorFactory) -> None:
        if provider in self._factories:
            raise ContractViolationError(f"Connector already registered for {provider.value}")
        connector = factory()
        if connector.provider != provider:
            raise ContractViolationError("Connector factory returned the wrong provider")
        self._factories[provider] = factory

    def create(self, provider: Provider) -> Connector:
        try:
            return self._factories[provider]()
        except KeyError as error:
            raise ContractViolationError(f"No connector registered for {provider.value}") from error

    @property
    def providers(self) -> tuple[Provider, ...]:
        return tuple(sorted(self._factories, key=lambda provider: provider.value))
