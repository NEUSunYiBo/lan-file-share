"""挂载注册表与路径安全（防目录穿越）。"""

import os
import uuid
from pathlib import Path


class PathEscapeError(Exception):
    """相对路径越出了挂载根目录。"""


class MountRegistry:
    """内存态挂载表：id → {id, name, path}。"""

    def __init__(self, mounts=None):
        self._mounts = {}
        for m in mounts or []:
            entry = dict(m)
            entry.pop("system", None)  # 系统挂载不来自配置，防手改注入
            if not entry.get("id"):
                entry["id"] = uuid.uuid4().hex[:12]
            self._mounts[entry["id"]] = entry

    def add(self, path, name=None):
        """添加一个挂载（文件夹或文件），返回新挂载条目；路径不存在抛 FileNotFoundError。"""
        path = os.path.abspath(path)
        if not os.path.exists(path):
            raise FileNotFoundError(path)
        entry = {
            "id": uuid.uuid4().hex[:12],
            "name": name or (os.path.basename(path.rstrip("\\/")) or path),
            "path": path,
        }
        self._mounts[entry["id"]] = entry
        return entry

    def remove(self, mount_id):
        """移除挂载；不存在或为系统挂载时返回 False。"""
        entry = self._mounts.get(mount_id)
        if entry is None or entry.get("system"):
            return False
        del self._mounts[mount_id]
        return True

    def set_hidden(self, mount_id, hidden):
        """暂时隐藏 / 恢复挂载（用户端不可见，管理端仍显示并标注）。
        hidden=False 时删掉字段，保持 config 干净。"""
        entry = self._mounts.get(mount_id)
        if entry is None or entry.get("system"):
            return False
        if hidden:
            entry["hidden"] = True
        else:
            entry.pop("hidden", None)
        return True

    def get(self, mount_id):
        return self._mounts.get(mount_id)

    def list(self, managed_only=False):
        """挂载列表；系统挂载（上传文件夹）固定置顶，不参与正常排序。
        managed_only=True 时过滤系统挂载（保存配置用）。"""
        mounts = list(self._mounts.values())
        if managed_only:
            return [m for m in mounts if not m.get("system")]
        mounts.sort(key=lambda m: 0 if m.get("system") else 1)  # 稳定排序，普通挂载保持原序
        return mounts

    def add_system(self, mount_id, path, name):
        """添加系统挂载（上传目录）：不持久化，列表中固定置顶。"""
        entry = {
            "id": str(mount_id),
            "name": name,
            "path": os.path.abspath(path),
            "system": True,
        }
        self._mounts[entry["id"]] = entry
        return entry

    def remove_system(self, mount_id):
        """移除系统挂载；不存在或不是系统挂载时返回 False。"""
        entry = self._mounts.get(mount_id)
        if not entry or not entry.get("system"):
            return False
        del self._mounts[mount_id]
        return True

    # ── 排除规则（取消共享挂载点内部的子项） ──

    @staticmethod
    def _norm_rel(rel_path):
        """相对路径归一：统一 / 分隔、去首尾空白与斜杠。"""
        return str(rel_path or "").replace("\\", "/").strip().strip("/")

    def exclude(self, mount_id, rel_path):
        """把挂载点内的某个子项加入排除（对访问者隐藏）。"""
        mount = self._mounts.get(mount_id)
        if not mount:
            raise KeyError(mount_id)
        rel = self._norm_rel(rel_path)
        if not rel:
            raise ValueError("不能排除挂载点根目录（请直接移除整个挂载）")
        excluded = mount.setdefault("excluded", [])
        if rel not in excluded:
            excluded.append(rel)

    def restore(self, mount_id, rel_path):
        """移除一条排除规则；规则不存在返回 False。"""
        mount = self._mounts.get(mount_id)
        if not mount:
            return False
        rel = self._norm_rel(rel_path)
        excluded = mount.get("excluded", [])
        if rel in excluded:
            excluded.remove(rel)
            if not excluded:
                del mount["excluded"]  # 空列表不留字段，保持 config 干净
            return True
        return False

    def is_excluded(self, mount_id, rel_path):
        """rel_path 或其任一祖先被排除时返回 True（根路径永不排除）。"""
        mount = self._mounts.get(mount_id)
        if not mount:
            return False
        rel = self._norm_rel(rel_path)
        if not rel:
            return False
        excluded = mount.get("excluded", [])
        if not excluded:
            return False
        # 祖先匹配：逐级向上找，rel 为 a/b/c 时检查 a/b/c、a/b、a
        parts = rel.split("/")
        for i in range(len(parts), 0, -1):
            if "/".join(parts[:i]) in excluded:
                return True
        return False

    @staticmethod
    def safe_join(root, rel_path=""):
        """把相对路径拼到挂载根目录内并防穿越，返回真实绝对路径。

        - rel_path 为空时返回根目录本身
        - 含 .. 越界 / 绝对路径 / 符号链接指向外部时抛 PathEscapeError
        """
        root_real = Path(os.path.realpath(root))
        target = Path(os.path.realpath(root_real / rel_path))
        try:
            target.relative_to(root_real)
        except ValueError:
            raise PathEscapeError(rel_path) from None
        return str(target)
