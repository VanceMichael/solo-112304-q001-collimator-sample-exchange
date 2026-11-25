"""检测组评审：读数归属、基准状态与可挽回性（纯逻辑，不触网）。"""
from __future__ import annotations

from dataclasses import dataclass

from .model import parse_time

# 基准判定结果
BASIS_OK = "ok"
BASIS_MISSING = "missing"        # 基准尚未入库（尚无定稿）
BASIS_AXIS = "axis"              # 基准未覆盖该坐标系（坐标系无跨系通用约定）
BASIS_SUPERSEDED = "superseded"  # 基准被修订追溯取代


def basis_state(basis_id: str, axis: str, round_: str, bases) -> tuple:
    """判定基准对某轮次、某坐标系是否可用。

    返回 (状态, 取代者基准号)。标为 supersedes 的修订与其 applies 列表
    共同限定影响轮次。
    """
    records = {b.id: b for b in bases}
    rec = records.get(basis_id)
    if rec is None or not rec.issued:
        return (BASIS_MISSING, None)
    if axis not in rec.axes:
        return (BASIS_AXIS, None)
    for sup in bases:
        if sup.issued and sup.supersedes == basis_id and round_ in sup.applies:
            return (BASIS_SUPERSEDED, sup.id)
    return (BASIS_OK, None)


def usable_version(original, validity):
    """原件当前可用版本。

    同一来源键收到内容不同的原件后，该键停止使用，直到检具的声明明确
    哪份有效；声明为存疑（无有效指纹）时该键不可用。
    """
    if not original:
        return None
    decl = validity.get(original["key"])
    versions = original.get("versions", [])
    if decl is not None:
        digest = decl.get("valid_digest")
        if decl.get("state") == "disputed" or not digest:
            return None
        return next((v for v in versions if v["digest"] == digest), None)
    if original.get("contested"):
        return None
    return versions[-1] if versions else None


def key_fetchable(part, originals, validity) -> bool:
    """该来源键现在是否还值得取。

    已持有可用版本不重复取（重试取到同一原件应沿用原始出处）；
    被声明存疑或内容冲突未决的键停止使用。
    """
    decl = validity.get(part.key)
    if decl is not None and (decl.get("state") == "disputed" or not decl.get("valid_digest")):
        return False
    original = originals.get(part.key)
    if original:
        if usable_version(original, validity) is not None:
            return False
        if original.get("contested") and decl is None:
            return False
    return True


@dataclass
class GroupReview:
    """一个检测组（槽位、轮次、坐标系）的评审结果。"""
    key: tuple
    lines: list                 # [(Order, OrderLine)]
    held: tuple | None          # (原件记录, 可用版本)
    basis: tuple                # basis_state 结果；未持有时为 (None, None)
    candidates: list            # 现在仍可取的 Part（按可访问期限升序）
    unrecoverable: str | None   # 不可恢复原因；None 表示仍可挽回

    @property
    def met(self) -> bool:
        """取件层面已满足：持有可用读数且基准未被追溯取代。"""
        return self.held is not None and self.basis[0] != BASIS_SUPERSEDED

    @property
    def signable(self) -> bool:
        """可以签收：持有可用读数且基准已入库、覆盖该坐标系、未被取代。"""
        return self.held is not None and self.basis[0] == BASIS_OK

    def urgency(self):
        return min(order.urgency() for order, _ in self.lines)


def _find_held(gkey, originals, validity):
    for original in originals.values():
        if (original["slot"], original["round"], original["axis"]) != gkey:
            continue
        version = usable_version(original, validity)
        if version is not None:
            return (original, version)
    return None


def build_review(orders, parts, originals, validity, bases, now, gone=()) -> dict:
    """对全部检测组给出评审。

    候选原件严格匹配（槽位、轮次、坐标系）：不能拿同槽别的轮次的读数填上。
    """
    groups = {}
    for order in orders:
        for line in order.lines:
            groups.setdefault((line.slot, line.round, line.axis), []).append((order, line))
    reviews = {}
    for gkey, lines in groups.items():
        slot, round_, axis = gkey
        held = _find_held(gkey, originals, validity)
        basis = basis_state(held[0]["basis"], axis, round_, bases) if held else (None, None)
        candidates = []
        for part in parts:
            if (part.slot, part.round, part.axis) != gkey:
                continue
            if part.key in gone or not part.fetchable_at(now):
                continue
            if basis_state(part.basis, axis, round_, bases)[0] == BASIS_SUPERSEDED:
                continue
            if not key_fetchable(part, originals, validity):
                continue
            candidates.append(part)
        candidates.sort(key=lambda p: parse_time(p.until))
        unrecoverable = None
        held_ok = held is not None and basis[0] != BASIS_SUPERSEDED
        if not held_ok:
            earliest = min(parse_time(order.deadline) for order, _ in lines)
            if not candidates:
                if held is not None and basis[0] == BASIS_SUPERSEDED:
                    unrecoverable = f"基准 {held[0]['basis']} 已被 {basis[1]} 追溯取代，且无有效副本"
                elif any((o["slot"], o["round"], o["axis"]) == gkey
                         for o in originals.values()):
                    unrecoverable = "原件内容存疑且检具未声明有效版本"
                else:
                    unrecoverable = "缓冲已覆盖或无可用原件，其他会话亦无有效副本"
            elif now >= earliest:
                unrecoverable = f"已过交样截止 {earliest.isoformat()}"
        reviews[gkey] = GroupReview(key=gkey, lines=lines, held=held, basis=basis,
                                    candidates=candidates, unrecoverable=unrecoverable)
    return reviews
