from math import radians, sin, cos, asin, sqrt
from app.config import settings

# Geofence radius around home (km). A residence complex spans several buildings —
# tune to the footprint so "home" means anywhere on the property (100 m covers the suite's building).
HOME_RADIUS_KM = 0.1


def km_from_home(lat: float, lng: float) -> float:
    dlat = radians(lat - settings.home_lat)
    dlng = radians(lng - settings.home_lng)
    a = (sin(dlat / 2) ** 2
         + cos(radians(settings.home_lat)) * cos(radians(lat)) * sin(dlng / 2) ** 2)
    return 2 * 6371 * asin(sqrt(a))   # kilometers


def describe(lat: float, lng: float) -> str:
    if settings.home_lat is None or settings.home_lng is None:
        return f"at {lat:.4f}, {lng:.4f}"   # home not configured yet
    d = km_from_home(lat, lng)
    return "home" if d < HOME_RADIUS_KM else f"{d:.1f} km from home"
