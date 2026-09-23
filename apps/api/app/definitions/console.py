"""The assistant that moves somebody around the application itself.

Someone using this platform is moving around a place, and the ways of moving around it are
described by a definition like any other product's, so one engine answers both "take me to my
products" and whatever the product they are inside is asked. The alternative - deciding
navigation in the browser - would put behaviour back in the client that deliberately lives on
the server.

Which definition describes the application is configuration, not something written here: core
names no definition. When `PIXEL_CONSOLE_DEFINITION` is unset there is simply no such assistant,
which is the safe way round - a deployment that has not chosen one gets nothing rather than a
guess.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.definitions.organizations import OrganizationDirectory
from app.definitions.registry import RegistryError
from app.definitions.shipped_knowledge import publish_shipped_documents
from app.record_access import grant_records
from app.services.env import env_value


@dataclass(frozen=True)
class ConsoleProduct:
    definition_id: str
    product_id: str


def configured_console() -> ConsoleProduct | None:
    """The definition this deployment uses for moving around itself, if it chose one."""
    definition_id = (env_value("PIXEL_CONSOLE_DEFINITION") or "").strip()
    if not definition_id:
        return None
    return ConsoleProduct(definition_id, definition_id.replace("_", "-"))


def ensure_console_product(directory: OrganizationDirectory, tenant_id: str, team_id: str,
                           members: tuple[str, ...] = ()) -> ConsoleProduct | None:
    """Give one organization the assistant for moving around the application.

    Idempotent: an organization that already has it is left exactly as it is, including a version
    an operator moved it to. A deployment that configured no console gets nothing.
    """
    console = configured_console()
    if console is None:
        return None
    try:
        version = max(directory.definitions.source.versions(console.definition_id), default=0)
        if version < 1:
            return None
        directory.definitions.ensure_published(console.definition_id, version)
        existing = directory.product(tenant_id, console.product_id)
        if existing is None:
            directory.bind_product(tenant_id, console.product_id, team_id,
                                   console.definition_id, version)
        elif existing.definition_version < version:
            # Moving around Pixel is Pixel's own, so it follows the platform rather than waiting
            # for somebody to upgrade it. A customer's product is never moved this way.
            directory.move_product_version(tenant_id, console.product_id, version)
        # What this application is, in approved text, so that somebody asking what Pixel is or
        # how it works is answered from something real instead of being told there is nothing to
        # answer from. It is published like any other product's shipped documents.
        binding = directory.product(tenant_id, console.product_id)
        if binding is not None:
            publish_shipped_documents(
                directory.definitions.source, tenant_id, console.product_id,
                console.definition_id, binding.definition_checksum, binding.knowledge_version,
            )
    except RegistryError:
        # A console that cannot be published is not worth failing an organization over; the
        # person still gets their products, without the assistant that moves between them.
        return None
    for user_id in members:
        # It holds no records of its own, so nobody needs a scope in it; access is the point.
        grant_records(tenant_id, console.product_id, user_id, [], True)
    return console
