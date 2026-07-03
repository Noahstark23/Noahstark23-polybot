"""
Firma EIP-712 para el CLOB de Polymarket (F1).

Dos dominios:
    1. ClobAuth (L1): atestación de control del wallet para derivar/crear
       credenciales L2 (headers POLY_ADDRESS/POLY_SIGNATURE/POLY_TIMESTAMP/POLY_NONCE).
    2. CTF Exchange: firma de órdenes off-chain que el operador matchea on-chain.

⚠️ El bot firma SOLO órdenes del CLOB. Jamás firma transacciones de custodia
   (approvals/transfers) — eso lo hace el humano una única vez (ARCHITECTURE.md §3).

La private key se lee del secret volume (POLY_SIGNER_KEY_PATH): hex crudo o
keystore JSON cifrado (con POLY_KEYSTORE_PASSWORD). Nunca de un env var en claro.

Verificado contra vector fijo en tests/test_eip712_signer.py; el gate F1 exige
además validar una firma contra el API real (derive_api_key devuelve 200).
"""
from __future__ import annotations

import json
import secrets
from pathlib import Path
from typing import Any

from eth_account import Account
from eth_account.messages import encode_typed_data

from src.utils.config import Settings, get_settings

# Contratos del exchange en Polygon (chainId 137)
CTF_EXCHANGE = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
NEG_RISK_CTF_EXCHANGE = "0xC5d563A36AE78145C45a50134d48A1215220f80a"

CLOB_AUTH_MESSAGE = "This message attests that I control the given wallet"

# Enums del contrato
SIDE_BUY = 0
SIDE_SELL = 1
SIGNATURE_TYPE_EOA = 0

USDC_DECIMALS = 10**6  # USDC y shares del CTF usan 6 decimales


def _round_down(value: float, decimals: int = 2) -> float:
    """Truncar hacia abajo para no exceder ni el balance ni el size disponible."""
    factor = 10**decimals
    return int(value * factor + 1e-9) / factor


class Eip712Signer:
    """Firma mensajes EIP-712 del CLOB con la key del wallet."""

    def __init__(self, private_key: str, chain_id: int = 137) -> None:
        self._account = Account.from_key(private_key)
        self.chain_id = chain_id

    # ==================================================
    # Construcción
    # ==================================================

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> Eip712Signer:
        settings = settings or get_settings()
        if settings.POLY_SIGNER_KEY_PATH is None:
            raise RuntimeError(
                "POLY_SIGNER_KEY_PATH sin configurar — la wallet key va en secret volume"
            )
        key = cls._load_key(settings.POLY_SIGNER_KEY_PATH, settings.POLY_KEYSTORE_PASSWORD)
        signer = cls(key, chain_id=settings.POLYGON_CHAIN_ID)
        configured = settings.POLY_WALLET_ADDRESS
        if int(configured, 16) != 0 and configured.lower() != signer.address.lower():
            raise RuntimeError(
                "POLY_WALLET_ADDRESS no coincide con la key del secret volume "
                f"({configured} != {signer.address})"
            )
        return signer

    @staticmethod
    def _load_key(path: Path, keystore_password: str = "") -> str:
        """Lee la key del secret volume: hex crudo o keystore JSON cifrado."""
        raw = path.read_text().strip()
        if raw.startswith("{"):
            keystore = json.loads(raw)
            if not keystore_password:
                raise RuntimeError(
                    "El keystore está cifrado — configurar POLY_KEYSTORE_PASSWORD"
                )
            return Account.decrypt(keystore, keystore_password).hex()
        return raw if raw.startswith("0x") else f"0x{raw}"

    @property
    def address(self) -> str:
        return self._account.address

    # ==================================================
    # L1: ClobAuth (derivar credenciales L2)
    # ==================================================

    def clob_auth_typed_data(self, timestamp: str, nonce: int = 0) -> dict[str, Any]:
        return {
            "types": {
                "EIP712Domain": [
                    {"name": "name", "type": "string"},
                    {"name": "version", "type": "string"},
                    {"name": "chainId", "type": "uint256"},
                ],
                "ClobAuth": [
                    {"name": "address", "type": "address"},
                    {"name": "timestamp", "type": "string"},
                    {"name": "nonce", "type": "uint256"},
                    {"name": "message", "type": "string"},
                ],
            },
            "primaryType": "ClobAuth",
            "domain": {"name": "ClobAuthDomain", "version": "1", "chainId": self.chain_id},
            "message": {
                "address": self.address,
                "timestamp": timestamp,
                "nonce": nonce,
                "message": CLOB_AUTH_MESSAGE,
            },
        }

    def sign_clob_auth(self, timestamp: str, nonce: int = 0) -> str:
        """Firma la atestación L1. Devuelve la firma hex (0x...)."""
        signable = encode_typed_data(full_message=self.clob_auth_typed_data(timestamp, nonce))
        signed = self._account.sign_message(signable)
        return signed.signature.hex() if signed.signature.hex().startswith("0x") else f"0x{signed.signature.hex()}"

    # ==================================================
    # Órdenes del CTF Exchange
    # ==================================================

    def order_typed_data(self, order: dict[str, Any], neg_risk: bool = False) -> dict[str, Any]:
        return {
            "types": {
                "EIP712Domain": [
                    {"name": "name", "type": "string"},
                    {"name": "version", "type": "string"},
                    {"name": "chainId", "type": "uint256"},
                    {"name": "verifyingContract", "type": "address"},
                ],
                "Order": [
                    {"name": "salt", "type": "uint256"},
                    {"name": "maker", "type": "address"},
                    {"name": "signer", "type": "address"},
                    {"name": "taker", "type": "address"},
                    {"name": "tokenId", "type": "uint256"},
                    {"name": "makerAmount", "type": "uint256"},
                    {"name": "takerAmount", "type": "uint256"},
                    {"name": "expiration", "type": "uint256"},
                    {"name": "nonce", "type": "uint256"},
                    {"name": "feeRateBps", "type": "uint256"},
                    {"name": "side", "type": "uint8"},
                    {"name": "signatureType", "type": "uint8"},
                ],
            },
            "primaryType": "Order",
            "domain": {
                "name": "Polymarket CTF Exchange",
                "version": "1",
                "chainId": self.chain_id,
                "verifyingContract": NEG_RISK_CTF_EXCHANGE if neg_risk else CTF_EXCHANGE,
            },
            "message": order,
        }

    def build_signed_order(
        self,
        token_id: str,
        price: float,
        size: float,
        side: str,
        fee_rate_bps: int = 0,
        nonce: int = 0,
        expiration: int = 0,
        neg_risk: bool = False,
        salt: int | None = None,
        taker: str = "0x0000000000000000000000000000000000000000",
    ) -> dict[str, Any]:
        """
        Construye y firma una orden límite. NO la postea.

        BUY:  makerAmount = USDC entregado (price*size), takerAmount = shares.
        SELL: makerAmount = shares entregadas, takerAmount = USDC recibido.
        Montos en unidades de 6 decimales, truncados hacia abajo.
        """
        if not 0.0 < price < 1.0:
            raise ValueError(f"price fuera de rango (0,1): {price}")
        if size <= 0:
            raise ValueError(f"size debe ser positivo: {size}")
        side_norm = side.lower()
        if side_norm not in ("buy", "sell"):
            raise ValueError(f"side inválido: {side}")

        shares_units = int(_round_down(size) * USDC_DECIMALS)
        usdc_units = int(_round_down(_round_down(size) * price, 4) * USDC_DECIMALS)
        if side_norm == "buy":
            maker_amount, taker_amount, side_enum = usdc_units, shares_units, SIDE_BUY
        else:
            maker_amount, taker_amount, side_enum = shares_units, usdc_units, SIDE_SELL

        order = {
            "salt": salt if salt is not None else secrets.randbits(64),
            "maker": self.address,
            "signer": self.address,
            "taker": taker,
            "tokenId": int(token_id),
            "makerAmount": maker_amount,
            "takerAmount": taker_amount,
            "expiration": expiration,
            "nonce": nonce,
            "feeRateBps": fee_rate_bps,
            "side": side_enum,
            "signatureType": SIGNATURE_TYPE_EOA,
        }
        signable = encode_typed_data(full_message=self.order_typed_data(order, neg_risk))
        signed = self._account.sign_message(signable)
        sig = signed.signature.hex()

        # Payload en el formato que espera POST /order (montos como strings)
        return {
            "salt": order["salt"],
            "maker": order["maker"],
            "signer": order["signer"],
            "taker": order["taker"],
            "tokenId": str(order["tokenId"]),
            "makerAmount": str(maker_amount),
            "takerAmount": str(taker_amount),
            "expiration": str(expiration),
            "nonce": str(nonce),
            "feeRateBps": str(fee_rate_bps),
            "side": "BUY" if side_enum == SIDE_BUY else "SELL",
            "signatureType": SIGNATURE_TYPE_EOA,
            "signature": sig if sig.startswith("0x") else f"0x{sig}",
        }

    def order_struct_hash(self, order: dict[str, Any], neg_risk: bool = False) -> str:
        """Hash EIP-712 completo de la orden (para el vector fijo de tests)."""
        signable = encode_typed_data(full_message=self.order_typed_data(order, neg_risk))
        from eth_account.messages import _hash_eip191_message

        h = _hash_eip191_message(signable).hex()
        return h if h.startswith("0x") else f"0x{h}"
