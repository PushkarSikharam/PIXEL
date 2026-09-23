"""Publishing the documents a product package ships, so a product can answer about itself.

A product that has no approved text cannot answer a question about what it is, and the platform
says so plainly rather than guessing. That is right for a product nobody has written anything
about, and wrong for one whose package ships documentation: the text exists, it just never
reached the store that replies are grounded in.

This publishes it, on the same terms as text somebody approves by hand. The documents are stored
against the binding's definition checksum, so they belong to one pinned version of one product in
one organization, and a product moved to a new version publishes its new text rather than
quietly keeping the old. Nothing here reads or writes another organization's rows.
"""
from __future__ import annotations

import hashlib
import json

from app.db import get_connection
from app.definitions.loader import SHIPPED_PREFIX, DefinitionSource

# The same ceiling the approval endpoint uses, so shipped text cannot climb past text a person
# approved, and a product that somehow republished on every start would stop rather than grow.
MAX_KNOWLEDGE_VERSION = 21


def publish_shipped_documents(source: DefinitionSource, tenant_id: str, product_id: str,
                              definition_id: str, definition_checksum: str,
                              knowledge_version: int) -> int:
    """Publish this package's documents for one product, unless somebody's own text is there.

    Returns the knowledge version in force afterwards. Idempotent: text that already matches what
    the package ships is left exactly as it is, so starting the application twice does not add a
    version twice, and text an administrator approved is never replaced by this - a document
    somebody wrote is theirs, and a package upgrade is not a reason to overwrite it.
    """
    documents = source.documents(definition_id)
    if not documents or knowledge_version >= MAX_KNOWLEDGE_VERSION:
        return knowledge_version
    shipped = {document.document_id: (document.title, document.body) for document in documents}
    with get_connection() as connection:
        connection.execute("begin immediate")
        rows = connection.execute(
            "select document_id, title, body from approved_documents where tenant_id=? "
            "and product_id=? and knowledge_version=? and definition_checksum=?",
            (tenant_id, product_id, knowledge_version, definition_checksum),
        ).fetchall()
        held = {row["document_id"]: (row["title"], row["body"]) for row in rows}
        if held == shipped:
            return knowledge_version
        if any(not key.startswith(SHIPPED_PREFIX) for key in held):
            # Somebody approved text of their own against this version. It is theirs to change,
            # and a package upgrade is not a reason to take it away from them.
            return knowledge_version
        version = knowledge_version + 1
        for document in documents:
            connection.execute(
                "insert or replace into approved_documents values (?, ?, ?, ?, ?, ?, ?)",
                (tenant_id, product_id, definition_checksum, version,
                 document.document_id, document.title, document.body),
            )
        checksum = hashlib.sha256(json.dumps(
            [{"document_id": d.document_id, "title": d.title, "body": d.body} for d in documents],
            sort_keys=True).encode()).hexdigest()
        connection.execute(
            "update product_bindings set knowledge_version=?, knowledge_checksum=? "
            "where tenant_id=? and product_id=?",
            (version, checksum, tenant_id, product_id),
        )
    return version
