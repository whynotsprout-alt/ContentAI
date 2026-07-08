from __future__ import annotations

from pydantic import BaseModel, SecretStr


class SearchSettings(BaseModel):
    traffic_relay_base_url: str = "https://traffic-relay.onrender.com/v1"
    traffic_relay_api_key: SecretStr = SecretStr("")
    tikhub_api_key: SecretStr = SecretStr("")
    metaso_api_key: SecretStr = SecretStr("")
    metaso_search_api_key: SecretStr = SecretStr("")
    metaso_key: SecretStr = SecretStr("")
    anspire_api_key: SecretStr = SecretStr("")
    search_cache_ttl_seconds: int = 300
    hotspot_cache_ttl_seconds: int = 180
