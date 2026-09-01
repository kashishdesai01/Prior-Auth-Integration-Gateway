from functools import lru_cache
from typing import Literal

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PAGW_", env_file=".env", extra="ignore")
    database_url: SecretStr = SecretStr("postgresql://pagw:local-only@127.0.0.1:55480/pagw_native")
    mode: Literal["local", "production"] = "local"
    service_role: Literal["all", "api", "worker", "webhooks", "relay", "scheduler", "migrate"] = (
        "all"
    )
    broker: Literal["postgres", "sqs"] = "postgres"
    sqs_endpoint: str | None = None
    submit_queue_url: str = ""
    webhook_queue_url: str = ""
    aws_region: str = "us-west-2"
    issuer: str = "http://127.0.0.1:8081"
    audience: str = "pagw"
    jwks_url: str = "http://127.0.0.1:8081/.well-known/jwks.json"
    payer_base_url: str = "http://127.0.0.1:8082"
    callback_base_url: str = "http://127.0.0.1:8080"
    receiver_url: str = "http://127.0.0.1:8083/events"
    callback_secret: SecretStr = SecretStr("")
    webhook_secret: SecretStr = SecretStr("")
    payer_client_secret: SecretStr = SecretStr("")
    encryption_key: SecretStr = SecretStr("")
    idp_key_path: str = ".runtime/idp.pem"
    client_key_path: str = ".runtime/client.pem"
    client_private_key: SecretStr | None = None
    client_registry: dict[str, dict] = {}
    receiver_db_path: str = ".runtime/receiver.sqlite"
    lease_seconds: int = 60
    http_timeout_seconds: float = 5
    poll_seconds: float = 2
    retry_base_seconds: float = 2
    max_attempts: int = 6
    sla_seconds: int = 3600
    otlp_endpoint: str = ""
    log_level: str = "INFO"

    @model_validator(mode="after")
    def safe_runtime(self):
        from cryptography.fernet import Fernet

        required = {
            "all": {"callback_secret", "webhook_secret", "payer_client_secret", "encryption_key"},
            "api": {"callback_secret", "encryption_key"},
            "worker": {"payer_client_secret", "client_private_key"},
            "webhooks": {"encryption_key"},
            "relay": set(),
            "scheduler": set(),
            "migrate": set(),
        }[self.service_role]
        for name in required:
            value = getattr(self, name)
            if value is None or not value.get_secret_value():
                raise ValueError(f"{self.service_role} requires {name}")
        if self.encryption_key.get_secret_value():
            Fernet(self.encryption_key.get_secret_value().encode())
        if self.lease_seconds <= self.http_timeout_seconds * 8:
            raise ValueError("lease_seconds must exceed the bounded auth/401-retry HTTP budget")
        if min(self.poll_seconds, self.retry_base_seconds, self.sla_seconds) <= 0:
            raise ValueError("scheduling intervals must be positive")
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        if self.mode == "production":
            from psycopg.conninfo import conninfo_to_dict

            if (
                conninfo_to_dict(self.database_url.get_secret_value()).get("sslmode")
                != "verify-full"
            ):
                raise ValueError("production PostgreSQL requires sslmode=verify-full")
            if self.service_role == "all":
                raise ValueError("production requires an explicit service_role")
            if self.service_role == "api" and not self.client_registry:
                raise ValueError("production requires an explicit client-to-tenant registry")
            if self.broker != "sqs" or self.sqs_endpoint:
                raise ValueError("production requires real SQS")
            for url in (
                self.issuer,
                self.jwks_url,
                self.payer_base_url,
                self.callback_base_url,
                self.receiver_url,
            ):
                if not url.startswith("https://"):
                    raise ValueError("production service URLs require TLS")
            for name in required & {"callback_secret", "webhook_secret", "payer_client_secret"}:
                if len(getattr(self, name).get_secret_value()) < 32:
                    raise ValueError("production secrets must be at least 32 characters")
        for client, entry in self.client_registry.items():
            if not client or not isinstance(entry.get("tenant"), str) or not entry["tenant"]:
                raise ValueError("each registered client requires a tenant")
            if not isinstance(entry.get("scopes"), list) or not set(entry["scopes"]) <= {
                "read",
                "submit",
                "webhooks",
                "ops",
            }:
                raise ValueError("invalid registered client scopes")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
