"""Reads definition files and adapter manifests, and verifies their identity.

A definition's identity is stated in the file itself. The loader rejects the file unless
its path, its declared identity and (when given) the registered checksum all agree.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import ValidationError

from app.definitions.contract import ProductDefinition, Strict
from app.definitions.safety import check_key

REPO_ROOT = Path(__file__).resolve().parents[4]
MAX_DEFINITION_BYTES = 256_000
_VERSION_FILE = re.compile(r"v([1-9][0-9]{0,4})\.yaml")


class DefinitionError(ValueError):
    """A definition file or adapter manifest is missing, malformed or inconsistent."""


@dataclass(frozen=True)
class DefinitionSource:
    """Where product packages and web adapter packages live, keyed by definition ID."""

    products_root: Path = REPO_ROOT / "products"
    adapters_root: Path = REPO_ROOT / "apps" / "web" / "adapters"

    def definition_path(self, definition_id: str, version: int) -> Path:
        return self.package_path(definition_id) / "definition" / f"v{version}.yaml"

    def manifest_path(self, definition_id: str) -> Path:
        return self.adapters_root / _checked_key(definition_id) / "manifest.json"

    def seed_path(self, definition_id: str) -> Path:
        return self.package_path(definition_id) / "seed" / "demo_organization.json"

    def package_path(self, definition_id: str) -> Path:
        return self.products_root / _checked_key(definition_id)

    def read(self, definition_id: str, version: int) -> bytes:
        """This version's text. A source that keeps definitions elsewhere overrides this."""
        return _read_bytes(self.definition_path(definition_id, version))

    def has(self, definition_id: str, version: int) -> bool:
        return self.definition_path(definition_id, version).is_file()

    def packages(self) -> list[str]:
        """Definition IDs of every product package that ships definitions."""
        if not self.products_root.is_dir():
            return []
        return sorted(
            path.name for path in self.products_root.iterdir()
            if (path / "definition").is_dir() and _is_key(path.name)
        )

    def versions(self, definition_id: str) -> list[int]:
        directory = self.package_path(definition_id) / "definition"
        if not directory.is_dir():
            return []
        return sorted(
            int(match.group(1))
            for path in directory.iterdir()
            if (match := _VERSION_FILE.fullmatch(path.name))
        )


DEFAULT_SOURCE = DefinitionSource()


@dataclass(frozen=True)
class LoadedDefinition:
    definition: ProductDefinition
    checksum: str


class AdapterManifest(Strict):
    """What a product package's web adapter can render. Code-owned, not definition-owned."""

    definition_id: str
    views: dict[str, dict[str, list[str]]]


def file_checksum(source: DefinitionSource, definition_id: str, version: int) -> str:
    return _checksum(source.read(definition_id, version))


def load_definition(
    source: DefinitionSource,
    definition_id: str,
    version: int,
    expected_checksum: str | None = None,
) -> LoadedDefinition:
    raw = source.read(definition_id, version)
    checksum = _checksum(raw)
    if expected_checksum is not None and checksum != expected_checksum:
        raise DefinitionError(
            f"{definition_id} v{version} no longer matches its registered checksum; "
            "published definitions are immutable, so publish a new version instead"
        )
    definition = _parse(raw, checksum)
    declared = definition.definition
    if (declared.definition_id, declared.version) != (definition_id, version):
        raise DefinitionError(
            f"file for {definition_id} v{version} declares {declared.definition_id} v{declared.version}"
        )
    check_adapter_views(definition, load_manifest(source, definition_id))
    return LoadedDefinition(definition=definition, checksum=checksum)


def parse_definition(raw: bytes) -> ProductDefinition:
    """Parse and validate definition content without any file identity checks."""
    return _parse(raw, _checksum(raw))


def load_manifest(source: DefinitionSource, definition_id: str) -> AdapterManifest | None:
    path = source.manifest_path(definition_id)
    if not path.is_file():
        return None
    try:
        manifest = AdapterManifest.model_validate(json.loads(path.read_text(encoding="utf-8")))
    except (ValueError, ValidationError) as error:
        raise DefinitionError(f"invalid adapter manifest for {definition_id}: {error}") from error
    if manifest.definition_id != definition_id:
        raise DefinitionError(f"adapter manifest in {definition_id} declares {manifest.definition_id}")
    return manifest


def check_adapter_views(definition: ProductDefinition, manifest: AdapterManifest | None) -> None:
    """Adapter views are rendered by registered product code; they cannot be invented."""
    available = manifest.views if manifest else {}
    for name, view in definition.views.items():
        if view.kind != "adapter":
            continue
        if name not in available:
            raise DefinitionError(f"view {name} needs a registered adapter, and none is registered")
        unknown = set(view.controls) - set(available[name].get("controls", []))
        if unknown:
            raise DefinitionError(f"view {name} declares controls its adapter does not render: {sorted(unknown)}")


def _checked_key(definition_id: str) -> str:
    try:
        return check_key(definition_id)
    except ValueError as error:
        raise DefinitionError(f"invalid definition id {definition_id!r}") from error


def _is_key(value: str) -> bool:
    try:
        check_key(value)
    except ValueError:
        return False
    return True


def _read_bytes(path: Path) -> bytes:
    if not path.is_file():
        raise DefinitionError(f"definition file not found: {path.name}")
    if path.stat().st_size > MAX_DEFINITION_BYTES:
        raise DefinitionError(f"definition file exceeds {MAX_DEFINITION_BYTES} bytes")
    return path.read_bytes()


def _checksum(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


@lru_cache(maxsize=64)
def _parse(raw: bytes, checksum: str) -> ProductDefinition:
    if len(raw) > MAX_DEFINITION_BYTES:
        raise DefinitionError(f"definition exceeds {MAX_DEFINITION_BYTES} bytes")
    try:
        document = yaml.load(raw.decode("utf-8"), Loader=_StrictLoader)  # noqa: S506 - safe subclass
    except (UnicodeDecodeError, yaml.YAMLError) as error:
        raise DefinitionError(f"definition is not valid YAML: {error}") from error
    if not isinstance(document, dict):
        raise DefinitionError("definition must be a mapping")
    try:
        return ProductDefinition.model_validate(document)
    except ValidationError as error:
        raise DefinitionError(f"definition is invalid: {error}") from error


class _StrictLoader(yaml.SafeLoader):
    """Safe YAML without aliases (no expansion bombs) and without silently repeated keys."""

    def compose_node(self, parent, index):  # type: ignore[override]
        if self.check_event(yaml.AliasEvent):
            raise DefinitionError("YAML aliases are not allowed in definitions")
        return super().compose_node(parent, index)

    def construct_mapping(self, node, deep=False):  # type: ignore[override]
        keys = [self.construct_object(key_node, deep=deep) for key_node, _ in node.value]
        if len(keys) != len(set(map(repr, keys))):
            raise DefinitionError("definition repeats a key")
        if any(not isinstance(key, str) for key in keys):
            raise DefinitionError("definition keys must be strings")
        return super().construct_mapping(node, deep=deep)
