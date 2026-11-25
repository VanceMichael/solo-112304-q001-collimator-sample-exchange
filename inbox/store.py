"""质检侧本地状态：原件版本、导出配额、买方回执、重测单与审计事件。"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


def _empty():
    return {
        "version": 1,
        "synced": {},       # 最近一次来源快照：config/parts/bases/orders/validity
        "quota": {},        # {"revision": 波次, "used": 已用份数}
        "originals": {},    # 来源键 -> 原件记录（含全部已取版本与出处）
        "gone": [],         # 上游已覆盖（410）的来源键
        "exports": [],      # 导出请求与回执（含已作废旧回执，保留当时依据，不删除）
        "remeasure": [],    # 不可恢复、已交质检排定重测的检测组
        "events": [],       # 审计事件
    }


class Store:
    def __init__(self, path):
        self.path = Path(path)
        if self.path.exists():
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        else:
            self.data = _empty()

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(self.data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    def event(self, kind: str, message: str, **extra):
        entry = {"kind": kind, "message": message}
        entry.update(extra)
        self.data["events"].append(entry)
