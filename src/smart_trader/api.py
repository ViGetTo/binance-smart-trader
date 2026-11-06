import asyncio
import hashlib
import hmac
import time
from urllib.parse import urlencode

import httpx

from .config import DEMO_URL, PUBLIC_URL
from .models import Uncertain


class APIError(Exception):
    def __init__(self, status, code=None):
        self.status, self.code = status, code
        # Never include URLs/query strings/headers or remote text containing credentials.
        super().__init__(f"Binance HTTP {status}, code {code}")


class BinanceAPI:
    """Public GET-only client, or strictly demo-bound signed client. No redirect following."""
    PUBLIC_PATHS = {"ping", "time", "exchangeInfo", "ticker/24hr", "ticker/bookTicker", "depth",
                    "klines", "avgPrice"}
    SIGNED_PATHS = {"account", "account/commission", "order", "order/test", "orderList/oco",
                    "orderList", "openOrders", "openOrderList", "myTrades", "myFilters"}

    def __init__(self, *, demo=False, key="", secret="", base_url=None, transport=None):
        self.demo = demo
        self.base = base_url or (DEMO_URL if demo else PUBLIC_URL)
        if self.base != (DEMO_URL if demo else PUBLIC_URL):
            raise ValueError("Endpoint is not on the exact allowlist")
        if not demo and (key or secret):
            raise ValueError("Public client cannot accept API keys")
        if demo and not (key and secret):
            raise ValueError("Demo HMAC credentials are required")
        self._key, self._secret = key, secret
        self.offset = 0
        self.client = httpx.AsyncClient(timeout=15, follow_redirects=False, transport=transport)
        self.lock = asyncio.Lock()
        self.next_request = 0.0
        self.blocked_until = 0.0
        self.min_interval = .15  # < 400 req/min even at weight 5; dynamic weight budget below.

    async def sync_time(self):
        before = time.time()*1000
        raw = await self.request("GET", "time")
        self.offset = raw["serverTime"] - (before + time.time()*1000)/2

    async def request(self, method, path, params=None, *, signed=False, before_send=None):
        if path.startswith("/") or ".." in path or "?" in path:
            raise ValueError("Invalid endpoint path")
        if signed:
            if not self.demo or self.base != DEMO_URL or path not in self.SIGNED_PATHS:
                raise ValueError("Signed requests are strictly demo-only")
        elif method != "GET" or path not in self.PUBLIC_PATHS:
            raise ValueError("Unsigned client is public GET-only")
        # Repeat only read operations; no automatic repeat for mutations, even HTTP 5xx.
        attempts = 4 if method == "GET" else 1
        for attempt in range(attempts):
            async with self.lock:
                delay = max(self.next_request, self.blocked_until) - time.monotonic()
                while delay > 0:
                    await asyncio.sleep(min(delay, 30))
                    delay = max(self.next_request, self.blocked_until) - time.monotonic()
                self.next_request = time.monotonic() + self.min_interval
                payload = dict(params or {})
                headers = {}
                if signed:
                    payload.update(timestamp=int(time.time()*1000+self.offset), recvWindow=5000)
                    query = urlencode(payload)
                    payload["signature"] = hmac.new(self._secret.encode(), query.encode(), hashlib.sha256).hexdigest()
                    headers["X-MBX-APIKEY"] = self._key
                url = f"{self.base}/v3/{path}"
                if before_send is not None:
                    before_send()
                if signed and url != f"{DEMO_URL}/v3/{path}":
                    raise ValueError("Final signed-request URL must use the exact Demo root")
                try:
                    response = await self.client.request(method, url, params=payload, headers=headers)
                except httpx.TransportError:
                    if method != "GET":
                        raise Uncertain("Transport error: mutation status unknown") from None
                    if attempt + 1 == attempts:
                        raise APIError(0) from None
                    continue
                weight = int(response.headers.get("X-MBX-USED-WEIGHT-1M", "0"))
                if weight >= 4500:
                    self.blocked_until = time.monotonic() + 61
                if response.status_code in (418, 429):
                    self.blocked_until = time.monotonic() + max(1, float(response.headers.get("Retry-After", "60")))
                try:
                    raw = response.json()
                except ValueError:
                    raw = {}
                code = raw.get("code") if isinstance(raw, dict) else None
                if response.status_code >= 500 or response.status_code == 408 or code in (-1006, -1007, -1000):
                    if method != "GET":
                        raise Uncertain("Binance mutation outcome unknown")
                if response.status_code == 200 and not (isinstance(code, int) and code < 0):
                    return raw
                if method == "GET" and (response.status_code in (418, 429) or response.status_code >= 500):
                    if attempt + 1 < attempts:
                        self.next_request = max(self.next_request, time.monotonic()+2**attempt)
                        continue
                raise APIError(response.status_code, code)
        raise APIError(503)

    async def get(self, path, **params):
        return await self.request("GET", path, params)

    async def signed(self, method, path, *, before_send=None, **params):
        return await self.request(method, path, params, signed=True, before_send=before_send)

    async def close(self):
        await self.client.aclose()
