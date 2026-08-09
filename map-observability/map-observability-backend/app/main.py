from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import Settings
from app.core.database import MongoCollections, create_client, get_database
from app.routers import analytics, correlation, friday, health
from app.services.analytics_service import AnalyticsService
from app.services.correlation_service import CorrelationService
from app.services.friday_service import FridayService
from app.services.indexes import ensure_indexes
from app.services.loki_query_service import LokiQueryService
from app.services.time_align_service import TimeAlignService

logger = logging.getLogger(__name__)


class DisabledCorrelationService:
    def time_align(self, *args, **kwargs):
        raise RuntimeError("Correlation service is not configured")

    def get_rid_correlation(self, *args, **kwargs):
        raise RuntimeError("Correlation service is not configured")

    def get_error_clusters(self, *args, **kwargs):
        raise RuntimeError("Correlation service is not configured")

    def get_tool_call_correlation(self, *args, **kwargs):
        raise RuntimeError("Correlation service is not configured")


class DisabledFridayService:
    def get_config(self, *args, **kwargs):
        raise RuntimeError("Friday service is not configured")

    def update_config(self, *args, **kwargs):
        raise RuntimeError("Friday service is not configured")

    def stream_chat(self, *args, **kwargs):
        raise RuntimeError("Friday service is not configured")

    def get_report_config(self, *args, **kwargs):
        raise RuntimeError("Friday service is not configured")

    def update_report_config(self, *args, **kwargs):
        raise RuntimeError("Friday service is not configured")

    def list_reports(self, *args, **kwargs):
        raise RuntimeError("Friday service is not configured")

    def get_report(self, *args, **kwargs):
        raise RuntimeError("Friday service is not configured")

    def run_report(self, *args, **kwargs):
        raise RuntimeError("Friday service is not configured")


def create_app(
    settings_override: Optional[Settings] = None,
    analytics_service_override: Optional[AnalyticsService] = None,
    correlation_service_override: Optional[CorrelationService] = None,
    friday_service_override: Optional[FridayService] = None,
) -> FastAPI:
    settings = settings_override or Settings.from_env()
    collections = MongoCollections()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.settings = settings
        app.state.mongo_client = None
        app.state.mongo_client_ubddev = None
        app.state.analytics_service_ubddev = None
        app.state.correlation_service_ubddev = None

        if analytics_service_override is not None and correlation_service_override is not None:
            app.state.analytics_service = analytics_service_override
            app.state.correlation_service = correlation_service_override
            app.state.friday_service = friday_service_override or DisabledFridayService()
            yield
            return

        if analytics_service_override is not None and correlation_service_override is None:
            app.state.analytics_service = analytics_service_override
            app.state.correlation_service = DisabledCorrelationService()
            app.state.friday_service = friday_service_override or DisabledFridayService()
            yield
            return

        mongo_client = create_client(settings.mongo_uri)
        app.state.mongo_client = mongo_client
        database = get_database(mongo_client, settings.mongo_db)
        loki_query_service = LokiQueryService(
            grafana_url=settings.grafana_url,
            grafana_user=settings.grafana_user,
            grafana_password=settings.grafana_password,
            loki_ds_uid=settings.loki_ds_uid,
        )
        app.state.analytics_service = analytics_service_override or AnalyticsService(
            database=database,
            settings=settings,
            collections=collections,
            loki_query_service=loki_query_service,
        )
        app.state.correlation_service = correlation_service_override or CorrelationService(
            database=database,
            collections=collections,
            time_align_service=TimeAlignService(default_tz=settings.default_tz),
            loki_query_service=loki_query_service,
        )
        app.state.friday_service = friday_service_override or FridayService(
            settings=settings,
            analytics_service=app.state.analytics_service,
            correlation_service=app.state.correlation_service,
        )
        if hasattr(app.state.friday_service, "start_scheduler"):
            app.state.friday_service.start_scheduler()

        if settings.mongo_uri_ubddev:
            mongo_client_ubddev = create_client(settings.mongo_uri_ubddev)
            app.state.mongo_client_ubddev = mongo_client_ubddev
            database_ubddev = get_database(mongo_client_ubddev, settings.mongo_db_ubddev)
            app.state.analytics_service_ubddev = AnalyticsService(
                database=database_ubddev,
                settings=settings,
                collections=collections,
                loki_query_service=loki_query_service,
                trusted_container_filters={"map_core-preprod"},
            )
            app.state.correlation_service_ubddev = CorrelationService(
                database=database_ubddev,
                collections=collections,
                time_align_service=TimeAlignService(default_tz=settings.default_tz),
                loki_query_service=loki_query_service,
            )

        if settings.index_ensure_mode != "skip":
            ensure_indexes(
                database=database,
                collections=collections,
                ignore_auth_error=settings.index_ensure_mode == "auto",
            )
            if settings.index_ensure_mode == "auto":
                logger.info("MongoDB index ensure mode: auto")
            elif settings.index_ensure_mode == "required":
                logger.info("MongoDB index ensure mode: required")

        try:
            yield
        finally:
            if hasattr(app.state.friday_service, "stop_scheduler"):
                await app.state.friday_service.stop_scheduler()
            mongo_client.close()
            if app.state.mongo_client_ubddev is not None:
                app.state.mongo_client_ubddev.close()

    app = FastAPI(title="MAP Log Analytics API", version="0.1.0", lifespan=lifespan)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(health.router, prefix=settings.api_prefix)
    app.include_router(analytics.router, prefix=settings.api_prefix)
    app.include_router(correlation.router, prefix=settings.api_prefix)
    app.include_router(friday.router, prefix=settings.api_prefix)

    return app


app = create_app()
