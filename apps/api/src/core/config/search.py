from __future__ import annotations

from pydantic import BaseModel, SecretStr


class SearchSettings(BaseModel):
    tikhub_api_key: SecretStr = SecretStr("")
    metaso_api_key: SecretStr = SecretStr("")
    anspire_api_key: SecretStr = SecretStr("")
    search_cache_ttl_seconds: int = 300
    hotspot_cache_ttl_seconds: int = 180
    hotspot_per_source_limit: int = 10
    hotspot_raw_candidate_limit: int = 200
