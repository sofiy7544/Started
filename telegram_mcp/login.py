"""Login: creates the Telethon session used by the MCP server.

Interactive (normal terminal):
    python -m telegram_mcp.login

Two-step, for hosts without an interactive terminal (cloud session, phone):
    python -m telegram_mcp.login --phone +79991234567      # Telegram sends a code
    python -m telegram_mcp.login --code 12345 [--password 2FA]
The second step writes a session string to --string-out; put it into TG_SESSION_STRING.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from telethon import TelegramClient, errors, utils
from telethon.sessions import StringSession

from .config import Config, ensure_private_dir, load_config


def _write_private(path: Path, content: str) -> None:
    ensure_private_dir(path.parent)
    path.write_text(content, encoding="utf-8")
    path.chmod(0o600)


async def _interactive(cfg: Config) -> None:
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


async def _request_code(cfg: Config, phone: str, pending: Path) -> None:
    client = TelegramClient(StringSession(), cfg.api_id, cfg.api_hash)
    await client.connect()
    sent = await client.send_code_request(phone)
    state = {"phone": phone, "phone_code_hash": sent.phone_code_hash, "session": client.session.save()}
    await client.disconnect()
    _write_private(pending, json.dumps(state))
    print("Code sent to your Telegram app. Now run: python -m telegram_mcp.login --code <CODE>")


async def _complete(cfg: Config, code: str, password: str | None, pending: Path, out: Path) -> None:
    if not pending.exists():
        sys.exit("No pending login: run with --phone first.")
    state = json.loads(pending.read_text(encoding="utf-8"))
    client = TelegramClient(StringSession(state["session"]), cfg.api_id, cfg.api_hash)
    await client.connect()
    try:
        await client.sign_in(state["phone"], code, phone_code_hash=state["phone_code_hash"])
    except errors.SessionPasswordNeededError:
        if not password:
            await client.disconnect()
            sys.exit("Two-step verification is enabled: repeat with --code <CODE> --password <PASSWORD>.")
        await client.sign_in(password=password)
    me = await client.get_me()
    _write_private(out, client.session.save())
    await client.disconnect()
    pending.unlink()
    print(
        f"OK: logged in as {utils.get_display_name(me)} (id={me.id}).\n"
        f"Session string saved to {out} — copy it into TG_SESSION_STRING, then delete the file.\n"
        "It grants full access to your account: never share or commit it."
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Log in to Telegram for telegram_mcp")
    parser.add_argument("--phone", help="step 1: phone number, e.g. +79991234567")
    parser.add_argument("--code", help="step 2: login code from the Telegram app")
    parser.add_argument(
        "--password",
        default=os.environ.get("TG_2FA_PASSWORD"),
        help="step 2: two-step verification password (or env TG_2FA_PASSWORD)",
    )
    parser.add_argument("--string-out", type=Path, help="where to write the session string")
    args = parser.parse_args()

    cfg = load_config()
    pending = cfg.data_dir / "pending_login.json"
    out = args.string_out or cfg.data_dir / "session_string.txt"
    try:
        if args.phone:
            asyncio.run(_request_code(cfg, args.phone, pending))
        elif args.code:
            asyncio.run(_complete(cfg, args.code, args.password, pending, out))
        else:
            asyncio.run(_interactive(cfg))
    except KeyboardInterrupt:
        sys.exit(1)


if __name__ == "__main__":
    main()
