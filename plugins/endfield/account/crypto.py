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
            raise CredentialKeyError("ENDFIELD_CREDENTIAL_KEY 配置有误：需为 Base64 编码的 32 字节密钥。")
        self._key = bytes(key)

    @classmethod
    def from_env(cls) -> "CredentialCipher":
        value = os.getenv(KEY_ENV_NAME, "").strip()
        if not value:
            raise CredentialKeyError("未配置环境变量 ENDFIELD_CREDENTIAL_KEY，终末地账号绑定功能暂未开放。")
        try:
            key = base64.b64decode(value, validate=True)
        except (ValueError, TypeError) as exc:
            raise CredentialKeyError("ENDFIELD_CREDENTIAL_KEY 格式无效：非标准的 Base64 字符串。") from exc
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
            raise CredentialKeyError("终末地账号凭据解密失败，请检查环境变量 ENDFIELD_CREDENTIAL_KEY 设置。") from exc
        return plaintext.decode("utf-8")
