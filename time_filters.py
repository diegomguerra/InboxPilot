# time_filters.py
from datetime import datetime, timedelta, timezone
from typing import Tuple, Optional, Dict, Any

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None

DEFAULT_TZ = "America/Sao_Paulo"
MAX_CUSTOM_RANGE_DAYS = 90

def _tz(tz_name: str):
    if ZoneInfo:
        return ZoneInfo(tz_name)
    return timezone(timedelta(hours=-3))


def build_date_range(
    filter_type: str,
    tz_name: str = DEFAULT_TZ,
    now: Optional[datetime] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
    n_days: Optional[int] = None
) -> Dict[str, Any]:
    """
    Unified date range builder.
    
    Args:
        filter_type: today | current_week | last_n_days | custom | rolling_week | rolling_month | current_month
        tz_name: Timezone name (default: America/Sao_Paulo)
        now: Override current time (for testing)
        start: Start date for custom range (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS)
        end: End date for custom range (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS)
        n_days: Number of days for last_n_days filter
    
    Returns:
        Dict with:
            - start_utc: datetime in UTC
            - end_utc: datetime in UTC
            - start_local_iso: ISO string in local time
            - end_local_iso: ISO string in local time
            - filter_type: normalized filter type
            - tz_name: timezone used
            - description: human readable description (e.g., "Mon 00:00 → Sun 23:59")
    """
    tz = _tz(tz_name)
    
    if now is None:
        now_local = datetime.now(tz)
    elif now.tzinfo is None:
        now_local = now.replace(tzinfo=tz)
    else:
        now_local = now.astimezone(tz)
    
    description = ""
    
    if filter_type == "today":
        s_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        e_local = now_local.replace(hour=23, minute=59, second=59, microsecond=999999)
        description = f"Today ({s_local.strftime('%Y-%m-%d')} 00:00 → 23:59)"
    
    elif filter_type == "current_week":
        monday = now_local - timedelta(days=now_local.weekday())
        s_local = monday.replace(hour=0, minute=0, second=0, microsecond=0)
        sunday = s_local + timedelta(days=6)
        e_local = sunday.replace(hour=23, minute=59, second=59, microsecond=999999)
        description = f"This Week ({s_local.strftime('%a %d')} 00:00 → {e_local.strftime('%a %d')} 23:59)"
    
    elif filter_type == "last_n_days":
        if not n_days or n_days < 1:
            n_days = 7
        if n_days > 60:
            n_days = 60
        today_start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        s_local = today_start - timedelta(days=n_days - 1)
        e_local = now_local.replace(hour=23, minute=59, second=59, microsecond=999999)
        s_date = s_local.strftime('%d/%m')
        e_date = e_local.strftime('%d/%m')
        description = f"Last {n_days} days ({s_date} → {e_date})"
    
    elif filter_type in ("rolling_week", "week"):
        e_local = now_local
        s_local = now_local - timedelta(days=7)
        description = f"Rolling 7 days ({s_local.strftime('%Y-%m-%d %H:%M')} → now)"
    
    elif filter_type == "current_month":
        s_local = now_local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        e_local = now_local.replace(hour=23, minute=59, second=59, microsecond=999999)
        description = f"This Month ({s_local.strftime('%b %d')} → {e_local.strftime('%b %d')})"
    
    elif filter_type in ("rolling_month", "month"):
        e_local = now_local
        s_local = now_local - timedelta(days=30)
        description = f"Rolling 30 days ({s_local.strftime('%Y-%m-%d')} → now)"
    
    elif filter_type == "custom":
        if not start or not end:
            raise ValueError("custom filter requires start and end dates")
        
        s_local = _parse_local_datetime(start, tz, is_start=True)
        e_local = _parse_local_datetime(end, tz, is_start=False)
        
        if s_local > e_local:
            raise ValueError("start date must be before end date")
        
        range_days = (e_local - s_local).days
        if range_days > MAX_CUSTOM_RANGE_DAYS:
            raise ValueError(f"custom range exceeds maximum of {MAX_CUSTOM_RANGE_DAYS} days")
        
        description = f"Custom ({s_local.strftime('%Y-%m-%d %H:%M')} → {e_local.strftime('%Y-%m-%d %H:%M')})"
    
    else:
        e_local = now_local
        s_local = now_local - timedelta(days=7)
        filter_type = "rolling_week"
        description = f"Rolling 7 days (default)"
    
    start_utc = s_local.astimezone(timezone.utc)
    end_utc = e_local.astimezone(timezone.utc)
    
    return {
        "start_utc": start_utc,
        "end_utc": end_utc,
        "start_local_iso": s_local.isoformat(),
        "end_local_iso": e_local.isoformat(),
        "start_local": s_local,
        "end_local": e_local,
        "filter_type": filter_type,
        "tz_name": tz_name,
        "description": description
    }


def _parse_local_datetime(value: str, tz, is_start: bool = True) -> datetime:
    """Parse a date/datetime string as local time."""
    if len(value) == 10:
        dt = datetime.fromisoformat(value)
        if is_start:
            dt = dt.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=tz)
        else:
            dt = dt.replace(hour=23, minute=59, second=59, microsecond=999999, tzinfo=tz)
        return dt
    
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt.astimezone(tz)


def period_to_range(
    period: str,
    tz_name: str = DEFAULT_TZ,
    start: Optional[str] = None,
    end: Optional[str] = None,
    n_days: Optional[int] = None
) -> Tuple[datetime, datetime]:
    """
    Legacy function - returns (start_utc, end_utc) tuple.
    Use build_date_range() for new code.
    """
    result = build_date_range(
        filter_type=period,
        tz_name=tz_name,
        start=start,
        end=end,
        n_days=n_days
    )
    return (result["start_utc"], result["end_utc"])


def get_date_range_info(
    date_mode: str,
    rolling_days: int = 7,
    from_date: str = None,
    to_date: str = None
) -> Dict[str, Any]:
    """
    Unified date range function for session_api and export_api.
    
    Args:
        date_mode: today | current_week | week | rolling | custom | last_n_days | etc.
        rolling_days: Number of days for rolling/last_n_days modes
        from_date: Start date for custom range
        to_date: End date for custom range
    
    Returns:
        Full date range info dict from build_date_range()
    """
    mode_map = {
        "today": "today",
        "current_week": "current_week",
        "this_week": "current_week",
        "rolling": "last_n_days",
        "rolling_week": "last_n_days",
        "week": "last_n_days",
        "last_n_days": "last_n_days",
        "current_month": "current_month",
        "this_month": "current_month",
        "rolling_month": "rolling_month",
        "month": "rolling_month",
        "custom": "custom",
        "custom_range": "custom",
    }
    
    filter_type = mode_map.get(date_mode, "today")
    
    n_days = None
    if filter_type == "last_n_days":
        n_days = rolling_days or 7
    
    try:
        return build_date_range(
            filter_type=filter_type,
            tz_name=DEFAULT_TZ,
            start=from_date,
            end=to_date,
            n_days=n_days
        )
    except ValueError as e:
        return build_date_range(filter_type="today", tz_name=DEFAULT_TZ)
