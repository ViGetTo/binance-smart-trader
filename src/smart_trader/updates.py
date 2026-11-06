"""Bounded, fast-forward-only updates. Never alter a running service or local secrets."""
import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

from .config import Settings
from .db import Database, encode
from .health import service_process_alive
from .runtime import ProcessLock

OFFICIAL_REMOTES = {"https://github.com/ViGetTo/binance-smart-trader.git",
                    "https://github.com/ViGetTo/binance-smart-trader",
                    "git@github.com:ViGetTo/binance-smart-trader.git"}


def run(args, root, timeout=20):
    return subprocess.run(args, cwd=root, env=dict(os.environ, GIT_TERMINAL_PROMPT="0",
                          GCM_INTERACTIVE="never", PIP_NO_INPUT="1"), timeout=timeout,
                          text=True, capture_output=True, check=True).stdout.strip()


def service_active(settings):
    paths = [settings.db_path]
    if settings.paper_lab:
        from .lab import account_settings
        paths.extend(cfg.db_path for cfg in account_settings(settings))
    for path in paths:
        if not Path(path).exists():
            continue
        db = Database(path)
        try:
            if db.positions() or db.rows("SELECT id FROM intents WHERE status NOT IN ('CLOSED','REJECTED')"):
                return True
            if service_process_alive(db.get("service_pid")):
                return True
            stopped = db.get("service_stop", False) and db.get("service_heartbeat", 0) == 0
            if not stopped and (time.time()-db.get("service_heartbeat", 0) < 30 or time.time()-db.get("heartbeat", 0) < 30):
                return True
        finally:
            db.close()
    return False


def update(root, settings, *, busy=False, force=False, runner=run):
    root = Path(root)
    if not settings.auto_update and not force:
        return "disabled", "Автообновление выключено в trade config"
    try:
        remote = runner(["git", "remote", "get-url", "origin"], root)
        if remote not in OFFICIAL_REMOTES:
            return "skipped", "Обновление пропущено: origin отличается от ViGetTo/binance-smart-trader"
        if runner(["git", "branch", "--show-current"], root) != "main":
            return "skipped", "Обновление пропущено: выбрана ветка, отличная от main"
        runner(["git", "fetch", "--no-tags", "origin", "main"], root)
        target = runner(["git", "rev-parse", "refs/remotes/origin/main"], root)
        current = runner(["git", "rev-parse", "HEAD"], root)
        marker = root/"data"/"update-install-pending"
        if current == target and not marker.exists():
            return "current", "GitHub: установлена актуальная версия"
        if busy or service_active(settings):
            return "deferred", "GitHub: обновление найдено; установка после trade stop, при следующем trade"
        if runner(["git", "status", "--porcelain", "--untracked-files=no"], root):
            return "skipped", "Обновление пропущено: есть локальные изменения кода; они сохранены"
        runner(["git", "merge-base", "--is-ancestor", current, target], root)
        # No stash, hard reset, clean, dependency command from a response, or secret-file rewrite.
        runner(["git", "merge", "--ff-only", target], root)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(target, "utf-8")
        try:
            runner([sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--no-input", "-e", "."],
                   root, timeout=240)
            runner([sys.executable, "-m", "pip", "check"], root, timeout=30)
        except (subprocess.SubprocessError, OSError):
            return "install_failed", "Код скачан, установка зависимостей не завершена. Запуск заблокирован; повторите trade update"
        marker.unlink()
        return "updated", f"Обновлено с GitHub: {current[:8]} → {target[:8]}; .env и история счетов сохранены"
    except (subprocess.SubprocessError, OSError):
        return "unavailable", "Проверка GitHub не завершена (сеть, доступ или история Git); текущая версия сохранена"


def main():
    parser = argparse.ArgumentParser(description="Safe GitHub updater")
    parser.add_argument("--busy", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    from .service import project_directory
    root = project_directory()
    os.chdir(root)
    try:
        with ProcessLock(str(root/"data"/"update.lock")):
            status, message = update(root, Settings.load(), busy=args.busy, force=args.force)
            (root/"data"/"update-status.json").write_text(encode({"ts": time.time(), "status": status,
                                                               "message": message}), "utf-8")
            print(message, flush=True)
            if status == "install_failed" or (root/"data"/"update-install-pending").exists():
                raise SystemExit(3)
            if status == "updated":
                raise SystemExit(10)
    except OSError:
        print("Другой процесс уже проверяет обновления; повторите trade через несколько секунд")
        raise SystemExit(3) from None


if __name__ == "__main__":
    main()
