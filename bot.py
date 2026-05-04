"""
bot.py

AFK VC Bot — stays in a voice channel 24/7 to preserve the Discord VC timer.
Never leaves. Has slash commands for status, uptime, and help.

Assumptions:
  - TOKEN and CHANNEL_ID are set in .env
  - Bot has Connect + Speak permissions in the target channel
  - Reconnect loop retries with exponential backoff on disconnect
"""

import asyncio
import logging
import os
import time
from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────

BOT_TOKEN: str = os.environ["TOKEN"]
VC_CHANNEL_ID: int = int(os.environ["CHANNEL_ID"])

RECONNECT_DELAY_SECONDS: int = 5       # Base delay before reconnect attempt
MAX_RECONNECT_DELAY_SECONDS: int = 60  # Cap on exponential backoff
PRESENCE_CHECK_INTERVAL_SECONDS: int = 30  # How often to verify still in VC

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("afk-vc-bot")

# ── Bot setup ─────────────────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.voice_states = True


class AFKVCBot(discord.Client):
    def __init__(self) -> None:
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.start_time: float = time.time()
        self._reconnect_delay: int = RECONNECT_DELAY_SECONDS

    async def setup_hook(self) -> None:
        await self.tree.sync()
        log.info("Slash commands synced.")

    async def on_ready(self) -> None:
        log.info(f"Logged in as {self.user} (id={self.user.id})")
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.listening,
                name="the VC — 24/7 🎙️",
            )
        )
        self.presence_watchdog.start()

    async def join_vc(self) -> bool:
        """
        Attempt to join the configured VC channel.

        Returns:
            True if successfully joined or already present, False otherwise.
        """
        channel = self.get_channel(VC_CHANNEL_ID)
        if channel is None:
            log.error(f"Channel {VC_CHANNEL_ID} not found — check CHANNEL_ID in .env")
            return False

        if not isinstance(channel, discord.VoiceChannel):
            log.error(f"Channel {VC_CHANNEL_ID} is not a voice channel")
            return False

        # Already connected to this channel
        if self.voice_clients:
            vc = self.voice_clients[0]
            if vc.channel.id == VC_CHANNEL_ID and vc.is_connected():
                return True
            # Connected elsewhere — move
            await vc.move_to(channel)
            log.info(f"Moved to #{channel.name}")
            return True

        try:
            await channel.connect(reconnect=True)
            log.info(f"Joined #{channel.name} in guild '{channel.guild.name}'")
            self._reconnect_delay = RECONNECT_DELAY_SECONDS  # reset backoff on success
            return True
        except discord.ClientException as e:
            log.warning(f"ClientException joining VC: {e}")
            return False
        except Exception as e:
            log.error(f"Unexpected error joining VC: {e}", exc_info=True)
            return False

    @tasks.loop(seconds=PRESENCE_CHECK_INTERVAL_SECONDS)
    async def presence_watchdog(self) -> None:
        """Periodically verify we're still in the VC; reconnect if not."""
        in_vc = bool(self.voice_clients) and self.voice_clients[0].is_connected()
        if not in_vc:
            log.warning("Not in VC — attempting reconnect...")
            success = await self.join_vc()
            if not success:
                # Exponential backoff before next watchdog tick catches it
                await asyncio.sleep(self._reconnect_delay)
                self._reconnect_delay = min(
                    self._reconnect_delay * 2, MAX_RECONNECT_DELAY_SECONDS
                )

    @presence_watchdog.before_loop
    async def before_watchdog(self) -> None:
        await self.wait_until_ready()
        await self.join_vc()

    def uptime_str(self) -> str:
        """Return a human-readable uptime string."""
        delta = timedelta(seconds=int(time.time() - self.start_time))
        hours, remainder = divmod(int(delta.total_seconds()), 3600)
        minutes, seconds = divmod(remainder, 60)
        days, hours = divmod(hours, 24)
        parts = []
        if days:
            parts.append(f"{days}d")
        if hours:
            parts.append(f"{hours}h")
        if minutes:
            parts.append(f"{minutes}m")
        parts.append(f"{seconds}s")
        return " ".join(parts)


# ── Client instance ───────────────────────────────────────────────────────────

bot = AFKVCBot()

# ── Slash commands ────────────────────────────────────────────────────────────


@bot.tree.command(name="status", description="Check if the bot is in the VC.")
async def cmd_status(interaction: discord.Interaction) -> None:
    in_vc = bool(bot.voice_clients) and bot.voice_clients[0].is_connected()
    channel = bot.get_channel(VC_CHANNEL_ID)
    channel_name = channel.name if channel else str(VC_CHANNEL_ID)

    if in_vc:
        embed = discord.Embed(
            title="✅ Active",
            description=f"Sitting in **#{channel_name}** keeping your timer alive.",
            color=discord.Color.green(),
        )
    else:
        embed = discord.Embed(
            title="⚠️ Disconnected",
            description=f"Not currently in **#{channel_name}**. Reconnect loop is running.",
            color=discord.Color.orange(),
        )
    embed.add_field(name="Uptime", value=bot.uptime_str(), inline=True)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="uptime", description="How long the bot has been running.")
async def cmd_uptime(interaction: discord.Interaction) -> None:
    embed = discord.Embed(
        title="⏱️ Uptime",
        description=bot.uptime_str(),
        color=discord.Color.blurple(),
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="vc", description="Show which voice channel the bot is in.")
async def cmd_vc(interaction: discord.Interaction) -> None:
    if bot.voice_clients and bot.voice_clients[0].is_connected():
        vc = bot.voice_clients[0]
        embed = discord.Embed(
            title="🎙️ Voice Channel",
            description=f"**#{vc.channel.name}** in **{vc.channel.guild.name}**",
            color=discord.Color.green(),
        )
        member_count = len(vc.channel.members) - 1  # exclude self
        embed.add_field(
            name="Other members",
            value=str(member_count) if member_count > 0 else "Just me 👻",
            inline=True,
        )
    else:
        embed = discord.Embed(
            title="🎙️ Voice Channel",
            description="Not currently connected.",
            color=discord.Color.red(),
        )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="help", description="List all available commands.")
async def cmd_help(interaction: discord.Interaction) -> None:
    embed = discord.Embed(
        title="AFK VC Bot — Commands",
        description=(
            "This bot sits in a voice channel 24/7 to keep your VC timer counting. "
            "It never leaves, auto-reconnects on disconnect, and requires no interaction."
        ),
        color=discord.Color.blurple(),
    )
    commands_info = [
        ("/status", "Check if the bot is active in the VC"),
        ("/uptime", "How long the bot process has been running"),
        ("/vc",     "Show which voice channel the bot is in"),
        ("/help",   "This message"),
    ]
    for name, desc in commands_info:
        embed.add_field(name=name, value=desc, inline=False)
    embed.set_footer(text="Bot stays in VC permanently. 50 hours and counting 🔥")
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    bot.run(BOT_TOKEN, log_handler=None)  # log_handler=None — we manage logging above
