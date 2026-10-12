"""Prayer times, computed locally (no API). adhanpy is a pure-Python port of the Adhan
library. Method and Asr school come from config (default ISNA + standard/Shafi'i Asr,
matching common Athan apps), at the home coordinates.
"""
from datetime import date, datetime
from zoneinfo import ZoneInfo

from adhanpy.PrayerTimes import PrayerTimes
from adhanpy.calculation.CalculationMethod import CalculationMethod
from adhanpy.calculation.CalculationParameters import CalculationParameters
from adhanpy.calculation.Madhab import Madhab

from app.config import settings

ORDER = ("fajr", "sunrise", "dhuhr", "asr", "maghrib", "isha")
PRAYERS = ("fajr", "dhuhr", "asr", "maghrib", "isha")       # sunrise isn't a prayer
NAMES = {
    "fajr": ("الفجر", "Fajr"), "sunrise": ("الشروق", "Sunrise"), "dhuhr": ("الضهر", "Dhuhr"),
    "asr": ("العصر", "Asr"), "maghrib": ("المغرب", "Maghrib"), "isha": ("العشا", "Isha"),
}


def is_configured() -> bool:
    return settings.home_lat is not None and settings.home_lng is not None


def times_for(day: date, lat: float | None = None, lng: float | None = None,
              tz_name: str | None = None) -> dict[str, datetime]:
    """Timezone-aware times for one LOCAL calendar day, at home unless a place is given
    (travel mode passes the member's place and its timezone).

    adhanpy derives the day from the datetime it's given as if it were UTC, so a local
    evening timestamp in Toronto would yield tomorrow's times. Passing a plain midnight
    for the local date sidesteps that.
    """
    params = CalculationParameters(method=CalculationMethod[settings.prayer_method])
    params.madhab = Madhab[settings.prayer_asr]
    tz = ZoneInfo(tz_name or settings.timezone)
    coords = (lat, lng) if lat is not None and lng is not None else (settings.home_lat, settings.home_lng)
    pt = PrayerTimes(coords, datetime(day.year, day.month, day.day),
                     calculation_parameters=params, time_zone=tz)
    return {name: getattr(pt, name).astimezone(tz) for name in ORDER}


def times_at(day: date, loc) -> dict[str, datetime]:
    """times_for at a location.Location."""
    return times_for(day, loc.lat, loc.lng, loc.tz)


def clock12(dt: datetime) -> str:
    """'3:52 p.m.' style, no leading zero."""
    h = dt.hour % 12 or 12
    return f"{h}:{dt.minute:02d} {'a.m.' if dt.hour < 12 else 'p.m.'}"


def describe_day(day: date, loc=None) -> str:
    """One line per prayer, for the tool/briefing (at `loc` if given, else home)."""
    t = times_at(day, loc) if loc is not None else times_for(day)
    return "\n".join(f"- {NAMES[n][1]} ({NAMES[n][0]}): {clock12(t[n])}" for n in ORDER)
