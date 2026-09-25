from pathlib import Path
from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from image23mf.engine.ingestion import DEFAULT_MAX_INPUT_BYTES


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="IMAGE23MF_",
        env_file=".env",
        extra="ignore",
    )

    host: str = "127.0.0.1"
    port: int = Field(default=8323, ge=1, le=65535)
    workspace: Path = Path("workspace")
    log_level: str = "INFO"
    bambu_resources_root: Optional[Path] = Path("/Applications/BambuStudio.app/Contents/Resources")
    max_image_input_bytes: int = Field(
        default=DEFAULT_MAX_INPUT_BYTES,
        ge=1,
        le=1024**3,
    )


settings = Settings()
