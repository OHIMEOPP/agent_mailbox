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
import time

from .config import DISCORD_BOT_TOKEN
from .inbound import process_discord_inbound

# Shared state surfaced via /healthz. Updated by the gateway thread; read by
# the HTTP server. Plain dict mutations are atomic enough for this use.
_state = {
    "expected": False,        # True iff start_gateway() decided to spawn the thread
    "online": False,          # True between on_ready and on_disconnect
    "last_ready_at": None,    # epoch seconds of most recent on_ready
    "last_error": None,       # last exception text from bot.run() (or "clean_exit")
    "attempts": 0,            # count of bot.run() attempts since process start
}


def gateway_state():
    """Snapshot of gateway state for /healthz."""
    return dict(_state)


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

    _state["expected"] = True

    def _run():
        import discord
        intents = discord.Intents.default()
        intents.message_content = True
        intents.dm_messages = True

        class Bot(discord.Client):
            async def on_ready(self):
                _state["online"] = True
                _state["last_ready_at"] = time.time()
                _state["last_error"] = None
                sys.stdout.write(f"[gateway] online as {self.user} "
                                 f"(id={self.user.id})\n")

            async def on_disconnect(self):
                _state["online"] = False

            async def on_resumed(self):
                _state["online"] = True
                _state["last_ready_at"] = time.time()

            async def on_message(self, message):
                if message.author.id == self.user.id:
                    return  # ignore self
                if not isinstance(message.channel, discord.DMChannel):
                    return  # DMs only
                # discord.py Attachment -> dict shape consumed by attachments.relay_*
                atts = [
                    {
                        "id": str(a.id),
                        "filename": a.filename,
                        "url": a.url,
                        "proxy_url": a.proxy_url,
                        "content_type": a.content_type,
                        "size": a.size,
                    }
                    for a in (message.attachments or [])
                ]
                status, resp = process_discord_inbound(
                    content=message.content,
                    author=message.author.name,
                    author_id=str(message.author.id),
                    channel=str(message.channel.id),
                    to_name_hint=None,
                    db_path=db_path,
                    attachments=atts,
                )
                if status >= 400:
                    sys.stdout.write(f"[gateway] on_message returned {status}: {resp}\n")

        # Retry forever: startup DNS race, transient network, Discord-side
        # disconnect — all recover without manual `docker restart`.
        backoff = 5
        backoff_max = 60
        while True:
            _state["attempts"] += 1
            _state["online"] = False
            try:
                bot = Bot(intents=intents)
                bot.run(DISCORD_BOT_TOKEN, log_handler=None)
                # bot.run() returned without exception — clean disconnect.
                _state["last_error"] = "clean_exit"
                sys.stdout.write("[gateway] bot.run() exited cleanly; "
                                 "reconnecting in 5s\n")
                time.sleep(5)
                backoff = 5
            except Exception as e:
                err = f"{type(e).__name__}: {e}"
                _state["last_error"] = err
                sys.stdout.write(f"[gateway] crashed: {err}; "
                                 f"retry in {backoff}s\n")
                time.sleep(backoff)
                backoff = min(backoff * 2, backoff_max)

    t = threading.Thread(target=_run, name="discord-gateway", daemon=True)
    t.start()
    return True
