"""Discord bridge skill — read-only introspection over
core.discord_bridge's authorization store (data/discord_authorized.json).

Deliberately does NOT start the bridge or call generate_reply(): the
Discord gateway connection already runs as its own always-on launchd agent
(scripts/com.jarvislite.discordbridge.plist), independent of this process —
see core/server.py's own comment on why core.server.start() never starts it
either. Doing so here would open a second Gateway connection on the same
bot token and answer every DM twice."""
from skills import LiraSkill
from core import discord_bridge


class DiscordBridgeSkill(LiraSkill):
    name = "discord_bridge"
    flag = "skill_discord"
    description = "Consulta quién está autorizado a hablar con LIRA por Discord."
    triggers = ["quién está autorizado en discord", "autorizados de discord"]

    def execute(self, query: str, context: dict) -> str:
        users = discord_bridge.list_authorized()
        if not users:
            return "Nadie más está autorizado en Discord además de ti."
        names = [u.get("username") or uid for uid, u in users.items()]
        return "Autorizados en Discord: " + ", ".join(names)
