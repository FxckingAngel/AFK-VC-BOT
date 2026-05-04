# AFK VC Bot

Sits in a Discord voice channel 24/7 to keep your VC timer counting. Never leaves. Auto-reconnects on disconnect.

## Setup

1. **Create a Discord bot** at https://discord.com/developers/applications
   - Enable `bot` scope + `Voice States` intent
   - Add to your server with `Connect` and `Speak` permissions

2. **Configure env**
   ```bash
   cp .env.example .env
   # Fill in TOKEN and CHANNEL_ID
   ```

3. **Install deps**
   ```bash
   pip install -r requirements.txt
   ```

4. **Run**
   ```bash
   python bot.py
   ```

## Commands

| Command | Description |
|---------|-------------|
| `/status` | Check if the bot is active in the VC |
| `/uptime` | How long the bot process has been running |
| `/vc` | Which voice channel the bot is in |
| `/help` | List all commands |

## Running 24/7

Use `screen`, `tmux`, or a systemd service to keep it alive:

```bash
# screen
screen -S afk-vc-bot
python bot.py
# Ctrl+A D to detach

# or systemd — create /etc/systemd/system/afk-vc-bot.service
```

The bot has an internal watchdog that checks every 30s and reconnects automatically if it ever drops from the channel.
