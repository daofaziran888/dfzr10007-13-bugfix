"""分析模型沉淀业务规则：状态流转、字段校验与筛选口径都收在这里。

列表、保存和动作不再各写一遍校验：字段口径用 normalize_entry 统一补齐，
必填校验用 validate_required，筛选口径用 matches_filters，状态推进用 TRANSITIONS，
三处入口读取同一份判定结果。
"""
from __future__ import annotations

import threading
from typing import Any

from app.store import store

MODULE = "model"

# 列表页与详情页共用的字段口径：编号/名称/类型必填，其余可选，模型状态由状态机回写。
REQUIRED_FIELDS = ["模型编号", "模型名称", "模型类型"]
OPTIONAL_FIELDS = ["训练数据", "评估指标", "模型版本", "部署路径"]
STATUS_FIELD = "模型状态"
LIST_FIELDS = REQUIRED_FIELDS + OPTIONAL_FIELDS + [STATUS_FIELD]

# 模型状态只能按 草稿 → 训练完成 → 已部署 → 已下线 推进。
STATUS_ORDER = ["草稿", "训练完成", "已部署", "已下线"]

# 动作 → (前置状态, 目标状态)；目标为 None 表示归档后从注册清单移除。
TRANSITIONS: dict[str, tuple[str, str | None]] = {
    "部署模型": ("训练完成", "已部署"),
    "下线模型": ("已部署", "已下线"),
    "归档模型": ("已下线", None),
}
NEGATIVE_ACTIONS = ["下线模型"]

# 保存与状态推进共用同一把锁：两个请求同时部署同一模型版本时只保留一个部署结果。
_lock = threading.Lock()


def normalize_entry(row: dict[str, Any]) -> dict[str, Any]:
    """把记录补齐到统一结构并就地回写。

    历史记录缺少可选字段时补 None（页面显示为 —），模型状态始终以状态机为准，
    保证字段保存和状态推进读取同一结果。
    """
    status = str(row.get("status") or STATUS_ORDER[0])
    if status not in STATUS_ORDER:
        status = STATUS_ORDER[0]
    row["status"] = status
    for field in REQUIRED_FIELDS + OPTIONAL_FIELDS:
        row.setdefault(field, None)
    row[STATUS_FIELD] = status
    row["pending"] = status in STATUS_ORDER[:2]
    row["abnormal"] = bool(row.get("abnormal", False))
    return row


def validate_required(values: dict[str, Any]) -> list[str]:
    """登记校验：返回缺失的必填字段；调用方在校验通过前不得写入任何记录。"""
    return [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]


def matches_filters(
    row: dict[str, Any],
    *,
    keyword: str | None = None,
    status: str | None = None,
    field_filters: dict[str, str | None] | None = None,
) -> bool:
    """列表筛选口径：编号关键字、状态精确匹配与字段模糊匹配共用同一判定。"""
    if keyword and keyword not in str(row.get("模型编号") or ""):
        return False
    if status and row.get("status") != status:
        return False
    for field, value in (field_filters or {}).items():
        if value and str(value) not in str(row.get(field) or ""):
            return False
    return True


class ModelService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        field_filters: dict[str, str | None] | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = [normalize_entry(row) for row in store.rows(MODULE)]
        rows = [
            row
            for row in rows
            if matches_filters(row, keyword=keyword, status=status, field_filters=field_filters)
        ]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        row = store.find(MODULE, entry_id)
        return normalize_entry(row) if row is not None else None

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = validate_required(values)
        if missing:
            return None, missing
        with _lock:
            rows = store.rows(MODULE)
            entry: dict[str, Any] = {
                "id": max((int(row.get("id", 0)) for row in rows), default=0) + 1
            }
            for field in REQUIRED_FIELDS + OPTIONAL_FIELDS:
                raw = values.get(field)
                entry[field] = raw if raw is not None and str(raw).strip() != "" else None
            entry["status"] = STATUS_ORDER[0]
            entry["abnormal"] = False
            rows.append(entry)
        return normalize_entry(entry), []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        with _lock:
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"模型注册 {entry_id} 不存在或已归档"
            normalize_entry(entry)
            if action not in TRANSITIONS:
                return None, f"动作「{action}」不属于分析模型沉淀可执行范围"
            required, target = TRANSITIONS[action]
            current = str(entry["status"])
            if current != required:
                return None, (
                    f"模型注册当前状态为「{current}」，仅「{required}」状态可执行{action}，"
                    "越级或重复操作已拒绝"
                )
            if target is None:
                store.rows(MODULE).remove(entry)
                return normalize_entry(dict(entry)), f"模型注册已{action}"
            entry["status"] = target
            entry["abnormal"] = action in NEGATIVE_ACTIONS
            return normalize_entry(entry), f"模型注册已{action}"
