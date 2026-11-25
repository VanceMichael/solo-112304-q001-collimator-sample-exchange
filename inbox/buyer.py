"""买方资料的组装、提交动作判定与回执跟踪。

买方只见约定的读数和来源标识（来源键、轮次、槽位、坐标系、基准），
检具内部诊断字段留在质检侧。补齐与更正引用旧回执号。
"""
from __future__ import annotations

from .model import request_id

ACCEPTED = "accepted"
REJECTED = "rejected"


def reading_of(version, position: int):
    for row in version["rows"]:
        if row["position"] == position:
            return row["pitch_um"]
    return None


def version_tag(version) -> str:
    return version["digest"][:12]


def last_export(exports, buyer: str):
    """该买方位号最近一次导出的落点（含回执状态与当时依据）。"""
    last = None
    for rec in exports:
        if buyer not in rec["items"]:
            continue
        row = None
        receipt = rec.get("receipt") or {}
        for entry in receipt.get("rows", []):
            if entry["item"] == buyer:
                row = entry["status"]
        details = rec["details"].get(buyer, {})
        last = {"operation": rec["operation"],
                "row": row,
                "receipt": receipt.get("receipt"),
                "version": details.get("version"),
                "withdrawn": details.get("state") == "withdrawn"}
    return last


def position_actions(reviews, exports):
    """按检测位置计算应收的买方动作（submit/supplement/amend）。

    - 可签收且从未外发：提交；
    - 曾被买方退回或已作废，如今可签收：补件（引用旧回执号）；
    - 已签收但资料版本变化：更正；
    - 已签收但依据失效（基准被追溯取代、原件存疑等）：更正作废，保留旧回执。
    """
    actions = []
    for gkey, review in reviews.items():
        for order, line in review.lines:
            last = last_export(exports, line.buyer)
            if review.signable:
                original, version = review.held
                pitch = reading_of(version, line.position)
                if pitch is None:
                    continue  # 原件未覆盖该位置，留待核查
                details = {
                    "position": line.position,
                    "pitch_um": pitch,
                    "source": {"key": original["key"], "round": original["round"],
                               "slot": original["slot"], "axis": original["axis"],
                               "basis": original["basis"]},
                    "version": version_tag(version),
                }
                if last is None:
                    actions.append((order, "submit", line, details))
                elif last["withdrawn"] or last["row"] == REJECTED:
                    actions.append((order, "supplement", line,
                                    dict(details, ref=last["receipt"])))
                elif last["row"] == ACCEPTED and last["version"] != version_tag(version):
                    actions.append((order, "amend", line,
                                    dict(details, ref=last["receipt"])))
            else:
                if last and last["row"] == ACCEPTED and not last["withdrawn"]:
                    actions.append((order, "amend", line, {
                        "position": line.position,
                        "state": "withdrawn",
                        "reason": invalidate_reason(review),
                        "ref": last["receipt"],
                        "version": last["version"],
                    }))
    return actions


def invalidate_reason(review) -> str:
    if review.held is not None and review.basis[0] == "superseded":
        return (f"基准 {review.held[0]['basis']} 已被 {review.basis[1]} "
                f"追溯取代（轮次 {review.key[1]}）")
    if review.unrecoverable:
        return review.unrecoverable
    return "读数暂不可签收"


def build_requests(actions):
    """按（交样单、动作）组批，请求号由内容决定。"""
    batches = {}
    for order, operation, line, details in actions:
        batch = batches.setdefault((order.id, operation),
                                   {"order": order, "operation": operation,
                                    "items": [], "details": {}})
        batch["items"].append(line.buyer)
        batch["details"][line.buyer] = details
    requests = []
    for (order_id, operation), batch in sorted(
            batches.items(), key=lambda kv: (kv[1]["order"].urgency(), kv[0][1])):
        requests.append({"request": request_id(order_id, operation,
                                               batch["items"], batch["details"]),
                         "operation": operation, "order": order_id,
                         "items": batch["items"], "details": batch["details"]})
    return requests
