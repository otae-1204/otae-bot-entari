from __future__ import annotations

import base64
import os
from dataclasses import dataclass

from Crypto.Cipher import AES


KEY_ENV_NAME = "ENDFIELD_CREDENTIAL_KEY"


class CredentialKeyError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class EncryptedCredential:
    nonce: bytes
    ciphertext: bytes
    tag: bytes


class CredentialCipher:
    def __init__(self, key: bytes):
        if len(key) != 32:
            raise CredentialKeyError("ENDFIELD_CREDENTIAL_KEY 必须是 Base64 编码的 32 字节密钥")
        self._key = bytes(key)

    @classmethod
    def from_env(cls) -> "CredentialCipher":
        value = os.getenv(KEY_ENV_NAME, "").strip()
        if not value:
            raise CredentialKeyError("未配置 ENDFIELD_CREDENTIAL_KEY，终末地账号绑定已禁用")
        try:
            key = base64.b64decode(value, validate=True)
        except (ValueError, TypeError) as exc:
            raise CredentialKeyError("ENDFIELD_CREDENTIAL_KEY 不是有效的 Base64") from exc
        return cls(key)

    def encrypt(self, plaintext: str, *, associated_data: bytes = b"endfield-account-token-v1") -> EncryptedCredential:
        cipher = AES.new(self._key, AES.MODE_GCM, nonce=os.urandom(12), mac_len=16)
        cipher.update(associated_data)
        ciphertext, tag = cipher.encrypt_and_digest(plaintext.encode("utf-8"))
        return EncryptedCredential(cipher.nonce, ciphertext, tag)

    def decrypt(
        self,
        encrypted: EncryptedCredential,
        *,
        associated_data: bytes = b"endfield-account-token-v1",
    ) -> str:
        try:
            cipher = AES.new(self._key, AES.MODE_GCM, nonce=encrypted.nonce, mac_len=16)
            cipher.update(associated_data)
            plaintext = cipher.decrypt_and_verify(encrypted.ciphertext, encrypted.tag)
        except (ValueError, KeyError) as exc:
            raise CredentialKeyError("终末地账号凭据解密失败，请检查 ENDFIELD_CREDENTIAL_KEY") from exc
        return plaintext.decode("utf-8")
