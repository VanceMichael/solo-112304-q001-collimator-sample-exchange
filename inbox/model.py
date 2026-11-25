"""来源资料的模型、解析与标识。"""
from __future__ import annotations

import csv
import hashlib
import io
import json
from dataclasses import dataclass
from datetime import datetime


def parse_time(text: str) -> datetime:
    """来源时间为带时区 ISO 时间。"""
    return datetime.fromisoformat(text)


def content_digest(text: str) -> str:
    """原件内容指纹：识别同一来源键内容是否变化。"""
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()


def canonical(payload) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def request_id(order_id: str, operation: str, items, details) -> str:
    """导出请求号由内容决定。

    同一导出请求重复运行时内容不变则沿用同一请求号（合作端返回原回执）；
    资料版本一旦变化即换新号，不同版本的资料不会共用一个回执号。
    """
    payload = {"order": order_id, "operation": operation,
               "items": list(items), "details": details}
    return f"{order_id}:{operation}:{content_digest(canonical(payload))[:10]}"


def _number(text: str):
    try:
        return int(text)
    except (TypeError, ValueError):
        pass
    try:
        return float(text)
    except (TypeError, ValueError):
        return text


@dataclass(frozen=True)
class Reading:
    position: int
    pitch_um: object
    diagnostic: str  # 检具内部诊断字段，留在质检侧，不随资料外发


def parse_readings(text: str) -> list[Reading]:
    """解析检具原件 CSV（position,pitch_um,diagnostic）。"""
    readings = []
    for row in csv.DictReader(io.StringIO(text)):
        readings.append(Reading(position=int(row["position"]),
                                pitch_um=_number(row["pitch_um"]),
                                diagnostic=(row.get("diagnostic") or "")))
    return readings


@dataclass(frozen=True)
class Part:
    """检具索引中的一个分块声明。"""
    key: str
    href: str
    round: str
    slot: str
    axis: str
    basis: str
    until: str
    copies: tuple = ()

    @classmethod
    def from_dict(cls, data) -> "Part":
        return cls(key=data["key"], href=data["href"], round=data["round"],
                   slot=data["slot"], axis=data["axis"], basis=data["basis"],
                   until=data["until"], copies=tuple(data.get("copies", [])))

    def fetchable_at(self, now: datetime) -> bool:
        """可访问期限右端不包含。"""
        return now < parse_time(self.until)


@dataclass(frozen=True)
class Basis:
    id: str
    axes: tuple
    status: str
    supersedes: str | None = None
    applies: tuple = ()

    @classmethod
    def from_dict(cls, data) -> "Basis":
        return cls(id=data["id"], axes=tuple(data.get("axes", [])),
                   status=data.get("status", ""), supersedes=data.get("supersedes"),
                   applies=tuple(data.get("applies", [])))

    @property
    def issued(self) -> bool:
        return self.status == "issued"


@dataclass(frozen=True)
class OrderLine:
    slot: str
    round: str
    axis: str
    position: int
    buyer: str  # 买方部件位号


@dataclass(frozen=True)
class Order:
    id: str
    deadline: str
    priority: int
    lines: tuple

    @classmethod
    def from_dict(cls, data) -> "Order":
        lines = tuple(OrderLine(slot=s, round=r, axis=a, position=int(p), buyer=b)
                      for s, r, a, p, b in data.get("lines", []))
        return cls(id=data["id"], deadline=data["deadline"],
                   priority=int(data.get("priority", 0)), lines=lines)

    def urgency(self):
        """deadline 相同时按 priority（数值越小越优先），再按订单号稳定次序。"""
        return (parse_time(self.deadline), self.priority, self.id)
