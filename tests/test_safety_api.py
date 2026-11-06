import hashlib
import hmac
from urllib.parse import parse_qsl, urlencode

import httpx
import pytest
import respx

from smart_trader.api import APIError, BinanceAPI
from smart_trader.config import DEMO_URL, PUBLIC_URL, Mode, Settings
from smart_trader.models import Uncertain


@pytest.mark.parametrize("url", [PUBLIC_URL, "https://demo-api.binance.com.evil/api", "http://demo-api.binance.com/api",
                                  "https://demo-api.binance.com:443/api", "https://x@demo-api.binance.com/api",
                                  DEMO_URL+"/", "https://testnet.binance.vision/api"])
def test_no_alternative_trading_domain(url):
    with pytest.raises(ValueError):
        BinanceAPI(demo=True, key="demo", secret="secret", base_url=url)


def test_no_live_mode():
    with pytest.raises(ValueError):
        Mode("LIVE")
    with pytest.raises(ValueError):
        BinanceAPI(key="production-key")


async def test_public_client_cannot_trade():
    api = BinanceAPI()
    try:
        with pytest.raises(ValueError):
            await api.signed("POST", "order", symbol="ETHUSDT")
        with pytest.raises(ValueError):
            await api.request("POST", "order")
        with pytest.raises(ValueError):
            await api.get("../order")
    finally:
        await api.close()


@respx.mock
async def test_hmac_only_demo_and_no_redirect():
    route = respx.post(DEMO_URL+"/v3/order").mock(return_value=httpx.Response(200, json={"status": "FILLED"}))
    api = BinanceAPI(demo=True, key="demo-key", secret="demo-secret")
    try:
        await api.signed("POST", "order", symbol="ETHUSDT", side="BUY", type="MARKET", quantity="1.001")
        request = route.calls[0].request
        pairs = parse_qsl(request.url.query.decode())
        query = urlencode(pairs[:-1])
        assert pairs[-1][1] == hmac.new(b"demo-secret", query.encode(), hashlib.sha256).hexdigest()
        assert request.headers["X-MBX-APIKEY"] == "demo-key"
        assert not api.client.follow_redirects
        assert "demo-secret" not in repr(request)
        assert "demo-secret" not in repr(Settings(demo_secret="demo-secret"))
    finally:
        await api.close()


@pytest.mark.parametrize("response", [httpx.Response(500, json={"code": -1000}),
                                      httpx.Response(200, json={"code": -1007}),
                                      httpx.Response(408, json={})])
@respx.mock
async def test_mutations_never_retry(response):
    route = respx.post(DEMO_URL+"/v3/order").mock(return_value=response)
    api = BinanceAPI(demo=True, key="demo", secret="secret")
    try:
        with pytest.raises(Uncertain):
            await api.signed("POST", "order", symbol="ETHUSDT")
        assert route.call_count == 1
    finally:
        await api.close()


@respx.mock
async def test_get_retries_429_with_retry_after():
    route = respx.get(PUBLIC_URL+"/v3/time").mock(side_effect=[
        httpx.Response(429, headers={"Retry-After": "0"}, json={"code": -1003}),
        httpx.Response(200, json={"serverTime": 123})])
    api = BinanceAPI()
    try:
        assert (await api.get("time"))["serverTime"] == 123
        assert route.call_count == 2
    finally:
        await api.close()


@respx.mock
async def test_restricted_location_fails_without_fallback():
    route = respx.get(PUBLIC_URL+"/v3/time").respond(451, json={"code": 0})
    api = BinanceAPI()
    try:
        with pytest.raises(APIError, match="451"):
            await api.get("time")
        assert route.call_count == 1
    finally:
        await api.close()
