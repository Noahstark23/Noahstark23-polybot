"""
Tests de la firma EIP-712 (gate F1: firma validada contra vector fijo).

El vector fijo se generó con la key de test estándar (0x...01), cuya dirección
pública es conocida (0x7E5F...5Bdf) — eso valida la derivación de address.
Además cada firma se verifica recuperando el signer con ecrecover, que es la
misma validación que hace el contrato del exchange.
"""
from __future__ import annotations

import pytest
from eth_account import Account
from eth_account.messages import encode_typed_data

from src.auth.eip712_signer import (
    CTF_EXCHANGE,
    NEG_RISK_CTF_EXCHANGE,
    Eip712Signer,
)

TEST_KEY = "0x0000000000000000000000000000000000000000000000000000000000000001"
TEST_ADDRESS = "0x7E5F4552091A69125d5DfCb7b8C2659029395Bdf"  # conocida para key=1

# --- Vector fijo (pineado; regenerar sólo si cambia el esquema EIP-712) ---
VECTOR_AUTH_SIG = (
    "0xb091cdd346fe092636d3c3241854a5a32fc4017671a2fdf4b4636180659cbfa8"
    "69016396be0366867109d74a036d12068c1bd12b53243f7e56f4879da762d3cf1c"
)
VECTOR_ORDER_SIG = (
    "0x986ab2a1c3a778382b74a5f23d5c598b1defbd55a733fc4a8488c6fd58a28a53"
    "0113afe89453b95d026468b2e3ded947842171d719fbe324786ddc331f3aa04c1b"
)
VECTOR_ORDER_HASH = "0x74901f4b54602bd49fd478c9afece109bb589558483acc6a31dc381a4a96245f"
VECTOR_SALT = 479249096354


@pytest.fixture()
def signer() -> Eip712Signer:
    return Eip712Signer(TEST_KEY, chain_id=137)


class TestDerivacion:
    def test_address_conocida(self, signer):
        assert signer.address == TEST_ADDRESS


class TestClobAuth:
    def test_vector_fijo(self, signer):
        sig = signer.sign_clob_auth(timestamp="1700000000", nonce=0)
        assert sig == VECTOR_AUTH_SIG

    def test_firma_recuperable(self, signer):
        """ecrecover devuelve la address del signer (lo que valida el server)."""
        typed = signer.clob_auth_typed_data(timestamp="1700000000", nonce=0)
        sig = signer.sign_clob_auth(timestamp="1700000000", nonce=0)
        recovered = Account.recover_message(encode_typed_data(full_message=typed), signature=sig)
        assert recovered == TEST_ADDRESS

    def test_cambia_con_timestamp(self, signer):
        assert signer.sign_clob_auth("1700000000") != signer.sign_clob_auth("1700000001")


class TestOrdenes:
    def _order(self, signer, **kw):
        defaults = {
            "token_id": "123456789",
            "price": 0.55,
            "size": 100.0,
            "side": "buy",
            "fee_rate_bps": 0,
            "nonce": 0,
            "expiration": 0,
            "salt": VECTOR_SALT,
        }
        defaults.update(kw)
        return signer.build_signed_order(**defaults)

    def test_vector_fijo_buy(self, signer):
        order = self._order(signer)
        assert order["signature"] == VECTOR_ORDER_SIG
        assert order["makerAmount"] == "55000000"  # 100 * 0.55 USDC (6 dec)
        assert order["takerAmount"] == "100000000"  # 100 shares (6 dec)
        assert order["side"] == "BUY"

    def test_hash_vector_fijo(self, signer):
        raw = {
            "salt": VECTOR_SALT,
            "maker": TEST_ADDRESS,
            "signer": TEST_ADDRESS,
            "taker": "0x0000000000000000000000000000000000000000",
            "tokenId": 123456789,
            "makerAmount": 55000000,
            "takerAmount": 100000000,
            "expiration": 0,
            "nonce": 0,
            "feeRateBps": 0,
            "side": 0,
            "signatureType": 0,
        }
        assert signer.order_struct_hash(raw) == VECTOR_ORDER_HASH

    def test_firma_recuperable(self, signer):
        order = self._order(signer)
        raw = {
            "salt": order["salt"],
            "maker": order["maker"],
            "signer": order["signer"],
            "taker": order["taker"],
            "tokenId": int(order["tokenId"]),
            "makerAmount": int(order["makerAmount"]),
            "takerAmount": int(order["takerAmount"]),
            "expiration": int(order["expiration"]),
            "nonce": int(order["nonce"]),
            "feeRateBps": int(order["feeRateBps"]),
            "side": 0,
            "signatureType": 0,
        }
        typed = signer.order_typed_data(raw)
        recovered = Account.recover_message(
            encode_typed_data(full_message=typed), signature=order["signature"]
        )
        assert recovered == TEST_ADDRESS

    def test_sell_invierte_montos(self, signer):
        order = self._order(signer, side="sell")
        assert order["makerAmount"] == "100000000"  # entrega shares
        assert order["takerAmount"] == "55000000"  # recibe USDC
        assert order["side"] == "SELL"

    def test_neg_risk_usa_otro_contrato(self, signer):
        normal = self._order(signer)
        neg = self._order(signer, neg_risk=True)
        assert normal["signature"] != neg["signature"]
        typed = signer.order_typed_data({}, neg_risk=True)
        assert typed["domain"]["verifyingContract"] == NEG_RISK_CTF_EXCHANGE
        assert signer.order_typed_data({})["domain"]["verifyingContract"] == CTF_EXCHANGE

    def test_salt_aleatorio_por_default(self, signer):
        a = self._order(signer, salt=None)
        b = self._order(signer, salt=None)
        assert a["salt"] != b["salt"]

    def test_precio_fuera_de_rango(self, signer):
        with pytest.raises(ValueError):
            self._order(signer, price=1.0)
        with pytest.raises(ValueError):
            self._order(signer, price=0.0)

    def test_size_invalido(self, signer):
        with pytest.raises(ValueError):
            self._order(signer, size=0)

    def test_side_invalido(self, signer):
        with pytest.raises(ValueError):
            self._order(signer, side="hold")


class TestCargaDeKey:
    def test_from_settings_exige_path(self, isolated_env):
        from src.utils.config import Settings

        with pytest.raises(RuntimeError, match="POLY_SIGNER_KEY_PATH"):
            Eip712Signer.from_settings(Settings(_env_file=None))

    def test_carga_hex_crudo(self, tmp_path, isolated_env):
        from src.utils.config import Settings

        key_file = tmp_path / "key"
        key_file.write_text(TEST_KEY)
        s = Settings(_env_file=None, POLY_SIGNER_KEY_PATH=key_file)
        assert Eip712Signer.from_settings(s).address == TEST_ADDRESS

    def test_valida_address_configurada(self, tmp_path, isolated_env):
        from src.utils.config import Settings

        key_file = tmp_path / "key"
        key_file.write_text(TEST_KEY)
        s = Settings(
            _env_file=None,
            POLY_SIGNER_KEY_PATH=key_file,
            POLY_WALLET_ADDRESS="0x" + "2" * 40,  # NO coincide
        )
        with pytest.raises(RuntimeError, match="no coincide"):
            Eip712Signer.from_settings(s)

    def test_keystore_cifrado_exige_password(self, tmp_path, isolated_env):
        from src.utils.config import Settings

        key_file = tmp_path / "key.json"
        key_file.write_text('{"crypto": {}}')
        s = Settings(_env_file=None, POLY_SIGNER_KEY_PATH=key_file)
        with pytest.raises(RuntimeError, match="POLY_KEYSTORE_PASSWORD"):
            Eip712Signer.from_settings(s)
