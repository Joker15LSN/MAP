from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class LokiQueryService:
    def __init__(
        self,
        grafana_url: str,
        grafana_user: str,
        grafana_password: str,
        loki_ds_uid: str,
        timeout_seconds: int = 20,
    ) -> None:
        self.grafana_url = grafana_url.rstrip("/")
        self.grafana_user = grafana_user
        self.grafana_password = grafana_password
        self.loki_ds_uid = loki_ds_uid
        self.timeout_seconds = timeout_seconds

    def is_enabled(self) -> bool:
        return all([self.grafana_url, self.grafana_user, self.grafana_password, self.loki_ds_uid])

    def _auth_header(self) -> str:
        raw = f"{self.grafana_user}:{self.grafana_password}".encode()
        return f"Basic {base64.b64encode(raw).decode('utf-8')}"

    def _request_json(self, path: str, params: dict[str, str]) -> dict:
        if not self.is_enabled():
            raise RuntimeError("Grafana/Loki integration is not configured")

        query = urlencode(params)
        url = f"{self.grafana_url}{path}?{query}"
        request = Request(url=url, headers={"Authorization": self._auth_header()})
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                payload = response.read().decode("utf-8")
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore") if exc.fp else str(exc)
            detail = detail[:500] if detail else str(exc)
            raise RuntimeError(f"Loki query failed: HTTP {exc.code} - {detail}") from exc
        except URLError as exc:
            raise RuntimeError(f"Loki query failed: {exc.reason}") from exc
        except TimeoutError as exc:
            raise RuntimeError("Loki query timed out") from exc

        try:
            return json.loads(payload)
        except json.JSONDecodeError as exc:
            raise RuntimeError("Loki query returned non-JSON response") from exc

    def query_range(
        self,
        query: str,
        start_ns: int,
        end_ns: int,
        limit: int = 200,
        direction: str = "backward",
    ) -> list[dict]:
        path = f"/api/datasources/proxy/uid/{self.loki_ds_uid}/loki/api/v1/query_range"
        response = self._request_json(
            path=path,
            params={
                "query": query,
                "start": str(start_ns),
                "end": str(end_ns),
                "limit": str(limit),
                "direction": direction,
            },
        )
        data = response.get("data") or {}
        result = data.get("result") if isinstance(data, dict) else []
        if not isinstance(result, list):
            return []

        rows: list[dict] = []
        for stream in result:
            stream_labels = stream.get("stream") if isinstance(stream, dict) else {}
            values = stream.get("values") if isinstance(stream, dict) else []
            if not isinstance(values, list):
                continue

            for raw_item in values:
                if not isinstance(raw_item, list) or len(raw_item) < 2:
                    continue
                ts_ns = str(raw_item[0])
                line = str(raw_item[1])
                try:
                    ts_int = int(ts_ns)
                    ts_utc = datetime.fromtimestamp(ts_int / 1_000_000_000, tz=UTC).isoformat()
                except (ValueError, OSError):
                    ts_utc = None
                rows.append(
                    {
                        "ts_ns": ts_ns,
                        "ts_utc": ts_utc,
                        "line": line,
                        "stream": stream_labels if isinstance(stream_labels, dict) else {},
                    }
                )

        rows.sort(key=lambda item: item.get("ts_ns", "0"))
        return rows
