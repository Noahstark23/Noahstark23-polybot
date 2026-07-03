"""
Cliente REST del CLOB de Polymarket (clob.polymarket.com).

F0: SOLO lecturas públicas (get_markets, get_market, get_orderbook, get_midpoint).
F1: auth L1 (EIP-712) para derivar credenciales L2, get_balance_allowance,
    build_order (firma, NO postea).
F3: post_order — gated: exige TRADING_ENABLED + POLYMARKET_ENV=production
    + check NO-GO verde. Nunca lo llama nadie fuera del Executor.

Los endpoints reflejan la doc del CLOB conocida a la fecha; el smoke test y el
gate F1 los validan contra el API real (este sandbox no tiene salida a
polymarket.com, CI y el droplet sí).
"""
from __future__ import annotations

import hashlib
import hmac
import time
from base64 import urlsafe_b64decode, urlsafe_b64encode
from typing import Any

import httpx
from loguru import logger

from src.utils.config import Settings, get_settings


class ClobApiError(RuntimeError):
    """Error de la API del CLOB (status != 2xx)."""

    def __init__(self, status_code: int, body: str) -> None:
        self.status_code = status_code
        self.body = body
        super().__init__(f"CLOB API {status_code}: {body[:300]}")


class PolymarketClobClient:
    """Cliente asyncio del CLOB. Inyectable con transport para tests."""

    def __init__(
        self,
        settings: Settings | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 15.0,
    ) -> None:
        self.settings = settings or get_settings()
        self._client = httpx.AsyncClient(
            base_url=self.settings.CLOB_API_URL,
            timeout=timeout,
            transport=transport,
        )
        # Credenciales L2 (se derivan en F1 o vienen de secrets)
        self._api_key = self.settings.CLOB_API_KEY
        self._api_secret = self.settings.CLOB_SECRET
        self._api_passphrase = self.settings.CLOB_PASSPHRASE

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> PolymarketClobClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # ==================================================
    # Helpers
    # ==================================================

    async def _get(self, path: str, params: dict[str, Any] | None = None,
                   headers: dict[str, str] | None = None) -> Any:
        resp = await self._client.get(path, params=params, headers=headers)
        if resp.status_code >= 300:
            raise ClobApiError(resp.status_code, resp.text)
        return resp.json()

    # ==================================================
    # F0 — lecturas públicas
    # ==================================================

    async def get_ok(self) -> bool:
        """Ping de conectividad (GET /)."""
        resp = await self._client.get("/")
        return resp.status_code < 300

    async def get_markets(self, next_cursor: str = "") -> dict[str, Any]:
        """
        Página de mercados. Devuelve {"data": [...], "next_cursor": "...",
        "limit": n, "count": n}. Cursor "LTE=" significa fin.
        """
        params = {"next_cursor": next_cursor} if next_cursor else {}
        return await self._get("/markets", params=params)

    async def get_sampling_markets(self, next_cursor: str = "") -> dict[str, Any]:
        """Mercados con rewards activos (suelen ser los líquidos)."""
        params = {"next_cursor": next_cursor} if next_cursor else {}
        return await self._get("/sampling-markets", params=params)

    async def get_market(self, condition_id: str) -> dict[str, Any]:
        """Metadata de un mercado por condition_id (tokens, tick_size, neg_risk...)."""
        return await self._get(f"/markets/{condition_id}")

    async def get_orderbook(self, token_id: str) -> dict[str, Any]:
        """Orderbook de un token: {"bids": [{price,size}...], "asks": [...], "hash": ...}."""
        return await self._get("/book", params={"token_id": token_id})

    async def get_midpoint(self, token_id: str) -> dict[str, Any]:
        return await self._get("/midpoint", params={"token_id": token_id})

    async def get_price(self, token_id: str, side: str) -> dict[str, Any]:
        """Mejor precio para un lado ("buy"/"sell")."""
        return await self._get("/price", params={"token_id": token_id, "side": side})

    # ==================================================
    # F1 — auth L1 (EIP-712) -> credenciales L2
    # ==================================================

    def _l1_headers(self, nonce: int = 0) -> dict[str, str]:
        """Headers de auth L1: firma EIP-712 ClobAuth con la wallet key."""
        from src.auth.eip712_signer import Eip712Signer  # import diferido (F1)

        signer = Eip712Signer.from_settings(self.settings)
        ts = str(int(time.time()))
        sig = signer.sign_clob_auth(timestamp=ts, nonce=nonce)
        return {
            "POLY_ADDRESS": signer.address,
            "POLY_SIGNATURE": sig,
            "POLY_TIMESTAMP": ts,
            "POLY_NONCE": str(nonce),
        }

    async def derive_api_key(self, nonce: int = 0) -> dict[str, str]:
        """
        Deriva credenciales L2 existentes desde la firma del wallet
        (GET /auth/derive-api-key). Si no existen, crearlas con create_api_key.
        """
        data = await self._get("/auth/derive-api-key", headers=self._l1_headers(nonce))
        self._api_key = data.get("apiKey", "")
        self._api_secret = data.get("secret", "")
        self._api_passphrase = data.get("passphrase", "")
        logger.info("Credenciales L2 derivadas del wallet")
        return data

    async def create_api_key(self, nonce: int = 0) -> dict[str, str]:
        """Crea credenciales L2 nuevas (POST /auth/api-key)."""
        resp = await self._client.post("/auth/api-key", headers=self._l1_headers(nonce))
        if resp.status_code >= 300:
            raise ClobApiError(resp.status_code, resp.text)
        data = resp.json()
        self._api_key = data.get("apiKey", "")
        self._api_secret = data.get("secret", "")
        self._api_passphrase = data.get("passphrase", "")
        return data

    @property
    def has_l2_credentials(self) -> bool:
        return bool(self._api_key and self._api_secret and self._api_passphrase)

    def _l2_headers(self, method: str, path: str, body: str = "") -> dict[str, str]:
        """Headers de auth L2: HMAC-SHA256 (base64 url-safe) sobre ts+method+path+body."""
        if not self.has_l2_credentials:
            raise RuntimeError("Sin credenciales L2 — correr derive_api_key() primero")
        ts = str(int(time.time()))
        message = ts + method.upper() + path + body
        secret = urlsafe_b64decode(self._api_secret)
        digest = hmac.new(secret, message.encode(), hashlib.sha256).digest()
        sig = urlsafe_b64encode(digest).decode()
        return {
            "POLY_ADDRESS": self.settings.POLY_WALLET_ADDRESS,
            "POLY_SIGNATURE": sig,
            "POLY_TIMESTAMP": ts,
            "POLY_API_KEY": self._api_key,
            "POLY_PASSPHRASE": self._api_passphrase,
        }

    async def get_balance_allowance(
        self, asset_type: str = "COLLATERAL", token_id: str | None = None
    ) -> dict[str, Any]:
        """Balance y allowance de USDC (COLLATERAL) o de un token condicional."""
        path = "/balance-allowance"
        params: dict[str, Any] = {"asset_type": asset_type}
        if token_id:
            params["token_id"] = token_id
        return await self._get(path, params=params, headers=self._l2_headers("GET", path))

    # ==================================================
    # F1 — build_order (firma, NO postea) / F3 — post_order (gated)
    # ==================================================

    def build_order(
        self,
        token_id: str,
        price: float,
        size: float,
        side: str,
        fee_rate_bps: int | None = None,
        nonce: int = 0,
        expiration: int = 0,
    ) -> dict[str, Any]:
        """
        Construye y FIRMA una orden EIP-712 del CLOB. NO la postea.
        Devuelve el payload listo para POST /order (lo usa sólo el Executor en F3).
        """
        from src.auth.eip712_signer import Eip712Signer  # import diferido (F1)

        signer = Eip712Signer.from_settings(self.settings)
        return signer.build_signed_order(
            token_id=token_id,
            price=price,
            size=size,
            side=side,
            fee_rate_bps=(
                fee_rate_bps if fee_rate_bps is not None else self.settings.FEE_RATE_BPS
            ),
            nonce=nonce,
            expiration=expiration,
        )

    async def post_order(self, signed_order: dict[str, Any], order_type: str = "GTC") -> dict[str, Any]:
        """
        F3: postea una orden firmada. GATED — jamás se llama fuera del Executor.
        Lanza RuntimeError si el trading no está habilitado por el humano.
        """
        if not self.settings.TRADING_ENABLED:
            raise RuntimeError(
                "post_order bloqueado: TRADING_ENABLED=false. "
                "Encender trading lo hace el humano (ARCHITECTURE.md §9)."
            )
        if not self.settings.is_production:
            raise RuntimeError("post_order bloqueado: POLYMARKET_ENV != production.")
        path = "/order"
        import json as _json

        body = _json.dumps({"order": signed_order, "orderType": order_type})
        resp = await self._client.post(
            path,
            content=body,
            headers={**self._l2_headers("POST", path, body), "Content-Type": "application/json"},
        )
        if resp.status_code >= 300:
            raise ClobApiError(resp.status_code, resp.text)
        return resp.json()

    async def cancel_order(self, order_id: str) -> dict[str, Any]:
        """Cancela una orden viva (F3)."""
        path = "/order"
        import json as _json

        body = _json.dumps({"orderID": order_id})
        resp = await self._client.request(
            "DELETE",
            path,
            content=body,
            headers={**self._l2_headers("DELETE", path, body), "Content-Type": "application/json"},
        )
        if resp.status_code >= 300:
            raise ClobApiError(resp.status_code, resp.text)
        return resp.json()

    async def get_open_orders(self, market: str | None = None) -> Any:
        """Órdenes vivas del wallet (F3, reconciliación)."""
        path = "/data/orders"
        params = {"market": market} if market else None
        return await self._get(path, params=params, headers=self._l2_headers("GET", path))

    async def get_positions(self) -> Any:
        """
        Posiciones on-chain del wallet vía data-api (F3, reconciliación).
        Nota: el endpoint vive en data-api.polymarket.com; se consulta con httpx
        aparte porque no requiere auth para direcciones públicas.
        """
        url = "https://data-api.polymarket.com/positions"
        resp = await self._client.get(
            url, params={"user": self.settings.POLY_WALLET_ADDRESS}
        )
        if resp.status_code >= 300:
            raise ClobApiError(resp.status_code, resp.text)
        return resp.json()
