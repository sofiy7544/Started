"""One-time interactive login: creates the Telethon session file used by the MCP server.

Run in a normal terminal (not through Claude):  python -m telegram_mcp.login
"""

from __future__ import annotations

import asyncio
import sys

from telethon import TelegramClient, utils

from .config import ensure_private_dir, load_config


async def _login() -> None:
    cfg = load_config()
    ensure_private_dir(cfg.session_path.parent)

    client = TelegramClient(str(cfg.session_path), cfg.api_id, cfg.api_hash)
    # start() asks for the phone number, the login code and (if enabled) the 2FA password.
    await client.start()
    me = await client.get_me()
    await client.disconnect()

    session_file = cfg.session_path.with_suffix(".session")
    session_file.chmod(0o600)
    print(
        f"\nOK: logged in as {utils.get_display_name(me)} (id={me.id}, @{me.username or '-'})\n"
        f"Session saved to {session_file}\n"
        "Keep this file secret: it grants full access to your account."
    )


def main() -> None:
    try:
        asyncio.run(_login())
    except KeyboardInterrupt:
        sys.exit(1)


if __name__ == "__main__":
    main()
