"""JSON 快照持久化。

服务把全部聚合以集合形式交给仓储，落盘为 JSON；重启后原样还原。
任务时限以绝对时间（datetime）存储，因此停机时间计入整改时限，
恢复后由服务重新扫描逾期任务。
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

from . import models

# 集合名 -> 元素模型类
COLLECTIONS: dict[str, type] = {
    "orgs": models.Org,
    "users": models.User,
    "identities": models.PersonaIdentity,
    "assets": models.Asset,
    "products": models.Product,
    "scripts": models.ScriptVersion,
    "cases": models.Case,
    "decisions": models.ReviewDecision,
    "placements": models.Placement,
    "tasks": models.EnforcementTask,
    "appeals": models.Appeal,
}


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)

    def save(self, data: dict[str, list[Any]]) -> None:
        payload = {key: [models.to_dict(item) for item in items] for key, items in data.items()}
        payload["_meta"] = {"format": 1}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=self.path.parent, delete=False
        ) as tmp:
            json.dump(payload, tmp, ensure_ascii=False, indent=2)
            tmp_path = Path(tmp.name)
        tmp_path.replace(self.path)  # 原子替换，避免写到一半崩溃损坏快照

    def load(self) -> dict[str, list[Any]]:
        if not self.path.exists():
            return {key: [] for key in COLLECTIONS}
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        result: dict[str, list[Any]] = {}
        for key, cls in COLLECTIONS.items():
            result[key] = [models.from_dict(cls, item) for item in payload.get(key, [])]
        return result
