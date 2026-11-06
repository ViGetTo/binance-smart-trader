"""Termux service supervisor and persistent controls. Trading stays in the existing gateway."""
import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from .config import Mode, Settings
from .db import Database
from .runtime import ProcessLock


def project_directory():
    # Editable installation is intentional for the Termux checkout mounted at /opt.
    return Path(__file__).resolve().parents[2]


def unresolved(db):
    return db.positions() or db.rows("SELECT id FROM intents WHERE status NOT IN ('CLOSED','REJECTED')")


def control(db, action):
    if action == "prepare":
        # A fresh explicit start may clear an old completed stop, never a pending liquidation.
        if not unresolved(db):
            db.set("service_stop", False)
        return {"message": "Старт подготовлен; Kill Switch и ожидающее закрытие сохранены"}
    if action == "status":
        heartbeat = db.get("heartbeat", 0)
        return {"mode": db.get("mode", "PAPER"), "service_pid": db.get("service_pid"),
                "service_heartbeat_age_seconds": round(time.time()-db.get("service_heartbeat", 0), 1),
                "trader_heartbeat_age_seconds": round(time.time()-heartbeat, 1) if heartbeat else None,
                "kill_switch": db.get("kill", False), "reason": db.get("kill_reason", ""),
                "stop_requested": db.get("service_stop", False), "flatten_requested": db.get("flatten", False),
                "equity": db.get("equity", db.get("cash")), "positions": db.positions(),
                "pending_intents": db.rows("SELECT id,symbol,venue,status FROM intents WHERE status NOT IN ('OPEN','CLOSED','REJECTED')"),
                "last_process_exit": db.get("service_last_exit"), "dashboard": "http://127.0.0.1:8501"}
    if action == "resume":
        if db.get("service_stop", False):
            raise ValueError("Сначала дождитесь trade stop, затем запустите trade и повторите trade resume")
        db.set("resume_requested", True)
        return {"message": "Запрошена проверка здоровья соединения, ордеров и риска перед возобновлением"}
    if action not in {"pause", "flatten", "stop"}:
        raise ValueError("Unknown service control")
    with db.transaction():
        db.halt(f"Termux user command: {action}")
        if action in {"flatten", "stop"}:
            db.set("flatten", True)
        if action == "stop":
            db.set("service_stop", True)
    return {"message": "Новые входы остановлены. Закрытие выполняет работающий монитор.", "command": action}


def terminate(process):
    if process is None or process.poll() is not None:
        return
    process.send_signal(signal.SIGINT)
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def supervise(root, db, *, popen=subprocess.Popen, sleep=time.sleep, stop_event=None):
    bot = dashboard = None
    retry_at = 0.0
    retry_delay = 5
    dashboard_retry_at = 0.0
    environment = dict(os.environ, PYTHONUNBUFFERED="1", TZ="UTC", OPENBLAS_NUM_THREADS="1",
                       OMP_NUM_THREADS="1", NUMEXPR_NUM_THREADS="1")
    db.set("service_pid", os.getpid())
    print("Binance Smart Trader: только PAPER/DEMO. Панель http://127.0.0.1:8501", flush=True)
    try:
        while True:
            if stop_event is not None and stop_event.is_set():
                control(db, "stop")
                stop_event.clear()
                print("Получен сигнал остановки; монитор продолжает работу до закрытия.", flush=True)
            now = time.time()
            db.set("service_heartbeat", now)
            if db.get("service_stop", False) and not unresolved(db):
                # No position, unresolved buy, or protective order intent may be abandoned.
                print("Все позиции и intents закрыты. Останавливаю процессы.", flush=True)
                break
            if bot is not None and bot.poll() is not None:
                code = bot.returncode
                db.set("service_last_exit", {"code": code, "ts": now})
                db.event("PROCESS", f"Trader exited {code}; retry in {retry_delay}s")
                print(f"Торговый процесс завершился: {code}. Повтор через {retry_delay} с; состояние в SQLite сохранено.", flush=True)
                bot = None
                retry_at = now+retry_delay
                retry_delay = min(60, retry_delay*2)
            if bot is None and now >= retry_at:
                bot = popen([sys.executable, "-u", "-m", "smart_trader.cli", "run"],
                            cwd=root, env=environment, start_new_session=True)
            if dashboard is not None and dashboard.poll() is not None:
                print("Панель завершилась; повтор запуска через 30 с. Бот продолжает работать.", flush=True)
                dashboard = None
                dashboard_retry_at = now+30
            if dashboard is None and now >= dashboard_retry_at:
                dashboard = popen([sys.executable, "-u", "-m", "streamlit", "run", str(root/"dashboard.py"),
                                   "--server.address=127.0.0.1", "--server.port=8501", "--server.headless=true",
                                   "--browser.gatherUsageStats=false"], cwd=root, env=environment, start_new_session=True)
            sleep(1)
    finally:
        # Signal handlers request a flatten and keep the loop alive until it is confirmed.
        terminate(bot)
        terminate(dashboard)
        db.set("service_pid", None)
        db.set("service_heartbeat", 0)


def main():
    parser = argparse.ArgumentParser(description="Virtual-only Termux service")
    parser.add_argument("action", choices=["run", "status", "pause", "flatten", "stop", "resume", "prepare"])
    args = parser.parse_args()
    root = project_directory()
    os.chdir(root)
    settings = Settings.load()
    if settings.mode == Mode.BACKTEST and args.action == "run":
        parser.error("trade запускает PAPER/DEMO; для BACKTEST используйте smart-trader backtest")
    db = Database(settings.db_path, settings.risk.initial_balance)
    try:
        if args.action != "run":
            print(json.dumps(control(db, args.action), ensure_ascii=False, indent=2))
            return
        with ProcessLock(settings.db_path+".service.lock"):
            event = threading.Event()
            def stop_requested(signum, frame):
                # No SQLite work in a signal handler: it could interrupt an active transaction.
                event.set()
            signal.signal(signal.SIGINT, stop_requested)
            signal.signal(signal.SIGTERM, stop_requested)
            supervise(root, db, stop_event=event)
    except (ValueError, OSError) as exc:
        print(f"Service error: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(2)
    finally:
        db.close()


if __name__ == "__main__":
    main()
