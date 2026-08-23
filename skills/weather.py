"""Weather skill — thin HugoSkill wrapper over core.tools.get_location /
get_weather_string (Open-Meteo; see core/tools_environment.py). No weather
fetching logic lives here."""
from skills import HugoSkill
from core import tools


class WeatherSkill(HugoSkill):
    name = "weather"
    description = "Clima actual para la ubicación detectada."
    triggers = ["qué tiempo hace", "clima", "va a llover", "temperatura"]

    def execute(self, query: str, context: dict) -> str:
        loc = tools.get_location()
        if not loc.get("lat") or not loc.get("lon"):
            return "No pude determinar la ubicación para consultar el clima."
        return tools.get_weather_string(loc["lat"], loc["lon"])
