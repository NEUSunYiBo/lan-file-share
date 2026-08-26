"""auth 模块测试：密码哈希、token 生命周期。"""

import auth


def test_hash_and_verify_roundtrip():
    stored = auth.hash_password("我的密码123")
    assert auth.verify_password("我的密码123", stored)
    assert not auth.verify_password("wrong", stored)


def test_hash_unique_salts():
    assert auth.hash_password("same") != auth.hash_password("same")


def test_verify_rejects_malformed():
    assert not auth.verify_password("x", None)
    assert not auth.verify_password("x", "")
    assert not auth.verify_password("x", "no-dollar-sign")


def test_token_store_lifecycle():
    ts = auth.TokenStore()
    t1 = ts.issue()
    t2 = ts.issue()
    assert t1 != t2
    assert ts.check(t1) and ts.check(t2)
    assert not ts.check("")
    assert not ts.check("bogus")
    ts.revoke(t1)
    assert not ts.check(t1)
    assert ts.check(t2)
    ts.clear()
    assert not ts.check(t2)
