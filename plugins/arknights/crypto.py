"""AES-GCM credential storage for the Arknights Skland binding.

The Arknights database is separate from the Endfield one, but operators may
already have provisioned a 32-byte key for that plugin.  ``ARKNIGHTS_CREDENTIAL_KEY``
is the preferred variable; ``ENDFIELD_CREDENTIAL_KEY`` is only read as a
fallback so a fresh deployment does not need a second secret.  This module never
generates or rewrites ``.env``.
"""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass

from Crypto.Cipher import AES

PRIMARY_KEY_ENV_NAME = "ARKNIGHTS_CREDENTIAL_KEY"
FALLBACK_KEY_ENV_NAME = "ENDFIELD_CREDENTIAL_KEY"
KEY_ENV_NAMES = (PRIMARY_KEY_ENV_NAME, FALLBACK_KEY_ENV_NAME)
ASSOCIATED_DATA = b"arknights-account-token-v1"


class CredentialKeyError(ValueError):
    """Raised when the credential key is missing, malformed, or wrong."""


@dataclass(frozen=True, slots=True)
class EncryptedCredential:
    nonce: bytes
    ciphertext: bytes
    tag: bytes


class ArknightsCipher:
    """Encrypt and decrypt one Arknights account token with AES-256-GCM."""

    def __init__(self, key: bytes):
        if len(key) != 32:
            raise CredentialKeyError(
                "明日方舟凭据密钥必须是 Base64 编码的 32 字节密钥（ARKNIGHTS_CREDENTIAL_KEY）。"
            )
        self._key = bytes(key)

    @classmethod
    def from_env(cls) -> ArknightsCipher:
        for name in KEY_ENV_NAMES:
            value = os.getenv(name, "").strip()
            if not value:
                continue
            try:
                key = base64.b64decode(value, validate=True)
            except (ValueError, TypeError) as exc:
                raise CredentialKeyError(f"{name} 不是有效的 Base64 密钥。") from exc
            if len(key) != 32:
                raise CredentialKeyError(f"{name} 解码后不是 32 字节密钥。")
            return cls(key)
        raise CredentialKeyError(
            "未配置 ARKNIGHTS_CREDENTIAL_KEY（可回退 ENDFIELD_CREDENTIAL_KEY），"
            "明日方舟账号绑定与签到已禁用。"
        )

    def encrypt(
        self,
        plaintext: str,
        *,
        associated_data: bytes = ASSOCIATED_DATA,
    ) -> EncryptedCredential:
        cipher = AES.new(self._key, AES.MODE_GCM, nonce=os.urandom(12), mac_len=16)
        cipher.update(associated_data)
        ciphertext, tag = cipher.encrypt_and_digest(str(plaintext).encode("utf-8"))
        return EncryptedCredential(cipher.nonce, ciphertext, tag)

    def decrypt(
        self,
        encrypted: EncryptedCredential,
        *,
        associated_data: bytes = ASSOCIATED_DATA,
    ) -> str:
        try:
            cipher = AES.new(self._key, AES.MODE_GCM, nonce=encrypted.nonce, mac_len=16)
            cipher.update(associated_data)
            plaintext = cipher.decrypt_and_verify(encrypted.ciphertext, encrypted.tag)
        except (ValueError, KeyError) as exc:
            raise CredentialKeyError(
                "明日方舟账号凭据解密失败，请检查 ARKNIGHTS_CREDENTIAL_KEY 是否与绑定时一致。"
            ) from exc
        return plaintext.decode("utf-8")
