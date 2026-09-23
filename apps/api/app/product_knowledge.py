"""Approved product-wide text, with immutable versions and no external URL fetching."""
from __future__ import annotations

import hashlib
import json
import re
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.auth import AuthUser, require_member
from app.db import get_connection
from app.definitions.access import AccessDenied, authorize_product
from app.definitions.organizations import OrganizationDirectory
from app.engine.knowledge import KnowledgeContext, KnowledgePassage

router = APIRouter(prefix="/api/products", tags=["knowledge"])


class ApprovedText(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=120)
    text: str = Field(min_length=1, max_length=32000)
    approved: bool


def _access(user: AuthUser, product_id: str):
    try:
        return authorize_product(user, product_id, OrganizationDirectory())
    except AccessDenied:
        raise HTTPException(404, "This product is not available.") from None


@router.get("/{product_id}/knowledge")
def documents(product_id: str, user: AuthUser = Depends(require_member)) -> dict:
    access = _access(user, product_id)
    with get_connection() as connection:
        rows = connection.execute(
            "select document_id, title, length(body) as characters from approved_documents "
            "where tenant_id=? and product_id=? and knowledge_version=? and definition_checksum=? order by title",
            (user.tenant_id, product_id, access.binding.knowledge_version, access.binding.definition_checksum),
        ).fetchall()
    return {"version": access.binding.knowledge_version, "documents": [dict(row) for row in rows]}


@router.post("/{product_id}/knowledge", status_code=201)
def approve_document(product_id: str, body: ApprovedText, user: AuthUser = Depends(require_member)) -> dict:
    if not body.approved or not body.title.strip() or not body.text.strip():
        raise HTTPException(422, "Approve non-empty source text before publishing.")
    with get_connection() as connection:
        connection.execute("begin immediate")
        directory = OrganizationDirectory(connection=connection)
        membership = directory.membership(user.tenant_id, user.user_id)
        try:
            access = authorize_product(user, product_id, directory)
        except AccessDenied:
            raise HTTPException(404, "This product is not available.") from None
        if membership is None or not (membership.role == "org_admin" or
                                      (membership.role == "team_admin" and membership.team_id == access.binding.team_id)):
            raise HTTPException(403, "Only this product's administrators can publish source text.")
        version = access.binding.knowledge_version
        rows = connection.execute("select document_id, title, body from approved_documents "
                                  "where tenant_id=? and product_id=? and knowledge_version=? and definition_checksum=?",
                                  (user.tenant_id, product_id, version, access.binding.definition_checksum)).fetchall()
        if len(rows) >= 16 or sum(len(row["body"]) for row in rows) + len(body.text) > 128000 or version >= 21:
            raise HTTPException(409, "This product's source capacity is reached.")
        document_id = uuid4().hex
        content = [dict(row) for row in rows] + [{"document_id": document_id, "title": body.title.strip(), "body": body.text.strip()}]
        checksum = hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()
        for item in content:
            connection.execute("insert into approved_documents values (?, ?, ?, ?, ?, ?, ?)",
                               (user.tenant_id, product_id, access.binding.definition_checksum, version + 1,
                                item["document_id"], item["title"], item["body"]))
        connection.execute("update product_bindings set knowledge_version=?, knowledge_checksum=? where tenant_id=? and product_id=?",
                           (version + 1, checksum, user.tenant_id, product_id))
    return {"version": version + 1, "document_id": document_id}


class ApprovedKnowledge:
    def __init__(self, context: KnowledgeContext):
        self.context = context

    def search(self, text: str, limit: int = 3) -> list[KnowledgePassage]:
        context = self.context
        terms = set(re.findall(r"\w{3,}", text.casefold())) - {"the", "what", "how", "does", "can", "you", "about", "this", "that", "are"}
        if not terms:
            return []
        with get_connection() as connection:
            rows = connection.execute("select document_id, title, body from approved_documents "
                                      "where tenant_id=? and product_id=? and knowledge_version=? and definition_checksum=?",
                                      (context.tenant_id, context.product_id, context.knowledge_version, context.definition_checksum)).fetchall()
        ranked = []
        for row in rows:
            for start in range(0, len(row["body"]), 900):
                snippet = row["body"][start:start + 1000]
                score = len(terms & set(re.findall(r"\w{3,}", (row["title"] + " " + snippet).casefold())))
                if score:
                    ranked.append((score, KnowledgePassage(title=row["title"], source=f"document:{row['document_id']}", snippet=snippet)))
        ranked.sort(key=lambda item: item[0], reverse=True)
        return [passage for _, passage in ranked[:max(0, min(limit, 5))]]
