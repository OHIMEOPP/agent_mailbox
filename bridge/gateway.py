"""discord.py gateway client — runs in a daemon thread.

Receives DMs directly via websocket. Calls process_discord_inbound() identical
to the HTTP /from-discord webhook path, so both inbound paths produce the same
mailbox state. The gateway is the singleton (Discord one-connection-per-token
limit), so node-red MUST be disconnected if this is enabled — verify via
docker logs after enabling.

Pre-req: Discord Developer Portal Bot page -> Privileged Gateway Intents ->
MESSAGE CONTENT INTENT enabled.
"""
import sys
import threading

from .config import DISCORD_BOT_TOKEN
from .inbound import process_discord_inbound


def start_gateway(db_path):
    """Spawn the gateway client in a daemon thread. Returns True if started,
    False if disabled (no library, no token, etc).
    """
    try:
        import discord  # noqa: F401
    except ImportError:
        sys.stdout.write("[gateway] discord.py not installed; gateway inbound disabled\n")
        return False
    if not DISCORD_BOT_TOKEN:
        sys.stdout.write("[gateway] DISCORD_BOT_TOKEN unset; gateway inbound disabled\n")
        return False

    def _run():
        import discord
        intents = discord.Intents.default()
        intents.message_content = True
        intents.dm_messages = True

        class Bot(discord.Client):
            async def on_ready(self):
                sys.stdout.write(f"[gateway] online as {self.user} "
                                 f"(id={self.user.id})\n")

            async def on_message(self, message):
                if message.author.id == self.user.id:
                    return  # ignore self
                if not isinstance(message.channel, discord.DMChannel):
                    return  # DMs only
                status, resp = process_discord_inbound(
                    content=message.content,
                    author=message.author.name,
                    author_id=str(message.author.id),
                    channel=str(message.channel.id),
                    to_name_hint=None,
                    db_path=db_path,
                )
                if status >= 400:
                    sys.stdout.write(f"[gateway] on_message returned {status}: {resp}\n")

        try:
            bot = Bot(intents=intents)
            bot.run(DISCORD_BOT_TOKEN, log_handler=None)
        except Exception as e:
            sys.stdout.write(f"[gateway] crashed: {type(e).__name__}: {e}\n")

    t = threading.Thread(target=_run, name="discord-gateway", daemon=True)
    t.start()
    return True
