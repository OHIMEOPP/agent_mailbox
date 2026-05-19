"""Bridge config — env vars, defaults, regex. No I/O at import time."""
import io
import os
import re
import sys

# Force UTF-8 on stdout/stderr (Windows console default is cp950).
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace', line_buffering=True)
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace', line_buffering=True)

# === Defaults ===============================================================
DEFAULT_DB = r'C:\Users\User\.claude\mailbox\mailbox.db'
DEFAULT_PORT = 1904

# === Inter-service URL ======================================================
# Bridge POSTs notifications (offline alerts, stranger-pending, command results)
# back to user via this endpoint. Default = node-red host port; override to
# the service hostname when running on the same docker network:
#   NOTIFY_URL=http://discordBot:1880/agent-notify
NOTIFY_URL = os.environ.get('NOTIFY_URL', 'http://localhost:1901/agent-notify')

# === Offline detection ======================================================
# If no agent heartbeat / mark_read within this window, treat target as offline
# and route a notification to Discord so user knows mail is sitting unread.
OFFLINE_THRESHOLD_SECONDS = 300  # 5 min

# === Trust model ============================================================
# Trusted root user — DMs from this Discord username are routed as user input
# (default to wiki, @prefix overrides). Strangers go through whitelist gate.
TRUSTED_USER = os.environ.get('TRUSTED_DISCORD_USER', 'ohimeopp').lower()

# Whitelist + pending DMs DB (separate from messages.db; smaller bind-mount
# surface for stranger-side tooling).
WHITELIST_DB = os.environ.get('WHITELIST_DB', '/data/whitelist.db')

# === Discord outbound (REST API, no gateway dependency) =====================
# Set DISCORD_BOT_TOKEN to enable bridge's own /agent-notify endpoint AND the
# gateway inbound listener. Same token shared with node-red is fine because
# Discord's single-connection limit applies to the GATEWAY websocket only;
# REST API is stateless and tolerates multiple concurrent clients.
DISCORD_BOT_TOKEN = os.environ.get('DISCORD_BOT_TOKEN', '')
DISCORD_API_BASE = os.environ.get('DISCORD_API_BASE', 'https://discord.com/api/v10')
DISCORD_DEFAULT_CHANNEL = os.environ.get('DISCORD_DEFAULT_CHANNEL', '1284065900659740773')

# status -> icon mapping (identical to node-red /agent-notify flow for parity).
NOTIFY_ICON = {'done': '✅', 'fail': '❌', 'warn': '⚠️', 'info': '📋'}

# === Regex used across inbound + whitelist ==================================
ALLOW_DENY_RE = re.compile(r'^(allow|deny)\s+(\S+)\s*$', re.IGNORECASE)
