"""Phone-friendly terminal dashboard. Quitting it never stops protective monitoring."""
import argparse
import asyncio
import json
import math
import os
import time
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
from .lab import account_status, comparison, open_accounts
from .selection import StrategySelector
from .setup import enable_paper, setup_wizard, switch_to_paper

NAMES = {"momentum": "Momentum breakout", "trend_pullback": "Trend pullback", "new_listings": "New listings"}


def number(value, suffix="", digits=2):
    try:
        numeric = float(value)
        if numeric == 0:
            numeric = 0.0
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
    cash_label = "Binance USDT" if data["mode"] == "DEMO" else "Свободно USDT"
    cash_value = data["account"].get("free_usdt") if data["mode"] == "DEMO" else data["cash"]
    items = [(cash_label, number(cash_value)), ("Капитал бота", number(data["equity"])),
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


def comparison_table(rows):
    table = Table(title="PAPER LAB · 3 независимых виртуальных счёта", box=box.ROUNDED)
    for label in ("Счёт", "Капитал", "PnL", "%", "Сделки", "Откр.", "Max DD %", "Издержки", "Статус"):
        table.add_column(label)
    for row in rows:
        state = account_status(row)
        table.add_row(row["name"], number(row["equity"]), number(row["net_pnl"]), number(row["return_pct"]),
                      str(row["report"]["trades"]), str(row["positions"]),
                      number(row["report"]["max_drawdown"]*100), number(row["fees_total"]+row["slippage_total"]), state)
    return table


def print_comparison(settings, console=None):
    console = console or Console()
    accounts = open_accounts(settings)
    try:
        rows = comparison(accounts)
        if console.width < 110:
            for row in rows:
                table = Table.grid(expand=True)
                table.add_column()
                table.add_column(justify="right")
                for label, value in (("Капитал", number(row["equity"])),
                                      ("PnL USDT / %", f"{number(row['net_pnl'])} / {number(row['return_pct'])}%"),
                                      ("Закрытых / открытых", f"{row['report']['trades']} / {row['positions']}"),
                                      ("Max DD", number(row["report"]["max_drawdown"]*100, "%")),
                                      ("Издержки USDT", number(row["fees_total"]+row["slippage_total"])),
                                      ("Статус", account_status(row))):
                    table.add_row(label, value)
                console.print(Panel(table, title=f"{row['name']} · PAPER", border_style="cyan"))
        else:
            console.print(comparison_table(rows))
        for cfg, db in accounts:
            state = health_state(db)
            console.print(f"{cfg.account_label}: {state['message']}", markup=False)
            if db.get("kill"):
                console.print(db.get("kill_reason", ""), style="red", markup=False)
        console.print("PnL включает открытые позиции по последней оценке. Издержки: комиссии + проскальзывание.\n"
                      "Каждый счёт имеет собственный депозит; деньги между счетами не переводятся.\n"
                      "Одна ночь — наблюдение, данных для вывода о прибыльности недостаточно. Подробно: trade ui")
    finally:
        for _, db in accounts:
            db.close()


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
    SUB_TITLE = "Virtual only · Terminal v0.3.1"
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
    #comparison-note { height: auto; }
    #confirm-box { width: 60; max-width: 95%; height: 12; border: round #44e6c3; background: #102132; padding: 1 2; }
    ConfirmFlatten { align: center middle; }
    """
    BINDINGS = [("q", "quit", "Выйти"), ("p", "pause", "Пауза"), ("r", "resume", "Продолжить"),
                ("f", "flatten", "Закрыть"), ("c", "config", "Настройки"), ("w", "web", "Веб"),
                ("d", "doctor", "Диагностика"), ("1", "tab('overview')", "Обзор"),
                ("2", "tab('positions')", "Позиции"), ("3", "tab('trades')", "Сделки"),
                ("4", "tab('market')", "Рынок"), ("5", "tab('strategies')", "Стратегии")]
    BINDINGS += [("0", "tab('accounts')", "Счета"), ("b", "account('base')", "База"),
                 ("a", "account('aggressive')", "Риск"), ("t", "account('active')", "Частый")]

    def __init__(self, settings=None, *, watch_config=False):
        super().__init__()
        self.watch_config = watch_config
        self.profile_changing = False
        self.settings = settings or Settings.load()
        self.coordinator = Database(self.settings.db_path, self.settings.risk.initial_balance)
        self.accounts = open_accounts(self.settings) if self.settings.paper_lab else []
        self.db = self.accounts[0][1] if self.accounts else self.coordinator
        self.account_settings = self.accounts[0][0] if self.accounts else self.settings

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static(id="account")
        yield Static(id="health", markup=False)
        with TabbedContent(initial="accounts" if self.accounts else "overview", id="tabs"):
            if self.accounts:
                with TabPane("Счета", id="accounts"):
                    yield Static("B — базовый · A — рискованный · T — активный.\n"
                                 "Enter по строке — детали. P/R/F управляют ВСЕМИ счетами.\n"
                                 "Только виртуальные деньги. Одна ночь не доказывает прибыльность.",
                                 id="comparison-note", markup=False)
                    yield DataTable(id="accounts-table", zebra_stripes=True, cursor_type="row")
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
        self.initialize_tables()
        self.refresh_data()
        self.set_interval(3, self.refresh_data)

    def initialize_tables(self):
        columns = {"rejections": ("Монета", "Причина / решение"),
                   "positions-table": ("Монета", "Стратегия", "Объём", "Вход", "Сейчас", "Стоп", "Цель", "Venue"),
                   "trades-table": ("UTC", "Монета", "Стратегия", "PnL USDT", "R", "Причина", "Venue"),
                   "market-table": ("Монета", "Рейтинг", "Стратегия", "Причина"),
                   "strategy-table": ("Стратегия", "Сделок", "PnL", "Win %", "PF", "R сред.", "Выбор / статус"),
                   "events-table": ("UTC", "Тип", "Событие")}
        for selector, names in columns.items():
            self.query_one("#"+selector, DataTable).add_columns(*names)
        if self.accounts:
            self.query_one("#accounts-table", DataTable).add_columns(
                "Счёт", "Капитал", "PnL", "%", "Закр.", "Откр.", "Max DD %", "Издержки", "Статус", "Риск %", "TF")

    def rows(self, selector, rows):
        table = self.query_one("#"+selector, DataTable)
        old_cursor = table.cursor_coordinate
        table.clear()
        for row in rows:
            table.add_row(*(cell if isinstance(cell, Text) else Text(str(cell)) for cell in row))
        if table.row_count:
            table.move_cursor(row=min(old_cursor.row, table.row_count-1), column=old_cursor.column, animate=False)

    def refresh_data(self):
        if self.profile_changing:
            return
        if self.watch_config:
            fresh = Settings.load()
            if self.profile_key(fresh) != self.profile_key(self.settings):
                self.profile_changing = True
                self.run_worker(self.reload_profile(fresh), group="profile", exclusive=True)
                return
        data = snapshot(self.db, self.account_settings)
        self.sub_title = f"{data['mode']} · Terminal v0.3.1"
        if self.accounts:
            self.sub_title = f"PAPER v0.3.1 · {self.account_settings.account_label} · B / A / T"
            self.rows("accounts-table", [(r["name"], number(r["equity"]), number(r["net_pnl"]), number(r["return_pct"]),
                                          r["report"]["trades"], r["positions"], number(r["report"]["max_drawdown"]*100),
                                          number(r["fees_total"]+r["slippage_total"]),
                                          account_status(r),
                                          r["risk_pct"], r["interval"]) for r in comparison(self.accounts)])
        self.query_one("#account", Static).update(summary_panel(data, compact=self.size.width < 70))
        note = "" if data["verified"] else "Баланс Demo ещё не подтверждён; trade config. "
        if self.db.get("scan_data_paused", False) or self.db.get("monitor_data_paused", False):
            note += "Нет актуальных данных: новые входы приостановлены. "
        if self.db.get("pause_until", 0) > time.time():
            note += "Пауза риска до "+datetime.fromtimestamp(self.db.get("pause_until"), timezone.utc).strftime("%H:%M UTC")+". "
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
        for _, db in self.accounts:
            db.close()
        self.coordinator.close()

    @staticmethod
    def profile_key(settings):
        return settings.mode, settings.db_path, settings.paper_lab

    async def reload_profile(self, fresh):
        self.profile_changing = True
        try:
            coordinator = Database(fresh.db_path, fresh.risk.initial_balance)
            try:
                accounts = open_accounts(fresh) if fresh.paper_lab else []
            except BaseException:
                coordinator.close()
                raise
            for _, db in self.accounts:
                db.close()
            self.coordinator.close()
            self.settings, self.coordinator, self.accounts = fresh, coordinator, accounts
            self.db = accounts[0][1] if accounts else coordinator
            self.account_settings = accounts[0][0] if accounts else fresh
            await self.recompose()
            self.initialize_tables()
        finally:
            self.profile_changing = False
        self.refresh_data()
        self.notify("Профиль переключён: "+("PAPER · 3 счёта" if accounts else fresh.mode.value))

    def action_tab(self, name):
        if name == "accounts" and not self.accounts:
            return
        self.query_one("#tabs", TabbedContent).active = name

    def action_account(self, name):
        for cfg, db in self.accounts:
            if cfg.account_id == name:
                self.account_settings, self.db = cfg, db
                self.action_tab("overview")
                self.refresh_data()
                break

    def on_data_table_row_selected(self, event):
        if event.data_table.id == "accounts-table":
            self.action_account(self.accounts[event.cursor_row][0].account_id)

    def control(self, action):
        from .service import control, control_all
        return control_all([self.coordinator, *(db for _, db in self.accounts)], action) if self.accounts else control(self.db, action)

    def action_pause(self):
        self.control("pause")
        self.refresh_data()
        self.notify("Новые входы остановлены. Монитор продолжает работать.")

    def action_resume(self):
        try:
            self.control("resume")
            self.notify("Запрошена проверка перед возобновлением")
        except ValueError as exc:
            self.notify(str(exc), severity="warning")

    def action_flatten(self):
        def confirmed(value):
            if value:
                self.control("flatten")
                self.notify("Закрытие запрошено. Ждите подтверждения в позициях.")
        self.push_screen(ConfirmFlatten(), confirmed)

    def action_web(self):
        self.coordinator.set("web_enabled", True)
        self.notify("Панель: http://127.0.0.1:8501 · запуск в течение нескольких секунд")

    async def action_config(self):
        with self.suspend():
            await asyncio.to_thread(setup_wizard)
        fresh = Settings.load()
        if self.profile_key(fresh) != self.profile_key(self.settings):
            await self.reload_profile(fresh)
            return
        self.refresh_data()

    async def action_doctor(self):
        with self.suspend():
            await doctor(self.db)
            await asyncio.to_thread(input, "Enter — вернуться в панель ")


def main():
    parser = argparse.ArgumentParser(description="Binance Smart Trader terminal")
    parser.add_argument("action", choices=["ui", "config", "paper", "status", "report", "doctor", "ensure-config"], default="ui", nargs="?")
    args = parser.parse_args()
    from .service import project_directory
    os.chdir(project_directory())
    if args.action == "config":
        setup_wizard()
        return
    settings = Settings.load()
    if args.action == "paper":
        try:
            fresh = switch_to_paper(settings)
            print("PAPER включён: Базовый / Рискованный / Активный. Demo-аккаунт отключён, API-ключи не нужны.")
            print("Настройки сохранены: "+fresh.db_path)
        except (ValueError, OSError) as exc:
            raise SystemExit(str(exc)) from None
        return
    if args.action == "ensure-config":
        if settings.mode == Mode.DEMO and (not settings.binance_enabled or not (settings.demo_key and settings.demo_secret)):
            enable_paper(settings)
            print("Demo-ключи не заданы: включены три локальных PAPER-счёта по 4900 USDT. Binance-аккаунт OFF.")
        return
    if args.action == "ui":
        TraderApp(settings, watch_config=True).run()
        return
    if args.action in {"status", "report"} and settings.paper_lab:
        print_comparison(settings)
        return
    db = Database(settings.db_path, settings.risk.initial_balance)
    try:
        if args.action in {"status", "report"}:
            print_status(db, settings)
        else:
            asyncio.run(doctor(db))
    finally:
        db.close()


if __name__ == "__main__":
    main()
