import asyncio
import os
import time
from pathlib import Path

import httpx

from .config import DEMO_URL, PUBLIC_URL


def service_process_alive(pid):
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        proc = Path(f"/proc/{pid}/cmdline")
        if proc.exists():
            return b"smart_trader.service" in proc.read_bytes()
        return True
    except (OSError, ValueError):
        return False


def health_state(db, now=None):
    now = now or time.time()
    service_age = now-db.get("service_heartbeat", 0)
    worker = db.get("heartbeat", 0)
    worker_age = now-worker if worker else None
    alive = service_process_alive(db.get("service_pid"))
    ready = alive and service_age < 30 and worker_age is not None and worker_age < 30
    return {"service_process_alive": alive, "service_fresh": service_age < 30,
            "trader_fresh": worker_age is not None and worker_age < 30,
            "ready": ready, "service_age": round(service_age, 1) if db.get("service_heartbeat") else None,
            "trader_age": round(worker_age, 1) if worker_age is not None else None,
            "message": "Соединение и heartbeat активны" if ready else
                       "PID не работает: сервис завершён. Запустите trade" if not alive else
                       "Heartbeat устарел: возможна приостановка Android или зависание. trade doctor"}


async def network_checks(*, transport=None):
    async def check(label, url):
        try:
            async with httpx.AsyncClient(timeout=8, follow_redirects=False, transport=transport) as client:
                r = await client.get(url)
                return {"name": label, "ok": r.status_code == 200, "http": r.status_code,
                        "reason": "Binance ограничивает текущий регион (451)" if r.status_code == 451 else
                                  "доступен" if r.status_code == 200 else "недоступен"}
        except httpx.HTTPError:
            return {"name": label, "ok": False, "http": None, "reason": "нет ответа / процесс не запущен"}
    return await asyncio.gather(check("Public Binance", PUBLIC_URL+"/v3/time"),
                                check("Binance Demo", DEMO_URL+"/v3/time"),
                                check("Веб-панель", "http://127.0.0.1:8501/_stcore/health"))
