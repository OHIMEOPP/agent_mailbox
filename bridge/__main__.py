"""Entry point — `python -m bridge`."""
import argparse
import os
import sys

from . import config  # noqa: F401 — side-effect: rewrap stdout/stderr as UTF-8
from .config import DEFAULT_DB, DEFAULT_PORT
from .gateway import start_gateway
from .http_server import serve


def main():
    p = argparse.ArgumentParser(prog="bridge",
                                description="Discord <-> mailbox bridge")
    p.add_argument('--port', type=int, default=DEFAULT_PORT,
                   help=f"HTTP server port (default {DEFAULT_PORT})")
    p.add_argument('--db', default=DEFAULT_DB,
                   help=f"mailbox SQLite path (default {DEFAULT_DB})")
    p.add_argument('--bind', default='0.0.0.0',
                   help="bind addr; 0.0.0.0 lets Docker reach via "
                        "host.docker.internal (127.0.0.1 only would block container)")
    p.add_argument('--no-gateway', action='store_true',
                   help="disable discord.py gateway client even if token + lib present")
    args = p.parse_args()

    if not os.path.exists(args.db):
        sys.stderr.write(f"[bridge] FATAL: mailbox db not found: {args.db}\n")
        return 1

    if not args.no_gateway:
        start_gateway(args.db)

    serve(args.db, args.bind, args.port)
    return 0


if __name__ == '__main__':
    sys.exit(main())
