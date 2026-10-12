"""Encrypted local secret store.

File: data/vault.json (mode 0600). Contents are encrypted with AES-256-GCM using
a key derived from a master password with scrypt. The password is never
written anywhere; it is asked for interactively each time the program starts.
The KDF parameters and format header are authenticated as associated data, so
tampering with them makes decryption fail.

Limits worth knowing: Python cannot reliably wipe strings from memory, so a
running process holds decrypted secrets in RAM, and malware running as your
user can read them. The vault protects the secrets at rest (disk, backups,
cloud sync, a stolen laptop), not against a compromised machine.
"""
from __future__ import annotations

import base64
import json
import os
import re
import secrets as pysecrets
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

FORMAT = "arbdesk-vault"
VERSION = 1
# ~128 MiB, ~0.3-1 s per unlock on a laptop: slows offline password guessing.
DEFAULT_SCRYPT = {"n": 2 ** 17, "r": 8, "p": 1}
MIN_PASSWORD_LEN = 12
NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")


class VaultError(Exception):
    pass


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def _unb64(s: str) -> bytes:
    return base64.b64decode(s.encode())


def _derive(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return Scrypt(salt=salt, length=32, n=n, r=r, p=p).derive(password.encode("utf-8"))


def _aad(header: dict) -> bytes:
    return json.dumps(header, sort_keys=True, separators=(",", ":")).encode()


class Vault:
    def __init__(self, path: Path, scrypt_params: dict | None = None):
        self.path = Path(path)
        self.scrypt_params = scrypt_params or DEFAULT_SCRYPT

    def exists(self) -> bool:
        return self.path.exists()

    # ------------------------------------------------------------------
    def encrypt(self, password: str, data: dict[str, str]) -> dict:
        salt, nonce = os.urandom(16), os.urandom(12)
        header = {"format": FORMAT, "v": VERSION, "cipher": "AES-256-GCM",
                  "kdf": {"name": "scrypt", "salt": _b64(salt), **self.scrypt_params}}
        key = _derive(password, salt, **self.scrypt_params)
        ct = AESGCM(key).encrypt(nonce, json.dumps(data).encode(), _aad(header))
        return {**header, "nonce": _b64(nonce), "ct": _b64(ct)}

    @staticmethod
    def decrypt(password: str, blob: dict) -> dict[str, str]:
        if blob.get("format") != FORMAT or blob.get("v") != VERSION:
            raise VaultError("not an arbdesk vault, or unsupported version")
        kdf = blob["kdf"]
        if kdf.get("name") != "scrypt" or kdf["n"] < 2 ** 14:
            raise VaultError("unsupported or weakened KDF parameters")
        header = {k: blob[k] for k in ("format", "v", "cipher", "kdf")}
        key = _derive(password, _unb64(kdf["salt"]), kdf["n"], kdf["r"], kdf["p"])
        try:
            plain = AESGCM(key).decrypt(_unb64(blob["nonce"]), _unb64(blob["ct"]), _aad(header))
        except InvalidTag:
            raise VaultError("wrong password or vault file was modified") from None
        return json.loads(plain)

    # ------------------------------------------------------------------
    def create(self, password: str) -> None:
        if self.exists():
            raise VaultError(f"{self.path} already exists")
        if len(password) < MIN_PASSWORD_LEN:
            raise VaultError(f"master password must be at least {MIN_PASSWORD_LEN} characters")
        self._write(password, {})

    def unlock(self, password: str) -> dict[str, str]:
        if not self.exists():
            raise VaultError(f"{self.path} not found; run: python -m arbdesk vault init")
        return self.decrypt(password, json.loads(self.path.read_text()))

    def save(self, password: str, data: dict[str, str]) -> None:
        self.unlock(password)  # refuse to overwrite with a different password
        self._write(password, data)

    def set(self, password: str, name: str, value: str) -> None:
        if not NAME_RE.match(name):
            raise VaultError("secret names must be UPPER_SNAKE_CASE")
        data = self.unlock(password)
        data[name] = value
        self._write(password, data)

    def delete(self, password: str, name: str) -> bool:
        data = self.unlock(password)
        found = data.pop(name, None) is not None
        self._write(password, data)
        return found

    def change_password(self, old: str, new: str) -> None:
        if len(new) < MIN_PASSWORD_LEN:
            raise VaultError(f"master password must be at least {MIN_PASSWORD_LEN} characters")
        self._write(new, self.unlock(old))

    def _write(self, password: str, data: dict[str, str]) -> None:
        blob = self.encrypt(password, data)
        if self.decrypt(password, blob) != data:  # paranoia: never replace with an unreadable file
            raise VaultError("self-check failed; vault not written")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f".{self.path.name}.{pysecrets.token_hex(4)}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(blob, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        finally:
            if tmp.exists():
                tmp.unlink()
        if os.name == "posix":
            os.chmod(self.path, 0o600)
