from pydantic import BaseModel


class RedisSettings(BaseModel):
    url: str = "redis://127.0.0.1:6379/0"
    event_ttl_seconds: int = 86400
    event_max_length: int = 10_000
    event_block_ms: int = 5_000
    rate_limit_prefix: str = "contentai:rate-limit"
