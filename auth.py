"""密码哈希校验与访问 token 管理（全内存，服务重启即失效）。"""

import hashlib
import secrets
import threading


def hash_password(password, salt=None):
    """生成 sha256(salt + password) 哈希，返回 "salt$hex" 格式。"""
    if salt is None:
        salt = secrets.token_hex(8)
    digest = hashlib.sha256((salt + password).encode("utf-8")).hexdigest()
    return f"{salt}${digest}"


def verify_password(password, stored):
    """校验密码是否与存储的 "salt$hex" 哈希匹配。"""
    if not stored or "$" not in stored:
        return False
    salt = stored.split("$", 1)[0]
    return secrets.compare_digest(hash_password(password, salt), stored)


class TokenStore:
    """内存 token 存储：签发 / 校验 / 注销（线程安全）。"""

    def __init__(self):
        self._tokens = set()
        self._lock = threading.Lock()

    def issue(self):
        """签发一个新 token。"""
        token = secrets.token_hex(16)
        with self._lock:
            self._tokens.add(token)
        return token

    def check(self, token):
        if not token:
            return False
        with self._lock:
            return token in self._tokens

    def revoke(self, token):
        with self._lock:
            self._tokens.discard(token)

    def clear(self):
        """清空全部 token（密码变更后调用）。"""
        with self._lock:
            self._tokens.clear()
