"""单实例互斥体测试。"""

import ctypes

import pytest

import main


def test_second_call_exits(monkeypatch):
    """互斥体已存在且没有可通知的看门狗时，第二次调用应 SystemExit(0)。"""
    # 用独立名字，避免与真实运行中的实例互相干扰
    monkeypatch.setattr(main, "MUTEX_NAME", r"Local\LanShare.Test.Mutex.A")
    monkeypatch.setattr(main, "RELAUNCH_EVENT_NAME", r"Local\LanShare.Test.Event.A")
    main._ensure_single_instance()  # 首次创建，正常返回
    with pytest.raises(SystemExit) as ei:
        main._ensure_single_instance()  # 互斥体已存在，无事件可通知 → 兜底提示后退出
    assert ei.value.code == 0


def test_second_instance_signals_running_instance(monkeypatch):
    """首实例看门狗已创建事件时，第二实例应触发事件后退出（toast 由首实例弹）。"""
    monkeypatch.setattr(main, "MUTEX_NAME", r"Local\LanShare.Test.Mutex.B")
    monkeypatch.setattr(main, "RELAUNCH_EVENT_NAME", r"Local\LanShare.Test.Event.B")
    main._ensure_single_instance()  # 首次创建

    # 模拟首实例的看门狗：创建同名通知事件
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    h = kernel32.CreateEventW(None, False, False, main.RELAUNCH_EVENT_NAME)
    assert h

    with pytest.raises(SystemExit) as ei:
        main._ensure_single_instance()  # 第二次：应 SetEvent 后立即退出
    assert ei.value.code == 0

    # 事件应已被置位（WaitForSingleObject 立即返回 WAIT_OBJECT_0=0）
    assert kernel32.WaitForSingleObject(h, 0) == 0
    kernel32.CloseHandle(h)
