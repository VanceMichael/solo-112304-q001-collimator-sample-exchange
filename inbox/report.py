"""按交样单的质检视图：已取、来不及取而需重测、可以签收、仍需补件。"""
from __future__ import annotations

from .buyer import last_export
from .model import Basis, Order, Part, parse_time
from .review import BASIS_AXIS, BASIS_MISSING, BASIS_SUPERSEDED, build_review

OP_LABEL = {"submit": "提交", "supplement": "补件", "amend": "更正"}
ROW_LABEL = {"accepted": "签收", "rejected": "退回"}


def _history(exports, buyer_id: str) -> str:
    """该买方位号的回执履历（含已作废旧回执及当时依据）。"""
    parts = []
    for rec in exports:
        if buyer_id not in rec["items"]:
            continue
        receipt = rec.get("receipt") or {}
        number = receipt.get("receipt", "待回执")
        label = OP_LABEL.get(rec["operation"], rec["operation"])
        row = None
        for entry in receipt.get("rows", []):
            if entry["item"] == buyer_id:
                row = entry["status"]
        note = ROW_LABEL.get(row, "待签收" if row is None else row)
        if rec["details"].get(buyer_id, {}).get("state") == "withdrawn":
            note += "（已作废）"
        parts.append(f"{number} {label}{note}")
    return "；".join(parts)


def _bucket(review):
    """归位到四个查看口径，并给出追溯说明。"""
    notes = []
    if not review.met and review.unrecoverable is not None:
        bucket = "来不及取而需重测"
        notes.append(review.unrecoverable)
    elif review.signable:
        bucket = "可以签收"
    elif review.held is not None:
        bucket = "已取"
        if review.basis[0] == BASIS_MISSING:
            notes.append(f"基准 {review.held[0]['basis']} 未入库，暂不可签收")
        elif review.basis[0] == BASIS_AXIS:
            notes.append(f"基准 {review.held[0]['basis']} 未覆盖该坐标系，暂不可签收")
    else:
        bucket = "仍需补件"
        if review.candidates:
            part = review.candidates[0]
            notes.append(f"待取 {part.key}（期限 {part.until}）")
    if review.held is not None:
        original, version = review.held
        notes.append(f"原件 {original['key']}（{version['wave']} 波次取回，"
                     f"基准 {original['basis']}）")
        if review.basis[0] == BASIS_SUPERSEDED:
            notes.append(f"基准已被 {review.basis[1]} 追溯取代")
    return bucket, notes


def render(store) -> str:
    synced = store.data.get("synced")
    if not synced:
        return "尚未同步来源。"
    cfg = synced["config"]
    reviews = build_review(
        orders=[Order.from_dict(o) for o in synced["orders"]],
        parts=[Part.from_dict(p) for p in synced["parts"]],
        originals=store.data["originals"],
        validity=synced.get("validity", {}),
        bases=[Basis.from_dict(b) for b in synced["bases"]],
        now=parse_time(cfg["now"]),
        gone=set(store.data.get("gone", [])),
    )
    quota = store.data.get("quota", {})
    lines = [f"波次 {cfg['revision']}（业务时间 {cfg['now']}）：导出配额已用 "
             f"{quota.get('used', 0)}/{cfg['capacity']}，滚存截止 {cfg['rollover']}"]
    exports = store.data["exports"]
    orders = sorted((Order.from_dict(o) for o in synced["orders"]),
                    key=lambda o: o.urgency())
    for order in orders:
        lines.append("")
        lines.append(f"交样单 {order.id}（截止 {order.deadline}，优先级 {order.priority}）")
        for line in order.lines:
            review = reviews[(line.slot, line.round, line.axis)]
            bucket, notes = _bucket(review)
            history = _history(exports, line.buyer)
            if history:
                notes.append(f"回执 {history}")
            detail = f" —— {'；'.join(notes)}" if notes else ""
            lines.append(f"  {line.buyer}（槽{line.slot} 轮次{line.round} "
                         f"{line.axis} 位{line.position}）：{bucket}{detail}")
    if store.data["remeasure"]:
        lines.append("")
        lines.append("待重测：")
        for entry in store.data["remeasure"]:
            slot, round_, axis = entry["key"]
            lines.append(f"  槽{slot} 轮次{round_} {axis}：{entry['reason']}"
                         f"（涉及 {', '.join(entry['positions'])}）")
    return "\n".join(lines)
