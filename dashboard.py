import json
import time
from dataclasses import asdict
from datetime import datetime, timezone

import pandas as pd
import streamlit as st

from smart_trader.analytics import database_report
from smart_trader.config import RiskConfig, Settings
from smart_trader.db import Database

st.set_page_config(page_title="Binance Smart Trader — virtual only", layout="wide")
settings = Settings.load()


@st.fragment(run_every="5s")
def panel():
    db = Database(settings.db_path, settings.risk.initial_balance)
    try:
        st.title("Binance Smart Trader")
        mode = db.get("mode", "PAPER")
        st.caption(f"Только виртуальные средства · {mode} · время UTC")
        heartbeat = db.get("heartbeat", 0)
        stale = time.time()-heartbeat > 30
        if stale:
            st.error("Нет свежего heartbeat исполнителя. PAPER-защита требует работающего процесса бота.")
        if db.get("kill", False):
            st.warning("Новые входы остановлены: " + db.get("kill_reason", "Kill switch"))
        report = database_report(db)
        initial = float(db.get("initial"))
        equity = float(db.get("equity", db.get("cash")))
        cash = float(db.get("cash"))
        columns = st.columns(5)
        for col, (label, value) in zip(columns, [("Капитал", equity), ("Свободный бюджет", cash),
                   ("Открытые активы", equity-cash), ("PnL сегодня", equity-float(db.get("day_anchor", {}).get("equity", initial))),
                   ("PnL недели", equity-float(db.get("week_anchor", {}).get("equity", initial)))], strict=True):
            col.metric(label, f"{value:,.2f} USDT")
        st.write(f"PnL всего: {equity-initial:,.2f} USDT · Max DD: {report['max_drawdown']:.2%}")
        a, b, c = st.columns(3)
        if a.button("Остановить новые сделки", use_container_width=True):
            db.halt("User dashboard kill switch")
            st.rerun()
        if b.button("Остановить входы и закрыть позиции", use_container_width=True):
            with db.transaction():
                db.halt("User requested virtual position flatten")
                db.set("flatten", True)
            st.rerun()
        if c.button("Запросить возобновление после проверки", disabled=stale, use_container_width=True):
            db.set("resume_requested", True)
            st.info("Исполнитель проверит соединение, состояние ордеров и настройки риска.")
        st.write({"Binance REST": db.get("rest_status"), "Public WebSocket": db.get("ws_status"),
                  "Binance DEMO": db.get("demo_status") if mode == "DEMO" else "не используется",
                  "BTC market filter": db.get("btc_bullish"), "last_scan": db.get("last_scan")})
        equity_rows = db.rows("SELECT * FROM equity ORDER BY ts")
        if equity_rows:
            f = pd.DataFrame(equity_rows).set_index("ts")
            f.index = pd.to_datetime(f.index, unit="s", utc=True)
            # Limit browser payload, preserve all SQLite observations.
            f = f.resample("5min").last().dropna()
            st.line_chart(f[["equity"]], x_label="UTC", y_label="USDT")
            st.area_chart((1-f.equity/f.equity.cummax()).rename("drawdown"))
        st.subheader("Открытые позиции: venue отличает PAPER от Binance DEMO")
        st.dataframe(pd.DataFrame(db.positions()), use_container_width=True)
        st.subheader("Лучшие кандидаты")
        st.dataframe(pd.DataFrame(db.get("candidates", [])), use_container_width=True)
        st.subheader("New Listings Hunter")
        st.dataframe(pd.DataFrame(db.get("listing_candidates", [])), use_container_width=True)
        listings = db.rows("SELECT symbol,first_seen,earliest_ms,last_seen,active FROM symbols WHERE "
                           "earliest_ms>? ORDER BY earliest_ms DESC", ((time.time()-7*86400)*1000,))
        st.dataframe(pd.DataFrame(listings), use_container_width=True)
        st.caption("earliest_ms — первая доступная свеча; first_seen — обнаружение ботом. Объявление Binance автоматически не подтверждено.")
        with st.expander("Binance Alpha — информационный список, без Spot-ордеров"):
            st.write(db.get("alpha_status", "Ещё не загружен"))
            st.dataframe(pd.DataFrame(db.get("alpha_watchlist", [])), use_container_width=True)
        st.subheader("Решения и причины отклонения")
        st.dataframe(pd.DataFrame(db.rows("SELECT * FROM decisions ORDER BY id DESC LIMIT 300")), use_container_width=True)
        st.subheader("Закрытые сделки")
        trades = [dict(json.loads(t["data"]), closed=datetime.fromtimestamp(t["closed"], timezone.utc))
                  for t in db.rows("SELECT * FROM trades ORDER BY closed DESC LIMIT 1000")]
        st.dataframe(pd.DataFrame(trades), use_container_width=True)
        st.download_button("Экспорт сделок CSV", pd.DataFrame(trades).to_csv(index=False), "trades.csv", "text/csv")
        st.subheader("Статистика стратегий")
        st.dataframe(pd.DataFrame(report["by_strategy"]).T, use_container_width=True)
        st.json({k: v for k, v in report.items() if k != "by_strategy"}, expanded=False)
        days = (time.time()-db.get("started", time.time()))/86400
        st.info(f"Форвард-наблюдение: {days:.1f} дней. Ориентир: 30–60 дней и достаточное число сделок. "
                "Малая выборка не подтверждает прибыльность. NULL означает, что метрика не определена.")
        with st.expander("Настройки риска"):
            current = db.get("risk_overrides", asdict(settings.risk))
            with st.form("risk"):
                risk_pct = st.number_input("Риск momentum, %", .01, .5, min(float(current["trade_risk"])*100, .5), .01)
                listing_pct = st.number_input("Риск новых листингов, %", .01, .2, float(current["listing_risk"])*100, .01)
                exposure = st.number_input("Суммарная стоимость позиций, %", 1., 25., min(float(current["max_exposure"])*100, 25.), 1.)
                positions = st.number_input("Максимум позиций", 1, 3, int(current["max_positions"]))
                daily = st.number_input("Дневной лимит потерь, %", .1, 5., float(current["daily_loss"])*100, .1)
                weekly = st.number_input("Недельный лимит потерь, %", .1, 10., float(current["weekly_loss"])*100, .1)
                if st.form_submit_button("Сохранить"):
                    current.update(trade_risk=risk_pct/100, listing_risk=listing_pct/100,
                                   max_exposure=exposure/100, max_positions=positions,
                                   daily_loss=daily/100, weekly_loss=weekly/100)
                    RiskConfig(**current).validate()
                    db.set("risk_overrides", current)
                    st.success("Следующее решение Risk Manager использует эти лимиты.")
        with st.expander("События"):
            st.dataframe(pd.DataFrame(db.rows("SELECT * FROM events ORDER BY id DESC LIMIT 200")), use_container_width=True)
    finally:
        db.close()


panel()
