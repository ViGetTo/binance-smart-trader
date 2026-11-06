import asyncio

import httpx


class Telegram:
    def __init__(self, db, token, chat):
        self.db, self.token, self.chat = db, token, chat

    async def flush(self):
        if not self.token or not self.chat:
            return
        async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
            for row in self.db.rows("SELECT * FROM outbox WHERE sent=0 ORDER BY id LIMIT 10"):
                try:
                    r = await client.post(f"https://api.telegram.org/bot{self.token}/sendMessage",
                                          json={"chat_id": self.chat, "text": row["message"][:4000]})
                    if r.status_code != 200 or not r.json().get("ok"):
                        break
                    self.db.execute("UPDATE outbox SET sent=1 WHERE id=?", (row["id"],))
                except (httpx.HTTPError, ValueError):
                    # Token is embedded in URL: never log exception objects.
                    break
                await asyncio.sleep(.1)
