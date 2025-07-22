import asyncio
from Check_30kaUser_bot import telegram_app, Config
import os

WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET")

async def main():
    webhook_url = f"{Config.WEBHOOK_URL}/webhook/{WEBHOOK_SECRET}" if WEBHOOK_SECRET else f"{Config.WEBHOOK_URL}/"
    await telegram_app.bot.set_webhook(webhook_url)
    print(f"Webhook set to {webhook_url}")

if __name__ == "__main__":
    asyncio.run(main()) 