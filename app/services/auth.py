"""Server-issued browser sessions and configured staff API credentials."""

import hashlib
import secrets
from fastapi import HTTPException, Request
from pydantic import BaseModel, Field
from app.services.tickets import TicketCatalog


class Principal(BaseModel):
    actor_id: str = Field(min_length=1)
    tenant: str = "demo"
    level: int = Field(default=1, ge=1, le=3)
    allowed_ticket_ids: list[str] | None = None
    credential_id: str | None = None


class AuthService:
    def __init__(self, settings, repository):
        self.settings, self.repository = settings, repository
        self.catalog = TicketCatalog(repository)

    def credential(self, token):
        for key, value in self.settings.api_users.items():
            if secrets.compare_digest(token, key):
                principal = Principal.model_validate(value).model_dump()
                principal["credential_id"] = hashlib.sha256(key.encode()).hexdigest()
                return principal
        return None

    async def principal(self, request: Request):
        authorization = request.headers.get("authorization")
        if authorization:
            scheme, _, token = authorization.partition(" ")
            principal = self.credential(token) if scheme.lower() == "bearer" else None
        else:
            principal = await self.repository.identity(request.cookies.get("aegis_session", ""))
            if principal:
                credential_id = principal.get("credential_id")
                if credential_id:
                    principal = next((self.credential(k) for k in self.settings.api_users
                                      if hashlib.sha256(k.encode()).hexdigest() == credential_id), None)
                elif not self.settings.demo_auth:
                    principal = None
        if not principal:
            raise HTTPException(401, "请建立客服身份会话")
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            if origin and origin != str(request.base_url).rstrip("/") and origin not in self.settings.allowed_origins:
                raise HTTPException(403, "跨站请求被拒绝")
        return principal

    async def bootstrap(self, request, response, api_token=None):
        origin = request.headers.get("origin")
        if origin and origin != str(request.base_url).rstrip("/") and origin not in self.settings.allowed_origins:
            raise HTTPException(403, "跨站请求被拒绝")
        if api_token:
            principal = self.credential(api_token)
            if not principal:
                raise HTTPException(401, "无效的客服凭据")
        else:
            try:
                return await self.principal(request)
            except HTTPException:
                if not self.settings.demo_auth:
                    raise HTTPException(401, "请提供配置的客服 API 凭据")
            principal = Principal(actor_id="demo_" + secrets.token_hex(12)).model_dump()
        token = secrets.token_urlsafe(32)
        await self.repository.save_identity(token, principal)
        response.set_cookie("aegis_session", token, max_age=86400 * 30,
            httponly=True, samesite="strict", secure=self.settings.cookie_secure)
        return principal

    async def require_session(self, request, sid):
        principal = await self.principal(request)
        session = await self.repository.session(sid)
        if not session or session["owner"] != principal["actor_id"] or session["tenant"] != principal["tenant"]:
            raise HTTPException(404, "会话不存在")
        if session["ticket_id"]:
            await self.require_ticket(principal, session["ticket_id"])
        return principal, session

    def ticket(self, principal, ticket_id):
        allowed = principal.get("allowed_ticket_ids")
        if allowed is not None and ticket_id not in allowed:
            raise HTTPException(403, "无权访问该工单")

    async def require_ticket(self, principal, ticket_id, *, team=False):
        record = await self.catalog.record(principal, ticket_id, team=team)
        if not record:
            # Preserve assigned-ticket authorization errors for visible demo identifiers.
            from app.data.seed import get_ticket
            if get_ticket(ticket_id):
                self.ticket(principal, ticket_id)
            raise HTTPException(404, "工单不存在或已删除")
        return record["ticket"]
