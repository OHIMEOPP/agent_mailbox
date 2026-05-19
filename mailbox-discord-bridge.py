"""Backward-compat shim. Real code lives in the `bridge/` package since
2026-05-19. Run with `python -m bridge`, or keep using this file:

    py mailbox-discord-bridge.py [--port N] [--db PATH] [--bind ADDR] [--no-gateway]

Same CLI as before; just delegates to bridge.__main__.main().
"""
import sys

from bridge.__main__ import main


if __name__ == '__main__':
    sys.exit(main())
