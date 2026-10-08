"""Weather (Open-Meteo, free, no key) and traffic-aware travel time (Google Routes)."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import httpx

from ..actions import Tier, Tool, ToolError, schema
from ..config import LocationConfig

FORECAST = "https://api.open-meteo.com/v1/forecast"
GEOCODE = "https://geocoding-api.open-meteo.com/v1/search"
ROUTES = "https://routes.googleapis.com/directions/v2:computeRoutes"

# WMO weather codes, condensed for speech.
_WMO = {
    0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast", 45: "foggy", 48: "foggy",
    51: "light drizzle", 53: "drizzle", 55: "heavy drizzle", 61: "light rain", 63: "rain",
    65: "heavy rain", 66: "freezing rain", 67: "freezing rain", 71: "light snow", 73: "snow",
    75: "heavy snow", 77: "snow grains", 80: "showers", 81: "showers", 82: "heavy showers",
    85: "snow showers", 86: "snow showers", 95: "thunderstorms", 96: "thunderstorms with hail",
    99: "thunderstorms with hail",
}


class Places:
    def __init__(self, location: LocationConfig, maps_key: str | None, http: httpx.AsyncClient | None = None):
        self.loc = location
        self.maps_key = maps_key
        self.http = http or httpx.AsyncClient(timeout=10)

    async def _get(self, url: str, **params: Any) -> dict[str, Any]:
        try:
            resp = await self.http.get(url, params=params)
            resp.raise_for_status()
        except httpx.HTTPError as e:
            raise ToolError("The weather service isn't reachable right now.") from e
        return resp.json()

    async def coords(self, place: str | None) -> tuple[float, float, str]:
        if not place:
            if self.loc.latitude is None or self.loc.longitude is None:
                raise ToolError("I don't know where home is yet; set latitude and longitude in the config.")
            return self.loc.latitude, self.loc.longitude, "home"
        found = (await self._get(GEOCODE, name=place, count=1)).get("results") or []
        if not found:
            raise ToolError(f"I couldn't find {place}.")
        return found[0]["latitude"], found[0]["longitude"], found[0]["name"]

    async def weather(self, place: str | None, days: int) -> str:
        lat, lon, name = await self.coords(place)
        data = await self._get(
            FORECAST, latitude=lat, longitude=lon, timezone="auto", forecast_days=max(1, min(days, 7)),
            current="temperature_2m,apparent_temperature,weather_code,wind_speed_10m",
            daily="weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
        )
        cur, daily = data["current"], data["daily"]
        units = data.get("current_units", {}).get("temperature_2m", "°C")
        lines = [
            f"{name} now: {_WMO.get(cur['weather_code'], 'unknown')}, {cur['temperature_2m']}{units} "
            f"(feels {cur['apparent_temperature']}{units}), wind {cur['wind_speed_10m']} km/h."
        ]
        for i, day in enumerate(daily["time"]):
            lines.append(
                f"{day}: {_WMO.get(daily['weather_code'][i], 'unknown')}, "
                f"{daily['temperature_2m_min'][i]} to {daily['temperature_2m_max'][i]}{units}, "
                f"{daily['precipitation_probability_max'][i]}% chance of rain."
            )
        return "\n".join(lines)

    async def travel_seconds(self, destination: str, arrive_by: datetime | None, origin: str | None) -> int:
        if not self.maps_key:
            raise ToolError("Travel times need a Google Maps key; it isn't set up yet.")
        origin = origin or self.loc.home_address
        if not origin:
            raise ToolError("I don't know your home address yet; set it in the config.")
        body: dict[str, Any] = {
            "origin": {"address": origin},
            "destination": {"address": destination},
            "travelMode": "DRIVE",
            "routingPreference": "TRAFFIC_AWARE",
        }
        if arrive_by is not None:
            # Traffic is predicted for a departure time; estimate one hour before arrival.
            depart = max(arrive_by - timedelta(hours=1), datetime.now(arrive_by.tzinfo))
            body["departureTime"] = depart.isoformat()
        try:
            resp = await self.http.post(ROUTES, json=body, headers={
                "X-Goog-Api-Key": self.maps_key,
                "X-Goog-FieldMask": "routes.duration,routes.staticDuration",
            })
            resp.raise_for_status()
        except httpx.HTTPError as e:
            raise ToolError("Google Maps isn't reachable right now.") from e
        routes = resp.json().get("routes") or []
        if not routes:
            raise ToolError(f"I couldn't find a route to {destination}.")
        return int(routes[0]["duration"].rstrip("s"))


def build_tools(places: Places) -> list[Tool]:
    async def weather(args: dict[str, Any]) -> str:
        return await places.weather(args.get("place"), args.get("days") or 1)

    async def leave_by(args: dict[str, Any]) -> str:
        arrive = datetime.fromisoformat(args["arrive_by"])
        seconds = await places.travel_seconds(args["destination"], arrive, args.get("origin"))
        buffer = args.get("buffer_minutes", 10)
        leave = arrive - timedelta(seconds=seconds, minutes=buffer)
        return (
            f"Drive takes about {round(seconds / 60)} minutes with traffic. "
            f"Leave by {leave.isoformat(timespec='minutes')} to arrive {buffer} minutes early."
        )

    return [
        Tool("get_weather", "Weather now and the daily forecast. `place` defaults to home; `days` 1-7.",
             schema({"place": {"type": "string"}, "days": {"type": "integer"}}),
             Tier.READ, weather, service="Open-Meteo"),
        Tool("leave_by_time", "Traffic-aware driving time and the time to leave to arrive by `arrive_by` "
             "(ISO 8601 with offset). Origin defaults to home.",
             schema({"destination": {"type": "string"}, "arrive_by": {"type": "string"},
                     "origin": {"type": "string"}, "buffer_minutes": {"type": "integer"}},
                    ["destination", "arrive_by"]),
             Tier.READ, leave_by, cue="Checking traffic.", service="Google Maps"),
    ]
