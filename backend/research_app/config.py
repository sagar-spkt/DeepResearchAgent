from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore", populate_by_name=True)
    environment: Literal["local", "staging", "production"] = Field(
        "local", validation_alias=AliasChoices("ENVIRONMENT", "ENV", "environment")
    )
    database_url: str = "sqlite:///./local-data/research.db"
    model_provider: Literal["fake", "bedrock", "vllm"] = "bedrock"
    release_id: str = "development"
    bedrock_model_id: str = ""
    aws_region: str = "us-east-1"
    vllm_base_url: str = ""
    vllm_api_key: str = ""
    vllm_model: str = "Qwen/Qwen3-8B"
    tavily_api_key: str = ""
    bootstrap_owner_email: str = "demo@example.test"
    cognito_issuer: str = ""
    cognito_client_id: str = ""
    storage_backend: Literal["local", "s3"] = "local"
    storage_path: Path = Path("local-data/uploads")
    s3_bucket: str = ""
    sqs_queue_url: str = ""
    max_upload_bytes: int = 20 * 1024 * 1024
    max_user_storage_bytes: int = 200 * 1024 * 1024
    max_user_documents: int = 50
    max_queue_size: int = 10
    run_timeout_seconds: int = 900
    lease_seconds: int = 60
    max_run_attempts: int = 3
    search_call_limit: int = 10
    langfuse_base_url: str = ""
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    observability_upstream: str = ""
    observability_console_url: str = ""
    allowed_origins: list[str] = ["http://localhost:3000"]
    embedding_provider: Literal["fake", "sentence-transformers"] = "sentence-transformers"
    embedding_model: str = ""

    @model_validator(mode="after")
    def validate_environment(self):
        if self.observability_console_url:
            from urllib.parse import urlsplit

            console = urlsplit(self.observability_console_url)
            if (
                console.scheme != "https"
                or not console.hostname
                or console.username
                or console.password
                or console.port
                or console.path not in {"", "/"}
                or console.query
                or console.fragment
            ):
                raise ValueError(
                    "OBSERVABILITY_CONSOLE_URL must be an HTTPS origin without credentials or a subpath"
                )
        if self.model_provider == "bedrock" and not self.bedrock_model_id:
            raise ValueError("BEDROCK_MODEL_ID must be explicitly configured")
        if self.model_provider == "vllm" and not (self.vllm_base_url and self.vllm_api_key):
            raise ValueError("VLLM_BASE_URL and VLLM_API_KEY are required")
        if self.embedding_provider == "sentence-transformers" and not self.embedding_model:
            raise ValueError("EMBEDDING_MODEL must be explicitly configured")
        if self.storage_backend == "s3" and not self.s3_bucket:
            raise ValueError("S3_BUCKET is required")
        if self.environment != "local":
            if not self.database_url.startswith("postgresql"):
                raise ValueError("Cloud environments require PostgreSQL")
            if not (
                self.cognito_issuer.startswith("https://cognito-idp.") and self.cognito_client_id
            ):
                raise ValueError("Cloud environments require Cognito issuer and client ID")
            if self.storage_backend != "s3" or self.model_provider not in {"vllm", "bedrock"}:
                raise ValueError("Cloud environments require S3 and a real model provider")
            if self.embedding_provider != "sentence-transformers":
                raise ValueError("Cloud environments require real embeddings")
        if self.max_queue_size < 1 or self.lease_seconds < 10 or self.run_timeout_seconds < 1:
            raise ValueError("Invalid operational limits")
        return self
