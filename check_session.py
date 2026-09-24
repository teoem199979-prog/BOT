import asyncio
from telethon import TelegramClient
from config import Config

async def main():
    c = TelegramClient(Config.SESSION_NAME, Config.API_ID, Config.API_HASH)
    await c.connect()
    auth = await c.is_user_authorized()
    print("AUTHORIZED:", auth)
    if auth:
        me = await c.get_me()
        print("USER:", me.username, "| ID:", me.id)
    await c.disconnect()

asyncio.run(main())