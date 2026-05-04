"""
bot.py

AFK VC Bot — stays in a voice channel 24/7 to preserve the Discord VC timer.
Also features 30+ roleplay slash commands with anime GIFs and per-user action counters.

Assumptions:
  - TOKEN and CHANNEL_ID are set in .env
  - Bot has Connect + Speak permissions in the target channel
  - Roleplay counters are in-memory (reset on restart)
  - GIFs sourced from nekos.best public API (no key required)
"""

import asyncio
import logging
import os
import time
from collections import defaultdict
from datetime import timedelta
from typing import Optional

import aiohttp
import discord
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────

BOT_TOKEN: str = os.environ["TOKEN"]
VC_CHANNEL_ID: int = int(os.environ["CHANNEL_ID"])

RECONNECT_DELAY_SECONDS: int = 5
MAX_RECONNECT_DELAY_SECONDS: int = 60
PRESENCE_CHECK_INTERVAL_SECONDS: int = 30

NEKOS_BEST_API: str = "https://nekos.best/api/v2"

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("afk-vc-bot")

# ── Roleplay action registry ──────────────────────────────────────────────────
# Maps action name -> (emoji, past-tense label, embed color)

ACTIONS: dict[str, tuple[str, str, int]] = {
    "hug":        ("🤗", "hugged",          0xFF6B9D),
    "kiss":       ("💋", "kissed",          0xFF4F7B),
    "pat":        ("👋", "patted",          0xFFD700),
    "slap":       ("💢", "slapped",         0xFF4500),
    "poke":       ("👉", "poked",           0x7EC8E3),
    "cuddle":     ("🥰", "cuddled",         0xFF85A1),
    "wave":       ("👋", "waved at",        0x00C9A7),
    "bite":       ("😬", "bit",             0xFF6347),
    "blush":      ("😳", "blushed at",      0xFF69B4),
    "boop":       ("👆", "booped",          0x9B59B6),
    "cry":        ("😢", "cried at",        0x5B9BD5),
    "dance":      ("💃", "danced with",     0xE91E63),
    "feed":       ("🍱", "fed",             0x27AE60),
    "handshake":  ("🤝", "shook hands with",0x95A5A6),
    "handhold":   ("🤲", "held hands with", 0xF39C12),
    "highfive":   ("🙏", "high-fived",      0x1ABC9C),
    "kick":       ("🦵", "kicked",          0xE74C3C),
    "laugh":      ("😂", "laughed at",      0xF1C40F),
    "nod":        ("😌", "nodded at",       0x2ECC71),
    "nope":       ("🙅", "noped at",        0xE67E22),
    "nom":        ("😋", "nommed",          0xFF9800),
    "pout":       ("😤", "pouted at",       0x9C27B0),
    "shoot":      ("🔫", "shot",            0x607D8B),
    "shrug":      ("🤷", "shrugged at",     0x795548),
    "smile":      ("😊", "smiled at",       0x4CAF50),
    "smug":       ("😏", "smugged at",      0x673AB7),
    "stare":      ("👀", "stared at",       0x3F51B5),
    "think":      ("🤔", "thought about",   0x009688),
    "thumbsup":   ("👍", "thumbs-upped",    0x8BC34A),
    "wink":       ("😉", "winked at",       0xFF5722),
    "yeet":       ("🚀", "yeeted",          0xF44336),
}

# ── In-memory counter store ───────────────────────────────────────────────────
# counters[actor_id][action][target_id] = count (target_id=0 means no target)

counters: dict[int, dict[str, dict[int, int]]] = defaultdict(
    lambda: defaultdict(lambda: defaultdict(int))
)

# ── Bot setup ─────────────────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.voice_states = True



class AFKVCBot(discord.Client):
    def __init__(self) -> None:
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.start_time: float = time.time()
        self._reconnect_delay: int = RECONNECT_DELAY_SECONDS
        self._http_session: Optional[aiohttp.ClientSession] = None

    async def setup_hook(self) -> None:
        self._http_session = aiohttp.ClientSession()
        await self.tree.sync()
        log.info("Slash commands synced.")

    async def close(self) -> None:
        if self._http_session:
            await self._http_session.close()
        await super().close()

    async def on_ready(self) -> None:
        log.info(f"Logged in as {self.user} (id={self.user.id})")
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.listening,
                name="the VC — 24/7 🎙️",
            )
        )
        self.presence_watchdog.start()



    async def fetch_gif(self, action: str) -> Optional[str]:
        """
        Fetch an anime GIF URL from nekos.best for the given action.

        Returns:
            GIF URL string, or None if the request fails.
        """
        if self._http_session is None:
            return None
        try:
            async with self._http_session.get(
                f"{NEKOS_BEST_API}/{action}",
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()
                return data["results"][0]["url"]
        except Exception as e:
            log.warning(f"Failed to fetch GIF for '{action}': {e}")
            return None

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

        if self.voice_clients:
            vc = self.voice_clients[0]
            if vc.channel.id == VC_CHANNEL_ID and vc.is_connected():
                return True
            await vc.move_to(channel)
            log.info(f"Moved to #{channel.name}")
            return True

        try:
            await channel.connect(reconnect=True)
            log.info(f"Joined #{channel.name} in guild '{channel.guild.name}'")
            self._reconnect_delay = RECONNECT_DELAY_SECONDS
            return True
        except discord.ClientException as e:
            log.warning(f"ClientException joining VC: {e}")
            return False
        except Exception as e:
            log.error(f"Unexpected error joining VC: {e}", exc_info=True)
            return False

    @tasks.loop(seconds=PRESENCE_CHECK_INTERVAL_SECONDS)
    async def presence_watchdog(self) -> None:
        """Periodically verify we're still in the VC; reconnect with backoff if not."""
        in_vc = bool(self.voice_clients) and self.voice_clients[0].is_connected()
        if not in_vc:
            log.warning("Not in VC — attempting reconnect...")
            success = await self.join_vc()
            if not success:
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

# ── Helper: build and send roleplay embed ─────────────────────────────────────


async def send_rp_action(
    interaction: discord.Interaction,
    action: str,
    target: Optional[discord.Member],
) -> None:
    """
    Build and send a roleplay embed with GIF, counter, and action text.

    Args:
        interaction: The slash command interaction.
        action:      The action key (must exist in ACTIONS).
        target:      Optional member being targeted.
    """
    emoji, past_tense, color = ACTIONS[action]
    actor = interaction.user

    if target:
        counters[actor.id][action][target.id] += 1
        count = counters[actor.id][action][target.id]
        description = (
            f"**{actor.display_name}** {past_tense} **{target.display_name}** {emoji}\n"
            f"*That's {count} time{'s' if count != 1 else ''}!*"
        )
    else:
        counters[actor.id][action][0] += 1
        count = counters[actor.id][action][0]
        description = (
            f"**{actor.display_name}** {past_tense} the air {emoji}\n"
            f"*{count} time{'s' if count != 1 else ''} now...*"
        )

    await interaction.response.defer()

    gif_url = await bot.fetch_gif(action)

    embed = discord.Embed(description=description, color=color)
    embed.set_author(
        name=f"{actor.display_name} › {action}",
        icon_url=actor.display_avatar.url,
    )
    if gif_url:
        embed.set_image(url=gif_url)
    embed.set_footer(text=f"Use /{action} to do it again!")

    await interaction.followup.send(embed=embed)


# ── VC / utility commands ─────────────────────────────────────────────────────


@bot.tree.command(name="status", description="Check if the bot is in the VC.")
async def cmd_status(interaction: discord.Interaction) -> None:
    in_vc = bool(bot.voice_clients) and bot.voice_clients[0].is_connected()
    channel = bot.get_channel(VC_CHANNEL_ID)
    channel_name = channel.name if channel else str(VC_CHANNEL_ID)
    embed = discord.Embed(
        title="✅ Active" if in_vc else "⚠️ Disconnected",
        description=(
            f"Sitting in **#{channel_name}** keeping your timer alive."
            if in_vc else
            f"Not currently in **#{channel_name}**. Reconnect loop is running."
        ),
        color=discord.Color.green() if in_vc else discord.Color.orange(),
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
        member_count = len(vc.channel.members) - 1
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


@bot.tree.command(name="mystats", description="See how many times you've done each roleplay action.")
async def cmd_mystats(interaction: discord.Interaction) -> None:
    user_counters = counters.get(interaction.user.id, {})
    if not user_counters:
        await interaction.response.send_message(
            "You haven't used any roleplay commands yet!", ephemeral=True
        )
        return

    embed = discord.Embed(
        title=f"📊 {interaction.user.display_name}'s Roleplay Stats",
        color=discord.Color.blurple(),
    )
    for action, targets in sorted(user_counters.items()):
        total = sum(targets.values())
        e = ACTIONS[action][0]
        embed.add_field(
            name=f"{e} /{action}",
            value=f"{total}x",
            inline=True,
        )
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="help", description="List all available commands.")
async def cmd_help(interaction: discord.Interaction) -> None:
    embed = discord.Embed(
        title="AFK VC Bot — Commands",
        description=(
            "Sits in VC 24/7 keeping your timer alive. "
            "Also has 31 roleplay commands with anime GIFs and counters!"
        ),
        color=discord.Color.blurple(),
    )
    embed.add_field(
        name="📡 VC Commands",
        value="`/status` `/uptime` `/vc` `/mystats` `/help`",
        inline=False,
    )
    rp_list = " ".join(f"`/{a}`" for a in sorted(ACTIONS.keys()))
    embed.add_field(name="🎭 Roleplay Commands", value=rp_list, inline=False)
    embed.set_footer(
        text="All roleplay commands accept an optional @target. "
             "/mystats shows your action counts."
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ── Roleplay commands (auto-generated from ACTIONS registry) ──────────────────


def make_rp_command(action: str, emoji: str, past_tense: str) -> app_commands.Command:
    """
    Factory that creates a slash command for the given roleplay action.

    Returns:
        A configured app_commands.Command ready to register on the tree.
    """
    @app_commands.command(
        name=action,
        description=f"{emoji} {past_tense.capitalize()} someone (or the air).",
    )
    @app_commands.describe(target="Who to target (leave empty to do it to the air)")
    async def rp_command(
        interaction: discord.Interaction,
        target: Optional[discord.Member] = None,
    ) -> None:
        await send_rp_action(interaction, action, target)

    return rp_command


for _action, (_emoji, _past_tense, _color) in ACTIONS.items():
    bot.tree.add_command(make_rp_command(_action, _emoji, _past_tense))


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    bot.run(BOT_TOKEN, log_handler=None)
