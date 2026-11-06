#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

if [[ -z "${PREFIX:-}" || ! -x "${PREFIX}/bin/pkg" ]]; then
    printf '%s\n' 'Запустите этот скрипт в обычном Termux, снаружи Ubuntu/proot.' >&2
    exit 1
fi

trader_project=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
trader_distro=smart-trader
trader_guest=/opt/binance-smart-trader
case "$trader_project" in
    *:*|*$'\n'*) printf '%s\n' 'Путь к проекту не должен содержать двоеточие или перенос строки.' >&2; exit 1 ;;
    /sdcard/*|/storage/*) printf '%s\n' 'Перенесите проект в домашний каталог Termux: Ubuntu venv требует Linux filesystem.' >&2; exit 1 ;;
esac
case "$(uname -m)" in
    aarch64|x86_64) ;;
    *) printf '%s\n' 'Эта установка рассчитана на 64-битный Android (aarch64 или x86_64).' >&2; exit 1 ;;
esac

pkg update -y
pkg install -y bash proot-distro tmux git nano

if ! proot-distro login "$trader_distro" -- /bin/true >/dev/null 2>&1; then
    trader_install_help=$(proot-distro install --help 2>&1 || true)
    if [[ "$trader_install_help" == *'--name'* ]]; then
        proot-distro install ubuntu:24.04 --name "$trader_distro"
    elif [[ "$trader_install_help" == *'--override-alias'* ]]; then
        printf '%s\n' 'Обнаружен старый proot-distro. Устанавливаю его Ubuntu под отдельным именем.'
        proot-distro install --override-alias "$trader_distro" ubuntu
    else
        printf '%s\n' 'Неизвестная версия proot-distro. Обновите Termux и пакет proot-distro.' >&2
        exit 1
    fi
fi

proot-distro login "$trader_distro" -- /bin/mkdir -p "$trader_guest"
if tmux has-session -t '=binance-smart-trader' 2>/dev/null; then
    printf '%s\n' 'Проверяю отсутствие позиций перед обновлением работающей сессии...'
    proot-distro login --bind "$trader_project:$trader_guest" "$trader_distro" -- \
        "$trader_guest/.venv-termux/bin/python" -m smart_trader.service freeze-flat
    tmux kill-session -t '=binance-smart-trader'
fi
proot-distro login --bind "$trader_project:$trader_guest" "$trader_distro" -- \
    /bin/bash "$trader_guest/scripts/ubuntu-install.sh"

trader_config_dir="${XDG_CONFIG_HOME:-$HOME/.config}/binance-smart-trader"
mkdir -p -- "$trader_config_dir"
printf '%s\n' "$trader_project" > "$trader_config_dir/project-path"
{
    printf '#!%s/bin/bash\n' "$PREFIX"
    tail -n +2 "$trader_project/scripts/trade-termux.sh"
} > "$PREFIX/bin/trade"
chmod 700 "$PREFIX/bin/trade"
printf '\n%s\n' 'Установка завершена. Теперь из любого каталога: trade' \
    'PAPER без ключей / Binance ON-OFF / Telegram: trade config | Аналитика: trade' \
    'Статус: trade status | Журнал: trade logs | Безопасная остановка: trade stop' \
    'По умолчанию: три PAPER-счёта, реальные публичные котировки; утром: trade report.'
