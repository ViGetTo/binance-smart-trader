"""Interactive setup. Secrets stay on this device and are never echoed or logged."""
import asyncio
import json
import os
import tempfile
import time
from dataclasses import asdict, replace
from pathlib import Path

import httpx
from dotenv import dotenv_values
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table

from .account import account_snapshot, identity
from .api import APIError, BinanceAPI
from .config import Mode, RiskConfig, Settings, dec
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
                    "chat_id": chat, "text": "Smart Trader: уведомления подключены. Только виртуальные PAPER/DEMO средства."})
                if response.status_code != 200 or not response.json().get("ok"):
                    raise ValueError("Telegram: не удалось отправить сообщение. Проверьте chat ID и напишите боту /start")
            return raw["result"].get("username", "bot")
    except (httpx.HTTPError, ValueError):
        # HTTP exception repr contains the token in its URL: never propagate it.
        raise ValueError("Ошибка Telegram. Проверьте token, chat ID, /start и сеть; секреты скрыты") from None


def credential_change_allowed(settings):
    if settings.paper_lab:
        from .lab import account_settings
        for profile in account_settings(settings):
            credential_change_allowed(profile)
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
                       "TRADER_PROFILE_SOURCE": "local",
                       "BINANCE_ENABLED": "true", "BINANCE_MARKET_DATA_ENABLED": "true", "TRADER_PAPER_LAB": "false",
                       "TRADER_DEMO_SYNC_BALANCE": "true", "TRADER_WEB_ENABLED": "false",
                       "TRADER_STRATEGY_AUTO": "true", "BINANCE_DEMO_API_KEY": key, "BINANCE_DEMO_API_SECRET": secret})
    console.print("[green]Сохранено локально с правами 600.[/green] PAPER-история сохранена отдельно.\n"
                  "Стартовый капитал будет получен из API при первом запуске; баланс не выдумывается.")
    return True


def enable_paper(settings, balance=4900):
    credential_change_allowed(settings)
    if not dec(balance) > 0:
        raise ValueError("Виртуальный стартовый баланс должен быть положительным")
    path = settings.db_path if settings.mode == Mode.PAPER and settings.paper_lab else "data/paper-lab.sqlite"
    if path != settings.db_path:
        credential_change_allowed(replace(settings, mode=Mode.PAPER, paper_lab=True, db_path=path))
    write_env(".env", {"TRADER_MODE": "PAPER", "TRADER_DB": path, "TRADER_PAPER_LAB": "true",
                       "TRADER_PROFILE_SOURCE": "local",
                       "BINANCE_ENABLED": "false", "BINANCE_MARKET_DATA_ENABLED": "true",
                       "PAPER_INITIAL_BALANCE": str(balance)})


def switch_to_paper(settings, *, wait_seconds=8):
    """Explicit keyless recovery: stop a flat old profile, retain all ledgers and risk halts."""
    from .lab import open_accounts
    from .service import unresolved
    root = Database(settings.db_path, settings.risk.initial_balance)
    accounts = open_accounts(settings) if settings.paper_lab else []
    ledgers = [root, *(db for _, db in accounts)]
    try:
        for db in ledgers:
            if not db.get("kill", False):
                db.halt("Switch to PAPER: pause entries")
        if any(unresolved(db) for db in ledgers):
            raise ValueError("Есть открытые позиции/заявки. Выполните trade stop и дождитесь закрытия; история сохранена")
        for db in ledgers:
            db.set("service_stop", True)
        deadline = time.monotonic()+wait_seconds
        while any(service_process_alive(db.get("service_pid")) for db in ledgers):
            if time.monotonic() >= deadline:
                raise ValueError("Старый сервис ещё завершается. Повторите trade paper через несколько секунд")
            time.sleep(.2)
    finally:
        for db in ledgers:
            db.close()
    enable_paper(settings)
    fresh = Settings.load()
    accounts = open_accounts(fresh)
    try:
        for _, db in accounts:
            # Request the normal validated resume only for an ordinary stop/migration.
            # A Risk Manager/ledger error or manual pause must remain stopped.
            reason = db.get("kill_reason", "")
            ordinary_stop = reason in {"Termux user command: stop", "Switch to PAPER: pause entries"}
            if db.get("kill", False) and ordinary_stop:
                db.set("resume_requested", True)
    finally:
        for _, db in accounts:
            db.close()
    return fresh


def configure_connection(console, settings):
    console.print("1 — Свой PAPER: 3 счёта, без ключей и верификации\n"
                  "2 — Подключить Binance Demo (необязательно)\n"
                  "3 — Публичные котировки Binance: включить / выключить полностью")
    choice = Prompt.ask("Подключение", choices=["1", "2", "3"], default="1", console=console)
    if choice == "1":
        balance = Prompt.ask("Стартовый виртуальный баланс КАЖДОГО нового счёта, USDT",
                             default="4900", console=console)
        enable_paper(settings, dec(balance.replace(",", ".")))
        console.print("[green]Binance-аккаунт OFF. Три PAPER-счёта включены.[/green]\n"
                      "Используются публичные реальные котировки. Ключи не нужны и не отправляются.\n"
                      "Существующие счета сохраняют баланс и историю; депозит повторно не начисляется.")
    elif choice == "2":
        configure_demo(console, settings)
    else:
        credential_change_allowed(settings)
        enabled = Confirm.ask("Разрешить публичные котировки Binance?", default=settings.market_data_enabled, console=console)
        write_env(".env", {"BINANCE_MARKET_DATA_ENABLED": "true" if enabled else "false"})
        console.print("Котировки ON" if enabled else "Binance-котировки OFF: запросов и новых PAPER-исполнений не будет")


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
    if settings.paper_lab:
        from .lab import account_settings
        profiles = {cfg.account_id: cfg for cfg in account_settings(settings)}
        name = Prompt.ask("Счёт", choices=list(profiles), default="base", console=console)
        settings = profiles[name]
    db = Database(settings.db_path, settings.risk.initial_balance)
    try:
        current = db.get("risk_overrides", asdict(settings.risk))
        for key, label, low, high in (("trade_risk", "Риск основной стратегии, %", .01, settings.risk.trade_risk*100),
                                      ("listing_risk", "Риск нового листинга, %", .01, .2),
                                      ("max_exposure", "Суммарная стоимость позиций, %", 1, settings.risk.max_exposure*100)):
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
    console.print(Panel.fit("[bold cyan]SMART TRADER · PAPER LAB[/bold cyan]\nТри виртуальных счёта · настройки v0.3", border_style="cyan"))
    while True:
        settings = Settings.load()
        menu = Table(show_header=False, box=None, padding=(0, 2))
        for key, label in (("1", "Binance: отключить аккаунт / PAPER / Demo / котировки"), ("2", "Telegram: token, chat ID, тест"),
                           ("3", "Стратегии: автоматический выбор / базовая"), ("4", "Лимиты риска"),
                           ("5", "Статус настройки"), ("6", "Автообновление с GitHub"), ("0", "Сохранить и выйти")):
            menu.add_row(f"[cyan]{key}[/cyan]", label)
        console.print(menu)
        try:
            choice = Prompt.ask("Выбор", choices=["1", "2", "3", "4", "5", "6", "0"], default="0", console=console)
        except (EOFError, KeyboardInterrupt):
            console.print("\nНастройка закрыта.")
            return
        try:
            if choice == "0":
                return
            if choice == "1":
                configure_connection(console, settings)
            elif choice == "2":
                configure_telegram(console, settings)
            elif choice == "3":
                enabled = Confirm.ask("Автоматический выбор momentum / trend pullback / new listings по подтверждённой статистике?", default=True, console=console)
                write_env(".env", {"TRADER_STRATEGY_AUTO": "true" if enabled else "false"})
                console.print("Стратегии сохранены; применятся после остановки и запуска. Лимиты риска не увеличиваются.")
            elif choice == "4":
                configure_risk(console, settings)
            elif choice == "6":
                enabled = Confirm.ask("Проверять и устанавливать обновления при запуске trade?", default=settings.auto_update, console=console)
                write_env(".env", {"TRADER_AUTO_UPDATE": "true" if enabled else "false"})
                console.print("Сохранено. Во время работы счетов установка откладывается; .env и БД сохраняются.")
            else:
                console.print({"mode": settings.mode.value, "paper_lab": settings.paper_lab,
                               "binance_account": settings.binance_enabled, "public_quotes": settings.market_data_enabled,
                               "auto_update": settings.auto_update, "strict_demo": settings.strict_demo,
                               "demo_key": "сохранён" if settings.demo_key else "не задан",
                               "demo_secret": "сохранён" if settings.demo_secret else "не задан",
                               "telegram": "настроен" if settings.telegram_token and settings.telegram_chat else "не задан",
                               "database": settings.db_path, "auto_selection": settings.selection.enabled})
        except (ValueError, APIError) as exc:
            console.print(str(exc), style="red", markup=False)
        except (EOFError, KeyboardInterrupt):
            console.print("\nНастройка прервана; уже сохранённые разделы остаются в .env.")
            return
