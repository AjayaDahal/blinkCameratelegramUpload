#!/usr/bin/env python3
"""
Interactive setup for Blink→Telegram daemon.
Run this first to configure credentials and test the connection.
"""

import asyncio
import json
import os
import sys

from blinkpy.blinkpy import Blink
from blinkpy.auth import Auth

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")


def load_config():
    with open(CONFIG_PATH) as f:
        return json.load(f)


def save_config(cfg):
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=4)
    print(f"Config saved to {CONFIG_PATH}")


async def setup_blink(cfg):
    """Authenticate with Blink and save credentials."""
    print("\n=== Blink Camera Setup ===\n")

    if not cfg.get("blink_username"):
        cfg["blink_username"] = input("Blink email: ").strip()
    if not cfg.get("blink_password"):
        cfg["blink_password"] = input("Blink password: ").strip()

    save_config(cfg)

    blink = Blink()
    auth = Auth(
        {"username": cfg["blink_username"], "password": cfg["blink_password"]},
        no_prompt=True,
    )
    blink.auth = auth

    print("Logging in...")
    try:
        await blink.start()
    except Exception as e:
        if "2FA" in str(type(e).__name__) or "2fa" in str(e).lower():
            print("\n2FA required! Check your email for the PIN.")
            pin = input("Enter 2FA PIN: ").strip()
            await auth.send_auth_key(blink, pin)
            await blink.setup_post_verify()
        else:
            raise

    creds_file = cfg["credentials_file"]
    await blink.save(creds_file)
    print(f"\nCredentials saved to {creds_file}")

    print(f"\nFound {len(blink.cameras)} camera(s):")
    for name, cam in blink.cameras.items():
        print(f"  - {name} (armed: {cam.arm})")

    print(f"\nSync modules:")
    for name, sync in blink.sync.items():
        has_ls = hasattr(sync, "local_storage") and sync.local_storage
        print(f"  - {name} (local storage: {'YES' if has_ls else 'NO'})")

    if blink.session:
        await blink.session.close()


async def setup_telegram(cfg):
    """Test Telegram bot connection."""
    import telegram

    print("\n=== Telegram Bot Setup ===\n")

    if not cfg.get("telegram_bot_token"):
        print("1. Open Telegram, search for @BotFather")
        print("2. Send /newbot and follow the prompts")
        print("3. Copy the bot token")
        cfg["telegram_bot_token"] = input("\nBot token: ").strip()

    if not cfg.get("telegram_chat_id"):
        print("\nTo get your chat/channel ID:")
        print("1. Create a channel or group in Telegram")
        print("2. Add your bot as admin")
        print("3. Send a message in the channel")
        print("4. Visit: https://api.telegram.org/bot<TOKEN>/getUpdates")
        print("   (replace <TOKEN> with your bot token)")
        print("5. Find 'chat':{'id': <NUMBER>} in the response")
        print("   For channels, the ID starts with -100")
        cfg["telegram_chat_id"] = input("\nChat/Channel ID: ").strip()

    save_config(cfg)

    print("\nTesting Telegram connection...")
    bot = telegram.Bot(token=cfg["telegram_bot_token"])
    try:
        me = await bot.get_me()
        print(f"Bot connected: @{me.username}")

        await bot.send_message(
            chat_id=cfg["telegram_chat_id"],
            text="✅ Blink→Telegram bot connected successfully!",
        )
        print("Test message sent to channel!")
    except Exception as e:
        print(f"Error: {e}")
        print("Check your bot token and chat ID")


async def main():
    cfg = load_config()

    print("Blink Camera → Telegram Backup Setup")
    print("=" * 40)
    print()
    print("1. Setup Blink credentials")
    print("2. Setup Telegram bot")
    print("3. Setup both")
    print("4. Test existing config")
    print()
    choice = input("Choose [3]: ").strip() or "3"

    if choice in ("1", "3"):
        await setup_blink(cfg)
        cfg = load_config()  # reload

    if choice in ("2", "3"):
        await setup_telegram(cfg)

    if choice == "4":
        await setup_blink(cfg)
        cfg = load_config()
        await setup_telegram(cfg)

    print("\n✅ Setup complete!")
    print(f"\nTo start the daemon:")
    print(f'  cd "{os.path.dirname(__file__)}"')
    print(f"  source venv/bin/activate")
    print(f"  python blink2telegram.py")
    print(f"\nTo run as a background service, use the systemd unit file:")
    print(f"  sudo cp blink2telegram.service /etc/systemd/system/")
    print(f"  sudo systemctl enable --now blink2telegram")


if __name__ == "__main__":
    asyncio.run(main())
