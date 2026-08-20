import asyncio
from telegram import Bot

class TelegramNotifier:
    def __init__(self, token, chat_id):
        self.bot = Bot(token=token)
        self.chat_id = chat_id

    def send_note(self, symbol, timeframe, notes):
        if not notes:
            return
            
        message = f"📌 **[S/R Notu - {symbol} | {timeframe}]**\n\n"
        message += "\n\n".join(notes)
        
        try:
            asyncio.run(self.bot.send_message(chat_id=self.chat_id, text=message, parse_mode='Markdown'))
        except Exception as e:
            print(f"Telegram mesaj gönderme hatası: {e}")
