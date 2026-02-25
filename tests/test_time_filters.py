import pytest
from datetime import datetime, timezone, timedelta
from time_filters import build_date_range, period_to_range, get_date_range_info

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None


def _make_tz():
    if ZoneInfo:
        return ZoneInfo("America/Sao_Paulo")
    return timezone(timedelta(hours=-3))


class TestBuildDateRange:
    
    def test_today_range(self):
        tz = _make_tz()
        now = datetime(2026, 1, 28, 14, 30, 0, tzinfo=tz)
        
        result = build_date_range("today", "America/Sao_Paulo", now=now)
        
        assert result["filter_type"] == "today"
        assert result["start_local"].hour == 0
        assert result["start_local"].minute == 0
        assert result["end_local"].hour == 23
        assert result["end_local"].minute == 59
        assert result["start_local"].day == 28
        assert result["end_local"].day == 28
        assert "Today" in result["description"]
    
    def test_current_week_monday_to_sunday(self):
        tz = _make_tz()
        now = datetime(2026, 1, 28, 14, 30, 0, tzinfo=tz)
        
        result = build_date_range("current_week", "America/Sao_Paulo", now=now)
        
        assert result["filter_type"] == "current_week"
        assert result["start_local"].weekday() == 0
        assert result["start_local"].day == 26
        assert result["end_local"].weekday() == 6
        assert result["end_local"].day == 1
        assert result["end_local"].month == 2
        assert "This Week" in result["description"]
    
    def test_last_n_days(self):
        tz = _make_tz()
        now = datetime(2026, 1, 28, 14, 30, 0, tzinfo=tz)
        
        result = build_date_range("last_n_days", "America/Sao_Paulo", now=now, n_days=7)
        
        assert result["filter_type"] == "last_n_days"
        assert result["start_local"].day == 22
        assert result["start_local"].hour == 0
        assert result["end_local"].day == 28
        assert result["end_local"].hour == 23
        assert "Last 7 days" in result["description"]
    
    def test_last_n_days_default_to_7(self):
        tz = _make_tz()
        now = datetime(2026, 1, 28, 14, 30, 0, tzinfo=tz)
        
        result = build_date_range("last_n_days", "America/Sao_Paulo", now=now, n_days=None)
        
        assert "Last 7 days" in result["description"]
    
    def test_last_n_days_max_60(self):
        tz = _make_tz()
        now = datetime(2026, 1, 28, 14, 30, 0, tzinfo=tz)
        
        result = build_date_range("last_n_days", "America/Sao_Paulo", now=now, n_days=100)
        
        assert "Last 60 days" in result["description"]
    
    def test_custom_date_only(self):
        tz = _make_tz()
        now = datetime(2026, 1, 28, 14, 30, 0, tzinfo=tz)
        
        result = build_date_range(
            "custom", "America/Sao_Paulo", 
            now=now, 
            start="2026-01-20", 
            end="2026-01-25"
        )
        
        assert result["filter_type"] == "custom"
        assert result["start_local"].day == 20
        assert result["start_local"].hour == 0
        assert result["end_local"].day == 25
        assert result["end_local"].hour == 23
        assert result["end_local"].minute == 59
    
    def test_custom_with_time(self):
        tz = _make_tz()
        now = datetime(2026, 1, 28, 14, 30, 0, tzinfo=tz)
        
        result = build_date_range(
            "custom", "America/Sao_Paulo", 
            now=now, 
            start="2026-01-20T10:00:00", 
            end="2026-01-25T18:00:00"
        )
        
        assert result["start_local"].hour == 10
        assert result["end_local"].hour == 18
    
    def test_custom_invalid_range(self):
        tz = _make_tz()
        now = datetime(2026, 1, 28, 14, 30, 0, tzinfo=tz)
        
        with pytest.raises(ValueError, match="start date must be before end date"):
            build_date_range(
                "custom", "America/Sao_Paulo", 
                now=now, 
                start="2026-01-25", 
                end="2026-01-20"
            )
    
    def test_custom_exceeds_max_days(self):
        tz = _make_tz()
        now = datetime(2026, 1, 28, 14, 30, 0, tzinfo=tz)
        
        with pytest.raises(ValueError, match="exceeds maximum"):
            build_date_range(
                "custom", "America/Sao_Paulo", 
                now=now, 
                start="2025-01-01", 
                end="2026-01-25"
            )
    
    def test_custom_missing_dates(self):
        tz = _make_tz()
        now = datetime(2026, 1, 28, 14, 30, 0, tzinfo=tz)
        
        with pytest.raises(ValueError, match="requires start and end"):
            build_date_range("custom", "America/Sao_Paulo", now=now)
    
    def test_current_month(self):
        tz = _make_tz()
        now = datetime(2026, 1, 28, 14, 30, 0, tzinfo=tz)
        
        result = build_date_range("current_month", "America/Sao_Paulo", now=now)
        
        assert result["start_local"].day == 1
        assert result["start_local"].month == 1
        assert result["end_local"].day == 28
    
    def test_rolling_month(self):
        tz = _make_tz()
        now = datetime(2026, 1, 28, 14, 30, 0, tzinfo=tz)
        
        result = build_date_range("rolling_month", "America/Sao_Paulo", now=now)
        
        expected_start = now - timedelta(days=30)
        assert result["start_local"].day == expected_start.day
    
    def test_utc_conversion(self):
        tz = _make_tz()
        now = datetime(2026, 1, 28, 2, 0, 0, tzinfo=tz)
        
        result = build_date_range("today", "America/Sao_Paulo", now=now)
        
        assert result["start_utc"].tzinfo == timezone.utc
        assert result["end_utc"].tzinfo == timezone.utc
        assert result["start_utc"].hour == 3
    
    def test_edge_case_year_change(self):
        tz = _make_tz()
        now = datetime(2026, 1, 2, 10, 0, 0, tzinfo=tz)
        
        result = build_date_range("current_week", "America/Sao_Paulo", now=now)
        
        assert result["start_local"].year == 2025
        assert result["start_local"].month == 12
        assert result["start_local"].day == 29


class TestPeriodToRange:
    
    def test_returns_tuple(self):
        result = period_to_range("today", "America/Sao_Paulo")
        
        assert isinstance(result, tuple)
        assert len(result) == 2
        assert isinstance(result[0], datetime)
        assert isinstance(result[1], datetime)
    
    def test_compatibility_with_old_api(self):
        start, end = period_to_range("current_week", "America/Sao_Paulo")
        
        assert start < end
        assert start.tzinfo == timezone.utc
        assert end.tzinfo == timezone.utc


class TestGetDateRangeInfo:
    
    def test_mode_mapping_today(self):
        result = get_date_range_info("today")
        assert result["filter_type"] == "today"
    
    def test_mode_mapping_current_week(self):
        result = get_date_range_info("current_week")
        assert result["filter_type"] == "current_week"
    
    def test_mode_mapping_week_to_last_n_days(self):
        result = get_date_range_info("week", rolling_days=7)
        assert result["filter_type"] == "last_n_days"
    
    def test_mode_mapping_rolling(self):
        result = get_date_range_info("rolling", rolling_days=14)
        assert result["filter_type"] == "last_n_days"
        assert "Last 14 days" in result["description"]
    
    def test_mode_mapping_custom(self):
        result = get_date_range_info("custom", from_date="2026-01-20", to_date="2026-01-25")
        assert result["filter_type"] == "custom"
    
    def test_invalid_mode_defaults_to_today(self):
        result = get_date_range_info("invalid_mode")
        assert result["filter_type"] == "today"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
