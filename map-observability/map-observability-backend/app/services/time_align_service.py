from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from dateutil import parser


@dataclass(frozen=True)
class AlignedRange:
    timezone: str
    start_local: datetime
    end_local: datetime
    start_utc: datetime
    end_utc: datetime
    start_ns: int
    end_ns: int
    buffer_seconds: int
    buffered_start_utc: datetime
    buffered_end_utc: datetime
    buffered_start_ns: int
    buffered_end_ns: int

    def to_payload(self) -> dict:
        return {
            "timezone": self.timezone,
            "start_local": self.start_local.isoformat(),
            "end_local": self.end_local.isoformat(),
            "start_utc": self.start_utc.isoformat(),
            "end_utc": self.end_utc.isoformat(),
            "start_ns": str(self.start_ns),
            "end_ns": str(self.end_ns),
            "buffer_seconds": self.buffer_seconds,
            "buffered_start_utc": self.buffered_start_utc.isoformat(),
            "buffered_end_utc": self.buffered_end_utc.isoformat(),
            "buffered_start_ns": str(self.buffered_start_ns),
            "buffered_end_ns": str(self.buffered_end_ns),
        }


class TimeAlignService:
    def __init__(self, default_tz: str = "Asia/Shanghai") -> None:
        self.default_tz = default_tz

    @staticmethod
    def _to_ns(value: datetime) -> int:
        return int(value.timestamp() * 1_000_000_000)

    @staticmethod
    def _parse_datetime(raw_value: str, tz_name: str) -> datetime:
        dt = parser.isoparse(raw_value)
        tz = ZoneInfo(tz_name)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=tz)
        return dt.astimezone(tz)

    def align_range(
        self,
        start_local: str,
        end_local: str,
        tz_name: str | None = None,
        buffer_seconds: int = 120,
    ) -> AlignedRange:
        timezone_name = tz_name or self.default_tz
        if not timezone_name:
            timezone_name = "Asia/Shanghai"

        start_local_dt = self._parse_datetime(start_local, timezone_name)
        end_local_dt = self._parse_datetime(end_local, timezone_name)
        if end_local_dt < start_local_dt:
            raise ValueError("end_local must be greater than or equal to start_local")

        start_utc = start_local_dt.astimezone(UTC)
        end_utc = end_local_dt.astimezone(UTC)

        delta = timedelta(seconds=max(buffer_seconds, 0))
        buffered_start_utc = start_utc - delta
        buffered_end_utc = end_utc + delta

        return AlignedRange(
            timezone=timezone_name,
            start_local=start_local_dt,
            end_local=end_local_dt,
            start_utc=start_utc,
            end_utc=end_utc,
            start_ns=self._to_ns(start_utc),
            end_ns=self._to_ns(end_utc),
            buffer_seconds=max(buffer_seconds, 0),
            buffered_start_utc=buffered_start_utc,
            buffered_end_utc=buffered_end_utc,
            buffered_start_ns=self._to_ns(buffered_start_utc),
            buffered_end_ns=self._to_ns(buffered_end_utc),
        )
