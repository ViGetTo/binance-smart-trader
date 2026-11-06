# Официальные API-источники

Сверено при разработке 2026-11-05. Для реализации загружены официальные Markdown Binance;
версия на стороне Binance может изменяться независимо от проекта. Перед изменением параметров
ордеров повторно проверьте документацию. Spot Testnet и Spot Demo — разные окружения.

| Контракт | Официальный источник |
|---|---|
| Demo REST и WebSocket, различия с Testnet | https://github.com/binance/binance-spot-api-docs/blob/master/demo-mode/general-info.md |
| Spot REST, HMAC, неопределённый результат | https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md |
| Trading endpoints, OCO | https://developers.binance.com/docs/binance-spot-api-docs/rest-api/trading-endpoints |
| Фильтры символов, exchange/account limits | https://github.com/binance/binance-spot-api-docs/blob/master/filters.md |
| Public market WebSocket | https://github.com/binance/binance-spot-api-docs/blob/master/web-socket-streams.md |
| Alpha token list, только информация | https://developers.binance.com/docs/alpha/market-data/rest-api/token-list |
| Telegram sendMessage | https://core.telegram.org/bots/api#sendmessage |

Локальные копии: [Demo Mode](api/demo-mode.md), [filters](api/filters.md).
Они служат справкой, а не кодом, динамически загружаемым при запуске.

## Используемые endpoints

Public production GET: `/api/v3/time`, `/exchangeInfo`, `/ticker/24hr`, `/klines`, `/depth`,
`/avgPrice`. Production WebSocket: `wss://stream.binance.com:443/ws/!miniTicker@arr`.
Market WS показывает состояние/котировки, финальное исполнение использует свежий REST depth.
WebSocket переподключается, отвечает на server pings библиотекой, учитывает serverShutdown;
после разрыва стакан заново запрашивается REST. Локальная diff-depth книга не поддерживается,
поэтому отсутствует проблема неприменённых пропусков sequence diff events.

Только DEMO signed: `/api/v3/account`, `/account/commission`, `/myFilters`, `/order/test`,
`POST/GET/DELETE /order`, `POST /orderList/oco`, `GET/DELETE /orderList`, `/myTrades`,
`/openOrders`, `/openOrderList`. Сверка ордеров выполняется REST-поллингом; приватный user data
WebSocket в этой версии не используется. Реальный account WS здесь не заявляется.

Demo root в коде: `https://demo-api.binance.com/api`. Добавляется `/v3/…`, а не повторный `/api`.
OCO: `side=SELL`, `aboveType=TAKE_PROFIT`, `aboveStopPrice=target`,
`belowType=STOP_LOSS`, `belowStopPrice=stop`, стабильные client IDs для списка и обеих веток.
Не используются неподтверждённые параметры «reduceOnly», «leverage» или «positionSide» для Spot.

## Границы проверки

Публичный и Demo `/api/v3/time` из этой среды ответили HTTP 451 с сообщением Binance об
ограничении по местоположению. Проверка не перенаправлялась через другие домены или регион.
Demo-ключи не предоставлялись. Следовательно: endpoint подтверждён документацией, сетевое
исполнение из этой среды недоступно, размещение настоящих Demo-ордеров не проверено.
