import dayjs from 'dayjs';
import timezone from 'dayjs/plugin/timezone';
import utc from 'dayjs/plugin/utc';

import type { Granularity } from '../types';

dayjs.extend(utc);
dayjs.extend(timezone);

export const DISPLAY_TZ = 'Asia/Shanghai';
const OUTPUT_FORMAT = 'YYYY-MM-DD HH:mm:ss.SSS';

export interface TimeTextPair {
  localText: string;
  utcText: string;
}

const parseNsToMillis = (nsValue: string): number | null => {
  try {
    const normalized = nsValue.replace(/,/g, '').trim();
    if (!normalized) {
      return null;
    }
    const nanos = BigInt(normalized);
    const millis = Number(nanos / 1_000_000n);
    return Number.isFinite(millis) ? millis : null;
  } catch {
    return null;
  }
};

export const formatIsoTimePair = (rawValue?: string | null, tzName = DISPLAY_TZ): TimeTextPair => {
  if (!rawValue) {
    return {
      localText: '-',
      utcText: '-',
    };
  }

  const parsed = dayjs(rawValue);
  if (!parsed.isValid()) {
    return {
      localText: String(rawValue),
      utcText: String(rawValue),
    };
  }

  return {
    localText: `${parsed.tz(tzName).format(OUTPUT_FORMAT)} (UTC+8)`,
    utcText: `${parsed.utc().format(OUTPUT_FORMAT)} (UTC)`,
  };
};

export const formatNsTimePair = (rawValue?: string | null, tzName = DISPLAY_TZ): TimeTextPair => {
  if (!rawValue) {
    return {
      localText: '-',
      utcText: '-',
    };
  }

  const millis = parseNsToMillis(rawValue);
  if (millis === null) {
    return {
      localText: String(rawValue),
      utcText: String(rawValue),
    };
  }

  const parsed = dayjs(millis);
  return {
    localText: `${parsed.tz(tzName).format(OUTPUT_FORMAT)} (UTC+8)`,
    utcText: `${parsed.utc().format(OUTPUT_FORMAT)} (UTC)`,
  };
};

export const formatAxisBucketLabel = (bucketTs: string, granularity: Granularity, tzName = DISPLAY_TZ): string => {
  const hasTimezone = /([zZ]|[+-]\d{2}:\d{2})$/.test(bucketTs);
  const parsed = hasTimezone ? dayjs(bucketTs) : dayjs.utc(bucketTs);
  if (!parsed.isValid()) {
    return bucketTs;
  }
  const local = parsed.tz(tzName);
  if (granularity === 'day') {
    return local.format('MM-DD');
  }
  return local.format('MM-DD HH:mm');
};
