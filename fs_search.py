"""挂载点递归搜索：用户端 /api/search 与管理端 /admin/api/search 共用。

按文件名不区分大小写子串匹配，最多 500 条；遍历忽略无权限等错误，尽力返回能搜到的。
user_view=True 时跳过用户端不可见的挂载（未共享的上传目录 / 已隐藏的挂载），
管理端传 False：管理员能看到全部挂载，搜索结果与之一致。
"""

import os

MAX_RESULTS = 500


def search_mounts(state, q, share_id=None, user_view=False):
    """在挂载点内递归搜索文件名，返回 [{share, share_name, path, name, size, mtime, is_dir}]。

    state 为 server.AppState；share_id 非空时只搜该挂载（"本共享"）。
    排除项（取消共享的子路径）在两种视图下都跳过：用户端本就不可见，
    管理端在侧栏「已排除」区单独管理。
    """
    q = str(q or "").strip().lower()
    if not q:
        return []
    results = []
    for m in state.registry.list():
        if user_view:
            if m.get("system") and not state.uploads_shared():
                continue  # 上传文件夹未共享：用户端搜不到
            if m.get("hidden"):
                continue  # 暂时隐藏的挂载：用户端搜不到
        if share_id and m["id"] != share_id:
            continue
        root = m["path"]
        if os.path.isfile(root):
            # 单文件挂载：只匹配自身文件名
            if q in os.path.basename(root).lower():
                st = os.stat(root)
                results.append({
                    "share": m["id"], "share_name": m["name"],
                    "path": "", "name": os.path.basename(root),
                    "size": st.st_size, "mtime": int(st.st_mtime), "is_dir": False,
                })
            continue

        # 目录挂载：递归遍历（忽略无权限等错误，尽力返回能搜到的）
        for dirpath, dirnames, filenames in os.walk(root, onerror=lambda _e: None):
            # 排除项剪枝：跳过被排除的目录（不进入）与条目
            rel_dir = os.path.relpath(dirpath, root).replace("\\", "/")
            if rel_dir != "." and state.registry.is_excluded(m["id"], rel_dir):
                dirnames[:] = []
                continue
            if rel_dir == ".":
                dirnames[:] = [d for d in dirnames if not state.registry.is_excluded(m["id"], d)]
                filenames = [f for f in filenames if not state.registry.is_excluded(m["id"], f)]
            else:
                dirnames[:] = [d for d in dirnames
                               if not state.registry.is_excluded(m["id"], rel_dir + "/" + d)]
                filenames = [f for f in filenames
                             if not state.registry.is_excluded(m["id"], rel_dir + "/" + f)]
            for name in dirnames + filenames:
                if q not in name.lower():
                    continue
                real = os.path.join(dirpath, name)
                try:
                    st = os.stat(real)
                except OSError:
                    continue
                rel = os.path.relpath(real, root).replace("\\", "/")
                results.append({
                    "share": m["id"], "share_name": m["name"],
                    "path": rel, "name": name,
                    "size": 0 if os.path.isdir(real) else st.st_size,
                    "mtime": int(st.st_mtime), "is_dir": os.path.isdir(real),
                })
                if len(results) >= MAX_RESULTS:
                    return results
    return results
