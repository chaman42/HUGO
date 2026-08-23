"""Live environment data: current datetime, session uptime, WiFi/IP-based
location, and Open-Meteo weather — with a background thread that keeps the
location/weather caches warm so the dispatch pipeline never blocks on a
network fetch."""
import json
import logging
import re
import ssl
import subprocess
import threading
import time
import datetime
import urllib.request

try:
    import certifi
    _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    # Fallback: skip verification (macOS Python missing system certs)
    _SSL_CTX = ssl.create_default_context()
    _SSL_CTX.check_hostname = False
    _SSL_CTX.verify_mode    = ssl.CERT_NONE

logger = logging.getLogger(__name__)

FETCH_TIMEOUT      = 3    # hard cap on every external HTTP request (seconds)
LOCATION_CACHE_TTL = 600  # 10 minutes
WEATHER_CACHE_TTL  = 600  # 10 minutes

# Background thread refreshes caches this many seconds after the last fetch —
# 5 minutes before the 10-minute TTL expires, so caches never go cold.
_REFRESH_INTERVAL = WEATHER_CACHE_TTL - 300  # 300 s

# ---------------------------------------------------------------------------
# Known SSID → fixed location (no IP lookup needed when on a known network)
# ---------------------------------------------------------------------------

_KNOWN_SSID_LOCATIONS: dict[str, dict] = {
    "PAXINET": {
        "display": "casa (Valencia, España)",
        "city":    "Valencia",
        "region":  "Comunidad Valenciana",
        "country": "España",
        "lat":     39.4699,
        "lon":     -0.3763,
    },
}

_DAYS_ES = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]


def get_current_datetime_string() -> str:
    """Return 'Hora actual: HH:MM:SS — Fecha: weekday DD/MM/YYYY' using real system clock."""
    now      = datetime.datetime.now()
    day_name = _DAYS_ES[now.weekday()]
    return (
        f"Hora actual: {now.strftime('%H:%M:%S')} — "
        f"Fecha: {day_name} {now.day:02d}/{now.month:02d}/{now.year}"
    )

_session_start_mono = time.monotonic()


def get_session_duration_string() -> str:
    """Return elapsed backend uptime as a short human string, e.g. '1h 12m'."""
    elapsed = int(time.monotonic() - _session_start_mono)
    h, rem = divmod(elapsed, 3600)
    m, s   = divmod(rem, 60)
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"

_location_cache: dict = {"data": None, "timestamp": 0.0, "ssid": None}
_location_lock         = threading.Lock()


def _get_wifi_ssid() -> str | None:
    """Read current WiFi SSID via macOS networksetup. Returns None on any error."""
    try:
        result = subprocess.run(
            ["networksetup", "-getairportnetwork", "en0"],
            capture_output=True, text=True, timeout=FETCH_TIMEOUT,
        )
        # Output format: "Current Wi-Fi Network: SSID_NAME"
        match = re.search(r"Current Wi-Fi Network:\s*(.+)", result.stdout)
        if match:
            return match.group(1).strip()
    except Exception as exc:
        logger.debug("WiFi SSID detection failed: %s", exc)
    return None


def _geolocate_by_ip() -> dict | None:
    """Approximate current location from public IP using ip-api.com (no API key)."""
    try:
        url = "http://ip-api.com/json/?fields=city,regionName,country,lat,lon"
        with urllib.request.urlopen(url, timeout=FETCH_TIMEOUT) as resp:
            data = json.loads(resp.read().decode())
        return {
            "display": f"{data['city']}, {data['country']}",
            "city":    data["city"],
            "region":  data["regionName"],
            "country": data["country"],
            "lat":     float(data["lat"]),
            "lon":     float(data["lon"]),
        }
    except Exception as exc:
        logger.debug("IP geolocation failed: %s", exc)
    return None


def get_location() -> dict:
    """
    Return the current location as a dict: {display, city, region, country, lat, lon}.
    Priority: known SSID mapping > IP geolocation > empty dict (graceful failure).
    Result is cached for LOCATION_CACHE_TTL; cache is invalidated when SSID changes.
    Never raises.
    """
    now_ts       = time.monotonic()
    current_ssid = _get_wifi_ssid()

    with _location_lock:
        cached      = _location_cache["data"]
        cached_ts   = _location_cache["timestamp"]
        cached_ssid = _location_cache["ssid"]

        # Return cached value if still fresh and SSID hasn't changed
        if (
            cached is not None
            and (now_ts - cached_ts) < LOCATION_CACHE_TTL
            and current_ssid == cached_ssid
        ):
            return cached

    # Resolve location: known SSID first, then IP geolocation
    if current_ssid and current_ssid in _KNOWN_SSID_LOCATIONS:
        loc = _KNOWN_SSID_LOCATIONS[current_ssid]
    else:
        loc = _geolocate_by_ip() or {}

    with _location_lock:
        _location_cache["data"]      = loc
        _location_cache["timestamp"] = now_ts
        _location_cache["ssid"]      = current_ssid

    return loc

_WEATHER_CODES: dict[int, str] = {
    0:  "despejado",
    1:  "mayormente despejado",
    2:  "parcialmente nublado",
    3:  "nublado",
    45: "niebla",
    48: "niebla con escarcha",
    51: "llovizna ligera",
    53: "llovizna moderada",
    55: "llovizna densa",
    61: "lluvia ligera",
    63: "lluvia moderada",
    65: "lluvia intensa",
    71: "nevada ligera",
    73: "nevada moderada",
    75: "nevada intensa",
    77: "granizo",
    80: "chubascos ligeros",
    81: "chubascos moderados",
    82: "chubascos fuertes",
    85: "chubascos de nieve ligeros",
    86: "chubascos de nieve fuertes",
    95: "tormenta",
    96: "tormenta con granizo",
    99: "tormenta con granizo fuerte",
}

_weather_cache: dict = {"data": None, "timestamp": 0.0, "lat": None, "lon": None}
_weather_lock         = threading.Lock()


def get_weather(lat: float, lon: float) -> dict | None:
    """
    Fetch current weather from Open-Meteo for (lat, lon).
    Returned dict keys: temperature, feels_like, humidity, wind_speed, condition.
    Cached for WEATHER_CACHE_TTL seconds. Returns None on any failure.
    """
    now_ts = time.monotonic()

    with _weather_lock:
        cached     = _weather_cache["data"]
        cached_ts  = _weather_cache["timestamp"]
        cached_lat = _weather_cache["lat"]
        cached_lon = _weather_cache["lon"]

        if (
            cached is not None
            and (now_ts - cached_ts) < WEATHER_CACHE_TTL
            and cached_lat == lat
            and cached_lon == lon
        ):
            return cached

    try:
        url = (
            "https://api.open-meteo.com/v1/forecast"
            f"?latitude={lat}&longitude={lon}"
            "&current=temperature_2m,relative_humidity_2m,apparent_temperature,"
            "weather_code,wind_speed_10m"
        )
        with urllib.request.urlopen(url, timeout=FETCH_TIMEOUT, context=_SSL_CTX) as resp:
            raw = json.loads(resp.read().decode())

        curr = raw["current"]
        code = int(curr.get("weather_code", -1))
        data: dict = {
            "temperature": curr.get("temperature_2m"),
            "feels_like":  curr.get("apparent_temperature"),
            "humidity":    curr.get("relative_humidity_2m"),
            "wind_speed":  curr.get("wind_speed_10m"),
            "condition":   _WEATHER_CODES.get(code, f"código {code}"),
        }
    except Exception as exc:
        logger.debug("Weather fetch failed: %s", exc)
        return None

    with _weather_lock:
        _weather_cache["data"]      = data
        _weather_cache["timestamp"] = now_ts
        _weather_cache["lat"]       = lat
        _weather_cache["lon"]       = lon

    return data


def get_weather_string(lat: float, lon: float) -> str:
    """Return a one-line weather summary in Spanish, or a graceful fallback."""
    w = get_weather(lat, lon)
    if not w:
        return "clima no disponible en este momento"
    return (
        f"{w['condition']}, {w['temperature']}°C "
        f"(sensación {w['feels_like']}°C), "
        f"humedad {w['humidity']}%, "
        f"viento {w['wind_speed']} km/h"
    )

def _do_refresh() -> None:
    """Unconditionally fetch location and weather and update their caches.

    Bypasses TTL checks — always does a real network/SSID lookup.
    Called exclusively by the background refresh thread; all errors suppressed.
    """
    # Refresh location: SSID lookup → known mapping or IP geolocation
    current_ssid = _get_wifi_ssid()
    if current_ssid and current_ssid in _KNOWN_SSID_LOCATIONS:
        loc: dict = _KNOWN_SSID_LOCATIONS[current_ssid]
    else:
        loc = _geolocate_by_ip() or {}

    now_ts = time.monotonic()
    with _location_lock:
        _location_cache["data"]      = loc
        _location_cache["timestamp"] = now_ts
        _location_cache["ssid"]      = current_ssid

    if loc:
        logger.debug("Background location refresh: %s", loc.get("display", "unknown"))

    # Refresh weather using the freshly resolved coordinates
    lat = loc.get("lat")
    lon = loc.get("lon")
    if lat is None or lon is None:
        return  # no location → skip weather

    try:
        url = (
            "https://api.open-meteo.com/v1/forecast"
            f"?latitude={lat}&longitude={lon}"
            "&current=temperature_2m,relative_humidity_2m,apparent_temperature,"
            "weather_code,wind_speed_10m"
        )
        with urllib.request.urlopen(url, timeout=FETCH_TIMEOUT, context=_SSL_CTX) as resp:
            raw = json.loads(resp.read().decode())

        curr = raw["current"]
        code = int(curr.get("weather_code", -1))
        data: dict = {
            "temperature": curr.get("temperature_2m"),
            "feels_like":  curr.get("apparent_temperature"),
            "humidity":    curr.get("relative_humidity_2m"),
            "wind_speed":  curr.get("wind_speed_10m"),
            "condition":   _WEATHER_CODES.get(code, f"código {code}"),
        }
        with _weather_lock:
            _weather_cache["data"]      = data
            _weather_cache["timestamp"] = now_ts
            _weather_cache["lat"]       = lat
            _weather_cache["lon"]       = lon
        logger.debug("Background weather refresh: %s", data.get("condition", "?"))
    except Exception as exc:
        logger.debug("Background weather refresh failed: %s", exc)


def _background_refresh_loop() -> None:
    """Warm caches immediately at startup, then refresh every _REFRESH_INTERVAL seconds.

    Runs in a daemon thread — never touches the dispatch pipeline.
    With WEATHER_CACHE_TTL=600 and _REFRESH_INTERVAL=300, the cache is always
    refreshed 5 minutes before expiry, eliminating cold cache misses on dispatch.
    """
    while True:
        try:
            _do_refresh()
        except Exception as exc:
            logger.debug("Background refresh loop error: %s", exc)
        time.sleep(_REFRESH_INTERVAL)


# Start background refresh immediately at module load.
# Caches are warm before any command arrives; dispatch never blocks on a fetch.
threading.Thread(
    target=_background_refresh_loop,
    daemon=True,
    name="tools-bg-refresh",
).start()
