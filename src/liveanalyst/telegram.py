from __future__ import annotations

import requests


class TelegramSender:
    def __init__(self, bot_token: str, chat_id: str):
        self.url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        self.chat_id = chat_id

    def send(self, message: str) -> None:
        resp = requests.post(
            self.url,
            json={"chat_id": self.chat_id, "text": message},
            timeout=10,
        )
        resp.raise_for_status()
