from __future__ import annotations

from pydantic import BaseModel


class AuthSettings(BaseModel):
    enabled: bool = False
    oidc_issuer: str = ""
    oidc_audience: str = ""
    oidc_jwks_url: str = ""
    user_claim: str = "sub"
    tenant_claim: str = "tenant_id"
    accounts_claim: str = "allowed_account_ids"
    tools_claim: str = "tool_permissions"
    allow_unsigned_test_tokens: bool = False
