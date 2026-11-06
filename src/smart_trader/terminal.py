"""Phone-friendly terminal dashboard. Quitting it never stops protective monitoring."""
import argparse
import asyncio
import json
import math
import os
from datetime import datetime, timezone

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Label,
    Sparkline,
    Static,
    TabbedContent,
    TabPane,
)

from .analytics import database_report
from .config import Mode, Settings
from .db import Database
from .health import health_state, network_checks
from .selection import StrategySelector
from .setup import setup_wizard

NAMES = {"momentum": "Momentum breakout", "trend_pullback": "Trend pullback", "new_listings": "New listings"}


def number(value, suffix="", digits=2):
    try:
        numeric = float(value)
        return f"{numeric:,.{digits}f}{suffix}" if math.isfinite(numeric) else "—"
    except (TypeError, ValueError):
        return "—"


def snapshot(db, settings):
    mode = db.get("mode", settings.mode.value)
    verified = mode != "DEMO" or db.get("demo_balance_initialized", False)
    report = database_report(db)
    equity = float(db.get("equity", db.get("cash"))) if verified else None
    initial = float(db.get("initial")) if verified else None
    return {"mode": mode, "verified": verified, "equity": equity, "initial": initial,
            "cash": db.get("cash") if verified else None,
            "net_pnl": equity-initial if verified else None,
            "day_pnl": equity-float(db.get("day_anchor", {}).get("equity", initial)) if verified else None,
            "week_pnl": equity-float(db.get("week_anchor", {}).get("equity", initial)) if verified else None,
            "account": db.get("demo_account", {}), "report": report, "positions": db.positions(),
            "health": health_state(db), "kill": db.get("kill", False), "reason": db.get("kill_reason", ""),
            "candidates": db.get("candidates", [])[:10]+db.get("listing_candidates", [])[:5],
            "selection": StrategySelector(db, settings.selection, mode).evidence()}


def summary_panel(data, compact=False):
    table = Table.grid(expand=True, padding=(0, 1))
    table.add_column(style="dim")
    table.add_column(justify="right")
    if not compact:
        table.add_column(style="dim")
        table.add_column(justify="right")
    items = [("Binance USDT", number(data["account"].get("free_usdt"))), ("Капитал бота", number(data["equity"])),
             ("PnL сегодня", number(data["day_pnl"])), ("PnL всего", number(data["net_pnl"])),
             ("PnL недели", number(data["week_pnl"])),
             ("Просадка max", number(data["report"]["max_drawdown"]*100, "%") if data["verified"] else "—")]
    if compact:
        for item in items:
            table.add_row(*item)
    else:
        for index in range(0, len(items), 2):
            table.add_row(*items[index], *items[index+1])
    badge = "[bold green]DEMO · Binance[/bold green]" if data["mode"] == "DEMO" else "[bold yellow]PAPER · локальная модель[/bold yellow]"
    status = "[red]ВХОДЫ ОСТАНОВЛЕНЫ[/red]" if data["kill"] else "[cyan]Лимиты риска активны[/cyan]"
    return Panel(table, title=f"{badge}   {status}", border_style="cyan", subtitle="USDT · только виртуальные средства")


def print_status(db, settings, console=None):
    console = console or Console()
    data = snapshot(db, settings)
    console.print(summary_panel(data, compact=console.width < 70))
    console.print(data["health"]["message"], style="green" if data["health"]["ready"] else "yellow", markup=False)
    if not data["verified"]:
        console.print("Demo-баланс ещё не подтверждён. Задайте ключи: trade config", style="yellow")
    if data["kill"]:
        console.print(data["reason"], style="red", markup=False)
    console.print(f"Открытых позиций: {len(data['positions'])}; закрытых сделок: {data['report']['trades']}. "
                  "Подробно: trade ui · диагностика: trade doctor")


async def doctor(db, console=None):
    console = console or Console()
    console.print(Panel.fit("Диагностика Binance Smart Trader", border_style="cyan"))
    state = health_state(db)
    console.print(state)
    table = Table("Проверка", "HTTP", "Результат", box=box.ROUNDED)
    for result in await network_checks():
        table.add_row(result["name"], str(result["http"] or "—"), Text(result["reason"], style="green" if result["ok"] else "yellow"))
    console.print(table)
    console.print("Откройте http://127.0.0.1:8501 в обычном браузере этого телефона.\n"
                  "Веб-панель включается командой trade web; торговля и терминал от сайта не зависят.\n"
                  "При устаревшем heartbeat разрешите Termux фоновую работу и отключите оптимизацию батареи.")


class ConfirmFlatten(ModalScreen[bool]):
    def compose(self) -> ComposeResult:
        with VerticalScroll(id="confirm-box"):
            yield Label("Остановить новые входы и закрыть все виртуальные позиции?")
            yield Button("Закрыть позиции", variant="error", id="yes")
            yield Button("Отмена", id="no")

    def on_button_pressed(self, event):
        self.dismiss(event.button.id == "yes")


class TraderApp(App):
    TITLE = "BINANCE SMART TRADER"
    SUB_TITLE = "DEMO · Terminal v0.2"
    CSS = """
    Screen { background: #0b1220; color: #d9e6f2; }
    Header { background: #102132; color: #44e6c3; }
    Footer { background: #102132; }
    #account { height: auto; min-height: 7; max-height: 12; margin: 0 1; }
    #health { height: auto; max-height: 4; padding: 0 2; color: #ffcc66; }
    TabbedContent { height: 1fr; margin: 0 1; }
    TabPane { padding: 0 1; }
    DataTable { height: 1fr; background: #0e1928; }
    Sparkline { color: #44e6c3; height: 4; margin: 1 0; }
    #overview-text { height: auto; }
    #confirm-box { width: 60; max-width: 95%; height: 12; border: round #44e6c3; background: #102132; padding: 1 2; }
    ConfirmFlatten { align: center middle; }
    """
    BINDINGS = [("q", "quit", "Выйти"), ("p", "pause", "Пауза"), ("r", "resume", "Продолжить"),
                ("f", "flatten", "Закрыть"), ("c", "config", "Настройки"), ("w", "web", "Веб"),
                ("d", "doctor", "Диагностика"), ("1", "tab('overview')", "Обзор"),
                ("2", "tab('positions')", "Позиции"), ("3", "tab('trades')", "Сделки"),
                ("4", "tab('market')", "Рынок"), ("5", "tab('strategies')", "Стратегии")]

    def __init__(self, settings=None):
        super().__init__()
        self.settings = settings or Settings.load()
        self.db = Database(self.settings.db_path, self.settings.risk.initial_balance)

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static(id="account")
        yield Static(id="health", markup=False)
        with TabbedContent(initial="overview", id="tabs"):
            with TabPane("Обзор", id="overview"):
                yield Sparkline([], id="equity-line")
                yield Static(id="overview-text", markup=False)
                yield DataTable(id="rejections", zebra_stripes=True)
            with TabPane("Позиции", id="positions"):
                yield DataTable(id="positions-table", zebra_stripes=True)
            with TabPane("Сделки", id="trades"):
                yield DataTable(id="trades-table", zebra_stripes=True)
            with TabPane("Рынок", id="market"):
                yield DataTable(id="market-table", zebra_stripes=True)
            with TabPane("Стратегии", id="strategies"):
                yield DataTable(id="strategy-table", zebra_stripes=True)
            with TabPane("Журнал", id="events"):
                yield DataTable(id="events-table", zebra_stripes=True)
        yield Footer()

    def on_mount(self):
        columns = {"rejections": ("Монета", "Причина / решение"),
                   "positions-table": ("Монета", "Стратегия", "Объём", "Вход", "Сейчас", "Стоп", "Цель", "Venue"),
                   "trades-table": ("UTC", "Монета", "Стратегия", "PnL USDT", "R", "Причина", "Venue"),
                   "market-table": ("Монета", "Рейтинг", "Стратегия", "Причина"),
                   "strategy-table": ("Стратегия", "Сделок", "PnL", "Win %", "PF", "R сред.", "Выбор / статус"),
                   "events-table": ("UTC", "Тип", "Событие")}
        for selector, names in columns.items():
            self.query_one("#"+selector, DataTable).add_columns(*names)
        self.refresh_data()
        self.set_interval(3, self.refresh_data)

    def rows(self, selector, rows):
        table = self.query_one("#"+selector, DataTable)
        old_cursor = table.cursor_coordinate
        table.clear()
        for row in rows:
            table.add_row(*(cell if isinstance(cell, Text) else Text(str(cell)) for cell in row))
        if table.row_count:
            table.move_cursor(row=min(old_cursor.row, table.row_count-1), column=old_cursor.column, animate=False)

    def refresh_data(self):
        data = snapshot(self.db, self.settings)
        self.query_one("#account", Static).update(summary_panel(data, compact=self.size.width < 70))
        note = "" if data["verified"] else "Баланс Demo ещё не подтверждён; trade config. "
        self.query_one("#health", Static).update(note+data["health"]["message"]+(" · "+data["reason"] if data["kill"] else ""))
        points = self.db.rows("SELECT equity FROM equity ORDER BY ts DESC LIMIT 100")
        self.query_one("#equity-line", Sparkline).data = [p["equity"] for p in reversed(points)] if data["verified"] else []
        report = data["report"]
        self.query_one("#overview-text", Static).update(
            f"Закрытых сделок: {report['trades']} · Win {number(report['win_rate']*100, '%') if report['win_rate'] is not None else '—'} · "
            f"PF {number(report['profit_factor'])} · Комиссии {number(report['fees'])} USDT\n"
            "Стратегии: breakout / trend pullback / new listings. При недостатке данных: исходные правила.\n"
            "Q закрывает только интерфейс — монитор продолжает работать. F — запрос закрытия позиций.")
        self.rows("rejections", [(r["symbol"], r["reason"]) for r in self.db.rows("SELECT symbol,reason FROM decisions ORDER BY id DESC LIMIT 25")])
        self.rows("positions-table", [(p["symbol"], NAMES.get(p["strategy"], p["strategy"]), p["qty"], number(p["entry"], digits=6),
                                       number(p.get("mark"), digits=6), number(p["stop"], digits=6), number(p["target"], digits=6), p["venue"])
                                      for p in data["positions"]])
        trades = self.db.rows("SELECT * FROM trades ORDER BY closed DESC LIMIT 150")
        self.rows("trades-table", [(datetime.fromtimestamp(r["closed"], timezone.utc).strftime("%m-%d %H:%M"), r["symbol"],
                                    NAMES.get(r["strategy"], r["strategy"]), number(json.loads(r["data"])["pnl"]),
                                    number(json.loads(r["data"])["r_multiple"]), json.loads(r["data"]).get("exit_reason", ""), r["venue"]) for r in trades])
        self.rows("market-table", [(p["symbol"], number(p["score"]*100, "%"), NAMES.get(p["strategy"], p["strategy"]), p["reason"]) for p in data["candidates"]])
        self.rows("strategy-table", [(NAMES[name], stats["trades"], number(stats["net_pnl"]) if data["verified"] else "—",
                                      number(stats["win_rate"]*100, "%") if stats["win_rate"] is not None else "—",
                                      number(stats["profit_factor"]), number(stats["expectancy_r"]), data["selection"][name]["state"])
                                     for name, stats in report["by_strategy"].items()])
        self.rows("events-table", [(datetime.fromtimestamp(r["ts"], timezone.utc).strftime("%H:%M:%S"), r["kind"], r["message"])
                                  for r in self.db.rows("SELECT * FROM events ORDER BY id DESC LIMIT 100")])

    def on_unmount(self):
        self.db.close()

    def action_tab(self, name):
        self.query_one("#tabs", TabbedContent).active = name

    def action_pause(self):
        from .service import control
        control(self.db, "pause")
        self.refresh_data()
        self.notify("Новые входы остановлены. Монитор продолжает работать.")

    def action_resume(self):
        from .service import control
        try:
            control(self.db, "resume")
            self.notify("Запрошена проверка перед возобновлением")
        except ValueError as exc:
            self.notify(str(exc), severity="warning")

    def action_flatten(self):
        def confirmed(value):
            if value:
                from .service import control
                control(self.db, "flatten")
                self.notify("Закрытие запрошено. Ждите подтверждения в позициях.")
        self.push_screen(ConfirmFlatten(), confirmed)

    def action_web(self):
        self.db.set("web_enabled", True)
        self.notify("Панель: http://127.0.0.1:8501 · запуск в течение нескольких секунд")

    async def action_config(self):
        with self.suspend():
            await asyncio.to_thread(setup_wizard)
        fresh = Settings.load()
        if fresh.db_path != self.settings.db_path:
            self.db.close()
            self.db = Database(fresh.db_path, fresh.risk.initial_balance)
        self.settings = fresh
        self.refresh_data()

    async def action_doctor(self):
        with self.suspend():
            await doctor(self.db)
            await asyncio.to_thread(input, "Enter — вернуться в панель ")


def main():
    parser = argparse.ArgumentParser(description="Binance Smart Trader terminal")
    parser.add_argument("action", choices=["ui", "config", "status", "doctor", "ensure-config"], default="ui", nargs="?")
    args = parser.parse_args()
    from .service import project_directory
    os.chdir(project_directory())
    if args.action == "config":
        setup_wizard()
        return
    settings = Settings.load()
    if args.action == "ensure-config":
        if settings.mode != Mode.DEMO or not (settings.demo_key and settings.demo_secret):
            setup_wizard()
            settings = Settings.load()
            if settings.mode != Mode.DEMO or not settings.demo_key or not settings.demo_secret:
                raise SystemExit("Сначала настройте Binance DEMO через trade config")
        return
    if args.action == "ui":
        TraderApp(settings).run()
        return
    db = Database(settings.db_path, settings.risk.initial_balance)
    try:
        if args.action == "status":
            print_status(db, settings)
        else:
            asyncio.run(doctor(db))
    finally:
        db.close()


if __name__ == "__main__":
    main()
