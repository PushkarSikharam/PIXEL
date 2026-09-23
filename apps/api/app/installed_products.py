"""The installed product packages: the only core module that imports product code.

Packages are listed here explicitly, in code, and looked up by definition ID. Nothing is ever
imported from a path or name supplied by a definition. A definition without an installed
package fails closed.

Imports happen inside `installed_packages()`, so importing this module has no side effects.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from functools import lru_cache
from types import MappingProxyType
from typing import Any


@dataclass(frozen=True)
class ProductPackage:
    definition_id: str
    # Builds a scope-bound RecordLookup for one caller (added in slice 3).
    lookup_factory: Callable[..., Any] | None = None
    # Translates validated generic actions for the current web app (added in slice 3; removed in 3.6).
    legacy_translator: Callable[..., Any] | None = None
    # Builds a scope-bound KnowledgeLookup for one caller (added in slice 4b). A product without
    # documents leaves this unset, and the platform then answers knowledge questions honestly.
    knowledge_factory: Callable[..., Any] | None = None
    # Immutable synthetic seed for a new private demo instance. Missing means the product cannot
    # offer public demo sessions; the platform never falls back to edited member records.
    demo_seed_factory: Callable[[], Any] | None = None
    # Every action type this product's client may be sent. The platform answers with one of these
    # or with nothing: a type no installed package declares never reaches a caller.
    client_action_types: frozenset[str] = frozenset()


class PackageMissing(LookupError):
    """No installed package serves this definition."""


def index_packages(packages: Iterable[ProductPackage]) -> Mapping[str, ProductPackage]:
    index: dict[str, ProductPackage] = {}
    for package in packages:
        if package.definition_id in index:
            raise ValueError(f"two installed packages serve {package.definition_id}")
        index[package.definition_id] = package
    return MappingProxyType(index)


@lru_cache(maxsize=1)
def installed_packages() -> Mapping[str, ProductPackage]:
    from products.linear_simplified.backend.package import PACKAGE as linear_simplified

    return index_packages([linear_simplified])


def client_action_types() -> frozenset[str]:
    """Every action type any installed product may send a client.

    The set is closed over what is installed, not over one product's vocabulary, so adding a
    product adds its own action types and nothing widens for the products already there.
    """
    return frozenset().union(*(package.client_action_types for package in installed_packages().values()))


def package_for(definition_id: str) -> ProductPackage:
    try:
        return installed_packages()[definition_id]
    except KeyError:
        raise PackageMissing(definition_id) from None
