from __future__ import annotations

import os
from dataclasses import dataclass
from typing import List


@dataclass(frozen=True)
class Settings:
    mongo_uri: str
    mongo_db: str = "map_db_dev"
    mongo_uri_ubddev: str = ""
    mongo_db_ubddev: str = "map_db_dev"
    api_prefix: str = "/api/v1"
    timezone: str = "Asia/Shanghai"
    default_tz: str = "Asia/Shanghai"
    cors_origins: List[str] = None
    index_ensure_mode: str = "auto"
    max_query_days: int = 31
    default_time_range_hours: int = 24
    slow_call_threshold_s: float = 10.0
    grafana_url: str = ""
    grafana_user: str = ""
    grafana_password: str = ""
    loki_ds_uid: str = "bex1a2pgx8oowd"
    friday_model_base_url: str = ""
    friday_model_name: str = ""
    friday_model_env_file: str = "./friday_model.env"
    friday_model_timeout_s: int = 180

    @staticmethod
    def _parse_bool(raw: str, default: bool) -> bool:
        if raw is None:
            return default
        return raw.strip().lower() in {"1", "true", "yes", "y", "on"}

    @staticmethod
    def _parse_origins(raw: str) -> List[str]:
        if not raw:
            return ["*"]
        return [item.strip() for item in raw.split(",") if item.strip()]

    @classmethod
    def _parse_index_mode(cls, raw_mode: str, raw_auto_create_indexes: str) -> str:
        valid = {"auto", "skip", "required"}
        if raw_mode:
            normalized = raw_mode.strip().lower()
            if normalized in valid:
                return normalized
            return "auto"

        if raw_auto_create_indexes is not None:
            return "auto" if cls._parse_bool(raw_auto_create_indexes, False) else "skip"

        return "auto"

    @classmethod
    def from_env(cls) -> "Settings":
        mongo_uri = os.getenv("MONGO_URI", "")
        mongo_db = os.getenv("MONGO_DB", "map_db_dev")
        mongo_uri_ubddev = os.getenv("MONGO_URI_UBDDEV", "")
        mongo_db_ubddev = os.getenv("MONGO_DB_UBDDEV", "map_db_dev")
        api_prefix = os.getenv("API_PREFIX", "/api/v1")
        timezone = os.getenv("TIMEZONE", "Asia/Shanghai")
        default_tz = os.getenv("DEFAULT_TZ", "Asia/Shanghai")
        cors_origins = cls._parse_origins(os.getenv("CORS_ORIGINS", "*"))
        index_ensure_mode = cls._parse_index_mode(
            os.getenv("INDEX_ENSURE_MODE"),
            os.getenv("AUTO_CREATE_INDEXES"),
        )
        max_query_days = int(os.getenv("MAX_QUERY_DAYS", "31"))
        default_time_range_hours = int(os.getenv("DEFAULT_TIME_RANGE_HOURS", "24"))
        slow_call_threshold_s = float(os.getenv("SLOW_CALL_THRESHOLD_S", "10"))
        grafana_url = os.getenv("GRAFANA_URL", "").rstrip("/")
        grafana_user = os.getenv("GRAFANA_USER", "")
        grafana_password = os.getenv("GRAFANA_PASSWORD", "")
        loki_ds_uid = os.getenv("LOKI_DS_UID", "bex1a2pgx8oowd")
        friday_model_base_url = os.getenv("FRIDAY_MODEL_BASE_URL", "").rstrip("/")
        friday_model_name = os.getenv("FRIDAY_MODEL_NAME", "")
        friday_model_env_file = os.getenv("FRIDAY_MODEL_ENV_FILE", "./friday_model.env")
        friday_model_timeout_s = int(os.getenv("FRIDAY_MODEL_TIMEOUT_S", "180"))

        return cls(
            mongo_uri=mongo_uri,
            mongo_db=mongo_db,
            mongo_uri_ubddev=mongo_uri_ubddev,
            mongo_db_ubddev=mongo_db_ubddev,
            api_prefix=api_prefix,
            timezone=timezone,
            default_tz=default_tz,
            cors_origins=cors_origins,
            index_ensure_mode=index_ensure_mode,
            max_query_days=max_query_days,
            default_time_range_hours=default_time_range_hours,
            slow_call_threshold_s=slow_call_threshold_s,
            grafana_url=grafana_url,
            grafana_user=grafana_user,
            grafana_password=grafana_password,
            loki_ds_uid=loki_ds_uid,
            friday_model_base_url=friday_model_base_url,
            friday_model_name=friday_model_name,
            friday_model_env_file=friday_model_env_file,
            friday_model_timeout_s=friday_model_timeout_s,
        )
