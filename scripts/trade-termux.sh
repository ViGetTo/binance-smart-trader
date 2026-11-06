#!/usr/bin/env bash
set -Eeuo pipefail
trader_config_dir="${XDG_CONFIG_HOME:-$HOME/.config}/binance-smart-trader"
if [[ ! -r "$trader_config_dir/project-path" ]]; then
    printf '%s\n' 'Сначала выполните: bash scripts/termux-install.sh' >&2
    exit 1
fi
IFS= read -r trader_project < "$trader_config_dir/project-path"
if [[ ! -d "$trader_project" || ! -f "$trader_project/pyproject.toml" ]]; then
    printf '%s\n' 'Каталог проекта перемещён. Повторите установку из нового каталога.' >&2
    exit 1
fi
trader_distro=smart-trader
trader_guest=/opt/binance-smart-trader
trader_python="$trader_guest/.venv-termux/bin/python"
trader_session=binance-smart-trader
trader_open_ui=0
if (( $# == 0 )); then trader_open_ui=1; fi
trader_action="${1:-start}"
if (( $# )); then shift; fi
if (( $# )); then
    printf '%s\n' 'Команда не принимает дополнительные аргументы. Список: trade help' >&2
    exit 2
fi

trader_guest_command() {
    proot-distro login --bind "$trader_project:$trader_guest" "$trader_distro" -- \
        "$trader_python" -u -m smart_trader.service "$1"
}
trader_terminal() {
    proot-distro login --bind "$trader_project:$trader_guest" "$trader_distro" -- \
        "$trader_python" -u -m smart_trader.terminal "$1"
}
trader_has_session() { tmux has-session -t "=$trader_session" 2>/dev/null; }

trader_update() {
    local trader_code=0
    local -a trader_update_args=()
    if trader_has_session; then trader_update_args+=(--busy); fi
    if [[ "${1:-}" == force ]]; then trader_update_args+=(--force); fi
    proot-distro login --bind "$trader_project:$trader_guest" "$trader_distro" -- \
        "$trader_python" -u -m smart_trader.updates "${trader_update_args[@]}" || trader_code=$?
    if (( trader_code == 10 )); then
        local trader_new_launcher
        trader_new_launcher=$(mktemp "$PREFIX/bin/.trade-update.XXXXXX")
        {
            printf '#!%s/bin/bash\n' "$PREFIX"
            tail -n +2 "$trader_project/scripts/trade-termux.sh"
        } > "$trader_new_launcher"
        chmod 700 "$trader_new_launcher"
        mv -f -- "$trader_new_launcher" "$PREFIX/bin/trade"
        if [[ "$trader_action" == start ]]; then
            if (( trader_open_ui )); then
                exec env TRADER_UPDATE_CHECKED=1 bash "$trader_project/scripts/trade-termux.sh"
            else
                exec env TRADER_UPDATE_CHECKED=1 bash "$trader_project/scripts/trade-termux.sh" start
            fi
        fi
    elif (( trader_code != 0 )); then
        printf '%s\n' 'Обновление не завершено. Запуск отменён; проверьте trade update.' >&2
        return "$trader_code"
    fi
}

case "$trader_action" in
    start)
        if [[ "${TRADER_UPDATE_CHECKED:-0}" != 1 ]]; then trader_update; fi
        trader_terminal ensure-config
        if trader_has_session; then
            printf '%s\n' 'Сессия уже работает. trade status покажет состояние торгового процесса.'
        else
            mkdir -p -- "$trader_project/data/logs"
            trader_guest_command prepare
            if command -v termux-wake-lock >/dev/null 2>&1; then termux-wake-lock || true; fi
            # Bash %q escapes the project path before tmux hands its command to a shell.
            printf -v trader_command '%q ' proot-distro login --bind "$trader_project:$trader_guest" \
                "$trader_distro" -- "$trader_python" -u -m smart_trader.service run
            printf -v trader_log '%q' "$trader_project/data/logs/trade.log"
            tmux new-session -d -s "$trader_session" "$trader_command >> $trader_log 2>&1"
            sleep 1
            if ! trader_has_session; then
                printf '%s\n' 'Сессия завершилась при запуске. Последние сообщения:' >&2
                tail -n 35 -- "$trader_project/data/logs/trade.log" >&2
                exit 1
            fi
            printf '%s\n' 'Сервис запущен в фоне. Готовность Binance проверьте: trade status'
        fi
        printf '%s\n' 'Терминал: trade ui | Дополнительный сайт: trade web | Логи: trade logs'
        if (( trader_open_ui )); then trader_terminal ui; fi
        ;;
    ui|stats) trader_terminal ui ;;
    config) trader_terminal config ;;
    status|report|doctor) trader_terminal "$trader_action" ;;
    web)
        trader_guest_command web
        printf '%s\n' 'Откройте в браузере этого телефона: http://127.0.0.1:8501. Нужна работающая сессия trade.'
        ;;
    repair)
        trader_guest_command freeze-flat
        if trader_has_session; then tmux kill-session -t "=$trader_session"; fi
        printf '%s\n' 'Плоская сессия остановлена. Дальше: trade config, затем trade и trade resume.'
        ;;
    pause|flatten|resume)
        trader_guest_command "$trader_action"
        ;;
    stop)
        trader_guest_command stop
        if trader_has_session; then
            printf '%s\n' 'Ожидаю подтверждённого закрытия виртуальных позиций (до 30 секунд)...'
            for ((trader_attempt=0; trader_attempt<15; trader_attempt++)); do
                if ! trader_has_session; then break; fi
                sleep 2
            done
            if trader_has_session; then
                printf '%s\n' 'Монитор оставлен работать: закрытие ещё не подтверждено. trade status / trade logs' >&2
                exit 2
            fi
        fi
        printf '%s\n' 'Сессия остановлена. Kill Switch остаётся включённым; для следующих входов: trade, затем trade resume.'
        if command -v termux-wake-unlock >/dev/null 2>&1; then termux-wake-unlock || true; fi
        ;;
    check)
        proot-distro login --bind "$trader_project:$trader_guest" "$trader_distro" -- \
            "$trader_python" -m smart_trader.cli check
        ;;
    logs)
        mkdir -p -- "$trader_project/data/logs"
        touch -- "$trader_project/data/logs/trade.log"
        exec tail -n 80 -F -- "$trader_project/data/logs/trade.log"
        ;;
    attach) exec tmux attach-session -t "=$trader_session" ;;
    update)
        if trader_has_session; then
            printf '%s\n' 'Сначала trade stop и дождитесь закрытия позиций; затем повторите trade update.' >&2
            exit 2
        fi
        trader_update force
        ;;
    help|-h|--help)
        printf '%s\n' 'trade — проверить обновления, запустить виртуальные счета и открыть CLI' \
            'trade start    — только фоновый сервис' \
            'trade ui       — открыть терминальную аналитику; Q закрывает только интерфейс' \
            'trade status   — режим, heartbeat, позиции, остановка входов' \
            'trade report   — сравнение трёх PAPER-счетов: PnL, сделки, просадка, издержки' \
            'trade doctor   — диагностика сервиса, Binance и веб-панели' \
            'trade web      — включить дополнительную веб-панель' \
            'trade logs     — журнал; Ctrl+C закрывает только просмотр' \
            'trade check    — публичная проверка доступности Binance' \
            'trade config   — PAPER / Binance ON-OFF / Telegram / риск / обновления' \
            'trade repair   — остановить зависшую сессию, только если нет позиций/intents' \
            'trade pause    — остановить входы, оставить защиту и монитор' \
            'trade flatten  — остановить входы и запросить закрытие позиций' \
            'trade stop     — закрыть позиции и затем остановить сервис' \
            'trade resume   — запросить возобновление после проверки риска' \
            'trade attach   — консоль сервиса; Ctrl+B, затем D: выйти без остановки' \
            'trade update   — принудительная проверка и установка после остановки'
        ;;
    *) printf '%s\n' 'Неизвестная команда. trade help' >&2; exit 2 ;;
esac
