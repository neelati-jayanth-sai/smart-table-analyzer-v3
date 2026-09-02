"""Deployment configuration (Runtime_Environments_UI.md #49, #50).

One typed ``Settings`` model loaded from the environment (plus an optional
``.env`` file for local development). Rules enforced here:

- Configuration is validated at startup; the selected environment's required
  connection settings are checked by :meth:`Settings.validate_environment`
  and missing values fail fast with an actionable message. Locally, S3
  settings are required only when object storage is actually used (an
  ``s3://`` warehouse, or a partially set S3 block); a local runtime without
  any S3 configuration is the supported offline ``file://`` data mode,
  detected and reported by :attr:`Settings.local_data_access_mode`.
- Secrets (catalog/S3/IOMETE credentials and the Ollama API key) never appear
  in ``repr``/``str`` and are masked in :meth:`Settings.safe_summary`; they are
  only handed to the backend/catalog layer through
  :meth:`Settings.pyiceberg_properties` or to the Ollama provider by
  :mod:`sta.investigator.agent`.

Variable names follow Runtime_Environments_UI.md #49:

    STA_ENV, STA_DB_PATH, STA_KNOWLEDGE_PATH, STA_MAX_CONCURRENT_RUNS,
    STA_QUERY_TIMEOUT,
    IOMETE_ENDPOINT, IOMETE_CATALOG, IOMETE_TOKEN,
    ICEBERG_CATALOG_URI, ICEBERG_WAREHOUSE, ICEBERG_CATALOG_NAME,
    S3_ENDPOINT, S3_ACCESS_KEY, S3_SECRET_KEY, S3_REGION, S3_PATH_STYLE_ACCESS,
    OLLAMA_API_KEY, OLLAMA_BASE_URL

Investigator model settings (Architecture.md #22: the model investigates):
``OLLAMA_API_KEY`` (or the legacy ``LOCAL_OLAMMA_API_KEY`` alias) enables the
Ollama investigator automatically; it is normalized into the internal secret
setting :attr:`Settings.ollama_api_key`. The cloud default is pinned to
exactly ``gpt-oss:120b-cloud``; only the endpoint is configurable via
``OLLAMA_BASE_URL`` (an explicitly set local Ollama daemon), defaulting to
Ollama Cloud. A local daemon keeps Ollama's local ``gpt-oss:120b`` tag (the
``-cloud`` tag is hosted-only): the same Ollama gpt-oss 120b model, never
another vendor/model.
"""

import os
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel, Field

LOCAL_ENV = "local"
PRODUCTION_ENV = "production"

_DEFAULT_ENV_FILE = ".env"

# Default Ollama endpoint for the investigator model. The bundled Pydantic AI
# Ollama provider speaks the OpenAI-compatible /v1 path; the supplied Ollama
# Cloud key works only at the native /api/chat endpoint, so the cloud default
# is the native URL. Only overridden by an explicitly set OLLAMA_BASE_URL for a
# local Ollama daemon.
OLLAMA_CLOUD_BASE_URL = "https://ollama.com/v1"
OLLAMA_CLOUD_NATIVE_URL = "https://ollama.com/api/chat"


def _load_env_file(path: str | Path = _DEFAULT_ENV_FILE) -> None:
    """Minimal ``.env`` loader for local development (§50).

    Only sets variables that are not already present in the environment, so
    real environment values always win. Malformed lines are ignored silently:
    a developer convenience file must never crash startup.
    """
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc


class Settings(BaseModel):
    """Typed deployment settings; secrets are excluded from ``repr``."""

    # -- common -------------------------------------------------------------
    sta_env: str = Field(default=LOCAL_ENV)
    db_path: str = Field(default="./sta.sqlite3")
    knowledge_path: str = Field(default="./knowledge")
    max_concurrent_runs: int = Field(default=2, ge=1)
    query_timeout_seconds: int = Field(default=30, ge=1)

    # -- local (Docker Iceberg + PyIceberg + DuckDB) --------------------------
    iceberg_catalog_uri: str | None = None
    iceberg_warehouse: str | None = None
    iceberg_catalog_name: str = "local"
    s3_endpoint: str | None = None
    s3_access_key: str | None = Field(default=None, repr=False)
    s3_secret_key: str | None = Field(default=None, repr=False)
    s3_region: str = "us-east-1"
    s3_path_style_access: bool = True

    # -- production (IOMETE / Spark) -----------------------------------------
    iomete_endpoint: str | None = None
    iomete_catalog: str | None = None
    iomete_token: str | None = Field(default=None, repr=False)

    # -- investigator model (Ollama, cloud default exactly gpt-oss:120b-cloud) -
    # Secret: normalized from OLLAMA_API_KEY or the legacy LOCAL_OLAMMA_API_KEY
    # alias. Excluded from repr/str, masked in safe_summary, and only handed to
    # the Ollama provider — never logged, stored in events, or sent to the model.
    ollama_api_key: str | None = Field(default=None, repr=False)
    ollama_base_url: str | None = None

    @property
    def environment_badge(self) -> str:
        if self.sta_env == PRODUCTION_ENV:
            return f"IOMETE · {self.iomete_catalog or 'production'}"
        return "LOCAL · Docker Iceberg"

    @property
    def s3_fully_configured(self) -> bool:
        """True when the local S3 data-access settings are all present."""
        return bool(self.s3_endpoint and self.s3_access_key and self.s3_secret_key)

    @property
    def local_data_access_mode(self) -> str:
        """Detected local data-access mode (local environment only).

        ``"s3"`` — object storage is configured; DuckDB reads ``s3://`` data
        files through the configured endpoint/credentials.
        ``"file"`` — no S3 configuration; the supported offline mode for
        tables whose data files live on local ``file://`` paths.
        """
        return "s3" if self.s3_fully_configured else "file"

    # -- validation ------------------------------------------------------------

    def validate_environment(self) -> None:
        """Fail fast on missing required configuration for the selected
        environment (§49). Raises ``ValueError`` with an actionable message;
        the app boundary translates this into a startup failure.
        """
        if self.sta_env == LOCAL_ENV:
            missing = [
                name
                for name, value in (("ICEBERG_CATALOG_URI", self.iceberg_catalog_uri),)
                if not value
            ]
            if missing:
                raise ValueError(
                    f"missing required local configuration: {', '.join(missing)} "
                    "(set it in the environment or .env; see .env.example)"
                )
            self._validate_local_s3()
        elif self.sta_env == PRODUCTION_ENV:
            missing = [
                name
                for name, value in (
                    ("IOMETE_ENDPOINT", self.iomete_endpoint),
                    ("IOMETE_CATALOG", self.iomete_catalog),
                )
                if not value
            ]
            if missing:
                raise ValueError(
                    f"missing required production configuration: {', '.join(missing)}"
                )
        else:
            raise ValueError(
                f"unsupported STA_ENV {self.sta_env!r} (expected {LOCAL_ENV!r} or {PRODUCTION_ENV!r})"
            )

    def _validate_local_s3(self) -> None:
        """Local S3 data-access validation (§49): fail fast on configuration
        that cannot work, never on the supported offline mode.

        - a partially set S3 block is rejected with the missing names,
        - an ``s3://`` warehouse without full S3 settings is rejected,
        - no S3 settings at all is the supported ``file://`` offline mode.
        """
        s3_names = ("S3_ENDPOINT", "S3_ACCESS_KEY", "S3_SECRET_KEY")
        s3_values = (self.s3_endpoint, self.s3_access_key, self.s3_secret_key)
        set_names = [name for name, value in zip(s3_names, s3_values) if value]
        if set_names and len(set_names) < len(s3_names):
            missing = [name for name in s3_names if name not in set_names]
            raise ValueError(
                f"partial local S3 configuration: missing {', '.join(missing)} "
                f"(set all of {', '.join(s3_names)}, or none of them for "
                "file:// offline mode)"
            )
        if (
            not self.s3_fully_configured
            and self.iceberg_warehouse
            and self.iceberg_warehouse.startswith("s3://")
        ):
            raise ValueError(
                "ICEBERG_WAREHOUSE is an s3:// location but object storage is not "
                f"configured (set {', '.join(s3_names)}); use a file:// warehouse "
                "for offline mode"
            )

    # -- safe rendering ----------------------------------------------------------

    def safe_summary(self) -> dict[str, str]:
        """Configuration facts that are safe for logs, events and UI badges.

        Secret-valued settings are masked, never echoed; absent settings are
        simply not reported.
        """
        summary = {"STA_ENV": self.sta_env}
        if self.sta_env == PRODUCTION_ENV:
            if self.iomete_endpoint:
                summary["IOMETE_ENDPOINT"] = self.iomete_endpoint
            if self.iomete_catalog:
                summary["IOMETE_CATALOG"] = self.iomete_catalog
            if self.iomete_token:
                summary["IOMETE_TOKEN"] = "***"
            return summary
        if self.iceberg_catalog_uri:
            summary["ICEBERG_CATALOG_URI"] = self.iceberg_catalog_uri
        if self.iceberg_warehouse:
            summary["ICEBERG_WAREHOUSE"] = self.iceberg_warehouse
        if self.s3_endpoint:
            summary["S3_ENDPOINT"] = self.s3_endpoint
        if self.s3_access_key:
            summary["S3_ACCESS_KEY"] = "***"
        if self.s3_secret_key:
            summary["S3_SECRET_KEY"] = "***"
        # Detected data-access mode: makes the offline file:// deployment
        # visible in logs/UI instead of silently assuming object storage.
        summary["DATA_ACCESS_MODE"] = self.local_data_access_mode
        if self.ollama_api_key:
            summary["OLLAMA_API_KEY"] = "***"
            summary["OLLAMA_BASE_URL"] = self.ollama_base_url or OLLAMA_CLOUD_NATIVE_URL
        return summary

    def pyiceberg_properties(self) -> dict[str, str]:
        """PyIceberg ``load_catalog`` properties for the local environment.

        Credentials stay inside the backend/connection layer (§6): this dict
        is consumed only by the local catalog provider and never logged,
        stored, or sent to the model. Callers must ensure the local
        environment is configured (see :meth:`validate_environment`).
        """
        if self.sta_env != LOCAL_ENV:
            raise ValueError("pyiceberg properties are only available for the local environment")
        properties: dict[str, str] = {
            "type": "rest",
            "uri": self.iceberg_catalog_uri or "",
        }
        if self.iceberg_warehouse:
            properties["warehouse"] = self.iceberg_warehouse
        if self.s3_endpoint:
            properties["s3.endpoint"] = self.s3_endpoint
        if self.s3_access_key:
            properties["s3.access-key-id"] = self.s3_access_key
        if self.s3_secret_key:
            properties["s3.secret-access-key"] = self.s3_secret_key
        properties["s3.region"] = self.s3_region
        properties["s3.path-style-access"] = "true" if self.s3_path_style_access else "false"
        return properties

    def s3_properties(self) -> dict[str, str]:
        """S3 data-access properties for local engines (DuckDB httpfs).

        Same secret-handling rules as :meth:`pyiceberg_properties`."""
        properties: dict[str, str] = {"region": self.s3_region}
        if self.s3_endpoint:
            properties["endpoint"] = self.s3_endpoint
        if self.s3_access_key:
            properties["access_key_id"] = self.s3_access_key
        if self.s3_secret_key:
            properties["secret_access_key"] = self.s3_secret_key
        if self.s3_path_style_access:
            properties["url_style"] = "path"
        return properties


@lru_cache
def get_settings() -> Settings:
    """Load settings once per process from the environment (plus ``.env``)."""
    _load_env_file()
    return Settings(
        sta_env=os.getenv("STA_ENV", LOCAL_ENV),
        db_path=os.getenv("STA_DB_PATH", "./sta.sqlite3"),
        knowledge_path=os.getenv("STA_KNOWLEDGE_PATH", "./knowledge"),
        max_concurrent_runs=_env_int("STA_MAX_CONCURRENT_RUNS", 2),
        query_timeout_seconds=_env_int("STA_QUERY_TIMEOUT", 30),
        iceberg_catalog_uri=os.getenv("ICEBERG_CATALOG_URI"),
        iceberg_warehouse=os.getenv("ICEBERG_WAREHOUSE"),
        iceberg_catalog_name=os.getenv("ICEBERG_CATALOG_NAME", "local"),
        s3_endpoint=os.getenv("S3_ENDPOINT"),
        s3_access_key=os.getenv("S3_ACCESS_KEY"),
        s3_secret_key=os.getenv("S3_SECRET_KEY"),
        s3_region=os.getenv("S3_REGION", "us-east-1"),
        s3_path_style_access=os.getenv("S3_PATH_STYLE_ACCESS", "true").strip().lower()
        not in {"0", "false", "no"},
        iomete_endpoint=os.getenv("IOMETE_ENDPOINT"),
        iomete_catalog=os.getenv("IOMETE_CATALOG"),
        iomete_token=os.getenv("IOMETE_TOKEN"),
        # Current name first; the legacy LOCAL_OLAMMA_API_KEY alias keeps the
        # existing local .env working. The value is treated as a secret.
        ollama_api_key=os.getenv("OLLAMA_API_KEY") or os.getenv("LOCAL_OLAMMA_API_KEY"),
        ollama_base_url=os.getenv("OLLAMA_BASE_URL"),
    )