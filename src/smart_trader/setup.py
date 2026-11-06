"""Interactive setup. Secrets stay on this device and are never echoed or logged."""
import asyncio
import json
import os
import tempfile
import time
from dataclasses import asdict
from pathlib import Path

import httpx
from dotenv import dotenv_values
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table

from .account import account_snapshot, identity
from .api import APIError, BinanceAPI
from .config import RiskConfig, Settings, dec
from .db import Database
from .health import service_process_alive


def write_env(path, updates):
    path = Path(path)
    previous = dotenv_values(path, interpolate=False) if path.exists() else {}
    values = {**previous, **updates}
    for key, value in values.items():
        if not key.replace("_", "").isalnum() or any(c in str(value) for c in "\r\n\x00"):
            raise ValueError("Недопустимое имя или многострочное значение настройки")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".env-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as out:
            os.chmod(temporary, 0o600)
            out.write("# Managed locally by trade config. Never commit this file.\n")
            for key, value in values.items():
                out.write(key+"="+json.dumps(str(value or ""), ensure_ascii=False)+"\n")
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


async def verify_demo(key, secret, *, factory=BinanceAPI):
    api = factory(demo=True, key=key, secret=secret)
    try:
        await api.sync_time()
        return account_snapshot(await api.signed("GET", "account"))
    finally:
        await api.close()


async def verify_telegram(token, chat, *, send=False, transport=None):
    try:
        async with httpx.AsyncClient(timeout=12, follow_redirects=False, transport=transport) as client:
            me = await client.get(f"https://api.telegram.org/bot{token}/getMe")
            raw = me.json()
            if me.status_code != 200 or not raw.get("ok"):
                raise ValueError("Telegram отклонил токен")
            if send:
                response = await client.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
                    "chat_id": chat, "text": "Binance Smart Trader: уведомления подключены. Только Binance DEMO, виртуальные средства."})
                if response.status_code != 200 or not response.json().get("ok"):
                    raise ValueError("Telegram: не удалось отправить сообщение. Проверьте chat ID и напишите боту /start")
            return raw["result"].get("username", "bot")
    except (httpx.HTTPError, ValueError):
        # HTTP exception repr contains the token in its URL: never propagate it.
        raise ValueError("Ошибка Telegram. Проверьте token, chat ID, /start и сеть; секреты скрыты") from None


def credential_change_allowed(settings):
    path = Path(settings.db_path)
    if not path.exists():
        return
    db = Database(path)
    try:
        stopped_flat = db.get("service_stop", False) and db.get("kill", False)
        active = (service_process_alive(db.get("service_pid")) or
                  (not stopped_flat and (time.time()-db.get("service_heartbeat", 0) < 20
                                         or time.time()-db.get("heartbeat", 0) < 30)))
        unresolved = db.positions() or db.rows("SELECT id FROM intents WHERE status NOT IN ('CLOSED','REJECTED')")
        if active or unresolved:
            raise ValueError("Сначала trade stop и дождитесь закрытия позиций. Ключи/профиль работающего бота не заменяются")
    finally:
        db.close()


def secret_prompt(console, label, existing):
    suffix = " (Enter — оставить сохранённый)" if existing else ""
    entered = Prompt.ask(label+suffix, password=True, console=console)
    value = entered.strip() or existing
    if any(c in value for c in "\n\r\x00"):
        raise ValueError("Секрет должен быть одной строкой")
    return value


def configure_demo(console, settings):
    credential_change_allowed(settings)
    console.print(Panel("Ключи создаются в [bold]demo.binance.com[/bold] → API Management.\n"
                        "Нужны HMAC key и secret для Spot Demo. Production-торговли нет.", title="BINANCE SPOT DEMO", border_style="cyan"))
    key = secret_prompt(console, "Demo API key", settings.demo_key)
    secret = secret_prompt(console, "Demo API secret", settings.demo_secret)
    if not key or not secret:
        raise ValueError("Нужны оба Demo-ключа. Изменения не сохранены")
    snapshot = None
    if Confirm.ask("Проверить Demo API и прочитать баланс сейчас?", default=True, console=console):
        try:
            with console.status("Проверка официального Binance Demo API..."):
                snapshot = asyncio.run(asyncio.wait_for(verify_demo(key, secret), timeout=30))
            console.print(f"[green]Demo подтверждён.[/green] Свободно {snapshot['free_usdt']} USDT; заблокировано {snapshot['locked_usdt']} USDT")
        except (APIError, ValueError, TimeoutError) as exc:
            # APIError is sanitized by BinanceAPI.
            console.print(f"[yellow]Проверка не завершена: {exc}[/yellow]", markup=False)
            if not Confirm.ask("Сохранить ключи для поздней проверки? Бот не начнёт торговлю без подтверждения API", default=False, console=console):
                return False
    profile = f"data/demo-{identity(key)}.sqlite"
    write_env(".env", {"TRADER_MODE": "DEMO", "TRADER_DB": profile, "TRADER_STRICT_DEMO": "true",
                       "TRADER_DEMO_SYNC_BALANCE": "true", "TRADER_WEB_ENABLED": "false",
                       "TRADER_STRATEGY_AUTO": "true", "BINANCE_DEMO_API_KEY": key, "BINANCE_DEMO_API_SECRET": secret})
    console.print("[green]Сохранено локально с правами 600.[/green] PAPER-история сохранена отдельно.\n"
                  "Стартовый капитал будет получен из API при первом запуске; баланс не выдумывается.")
    return True


def configure_telegram(console, settings):
    token = secret_prompt(console, "Telegram bot token", settings.telegram_token)
    chat = Prompt.ask("Telegram chat ID (сначала напишите своему боту /start)",
                      default=settings.telegram_chat or None, console=console).strip()
    if not token or not chat:
        raise ValueError("Нужны token и chat ID")
    if Confirm.ask("Проверить бота и отправить тестовое уведомление?", default=True, console=console):
        name = asyncio.run(verify_telegram(token, chat, send=True))
        console.print(f"[green]Уведомления подключены: @{name}[/green]")
    write_env(".env", {"TELEGRAM_BOT_TOKEN": token, "TELEGRAM_CHAT_ID": chat})
    console.print("[green]Telegram сохранён. Работающий бот подхватит изменение в течение 10 секунд.[/green]")


def configure_risk(console, settings):
    db = Database(settings.db_path, settings.risk.initial_balance)
    try:
        current = db.get("risk_overrides", asdict(settings.risk))
        for key, label, low, high in (("trade_risk", "Риск основной стратегии, %", .01, .5),
                                      ("listing_risk", "Риск нового листинга, %", .01, .2),
                                      ("max_exposure", "Суммарная стоимость позиций, %", 1, 25)):
            raw = Prompt.ask(label, default=str(current[key]*100), console=console)
            value = dec(raw.replace(",", "."))
            if not dec(low) <= value <= dec(high):
                raise ValueError(f"{label}: допустимо {low}–{high}")
            current[key] = float(value/100)
        RiskConfig(**current).validate()
        db.set("risk_overrides", current)
        console.print("[green]Лимиты сохранены. Они не обходят стопы, корреляцию и дневную/недельную остановку.[/green]")
    finally:
        db.close()


def setup_wizard(*, console=None):
    console = console or Console()
    console.print(Panel.fit("[bold cyan]BINANCE SMART TRADER[/bold cyan]\nНастройка Demo и уведомлений · v0.2", border_style="cyan"))
    while True:
        settings = Settings.load()
        menu = Table(show_header=False, box=None, padding=(0, 2))
        for key, label in (("1", "Binance Demo: ключи, проверка баланса"), ("2", "Telegram: token, chat ID, тест"),
                           ("3", "Стратегии: автоматический выбор / базовая"), ("4", "Лимиты риска"),
                           ("5", "Статус настройки"), ("0", "Сохранить и выйти")):
            menu.add_row(f"[cyan]{key}[/cyan]", label)
        console.print(menu)
        try:
            choice = Prompt.ask("Выбор", choices=["1", "2", "3", "4", "5", "0"], default="1" if not settings.demo_key or settings.mode.value != "DEMO" else "0", console=console)
        except (EOFError, KeyboardInterrupt):
            console.print("\nНастройка закрыта.")
            return
        try:
            if choice == "0":
                return
            if choice == "1":
                configure_demo(console, settings)
            elif choice == "2":
                configure_telegram(console, settings)
            elif choice == "3":
                enabled = Confirm.ask("Автоматический выбор momentum / trend pullback / new listings по подтверждённой статистике?", default=True, console=console)
                write_env(".env", {"TRADER_STRATEGY_AUTO": "true" if enabled else "false"})
                console.print("Стратегии сохранены; применятся после остановки и запуска. Лимиты риска не увеличиваются.")
            elif choice == "4":
                configure_risk(console, settings)
            else:
                console.print({"mode": settings.mode.value, "strict_demo": settings.strict_demo,
                               "demo_key": "сохранён" if settings.demo_key else "не задан",
                               "demo_secret": "сохранён" if settings.demo_secret else "не задан",
                               "telegram": "настроен" if settings.telegram_token and settings.telegram_chat else "не задан",
                               "database": settings.db_path, "auto_selection": settings.selection.enabled})
        except (ValueError, APIError) as exc:
            console.print(str(exc), style="red", markup=False)
        except (EOFError, KeyboardInterrupt):
            console.print("\nНастройка прервана; уже сохранённые разделы остаются в .env.")
            return
