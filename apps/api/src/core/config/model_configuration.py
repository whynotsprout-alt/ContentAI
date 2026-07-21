from pydantic import BaseModel, SecretStr


class ModelConfigurationSettings(BaseModel):
    encryption_key: SecretStr = SecretStr("")
