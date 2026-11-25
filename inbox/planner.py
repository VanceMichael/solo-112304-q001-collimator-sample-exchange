"""按工厂导出配额安排取件。"""
from __future__ import annotations


def plan_fetches(reviews, quota_left: int):
    """为未满足且仍可挽回的检测组挑选本次要取的原件。

    按订单紧迫度（硬交样期限、优先级）排序，区分各订单剩余缺项；
    每个检测组取可访问期限最近的一份，赶在测量块到期前补回缺项。
    不可恢复的缺口交给质检排定重测，不再占用取件额度。
    """
    plan = []
    for gkey, review in sorted(reviews.items(), key=lambda kv: (kv[1].urgency(), kv[0])):
        if review.met or review.unrecoverable is not None:
            continue
        if not review.candidates or quota_left <= 0:
            continue
        plan.append(review.candidates[0])
        quota_left -= 1
    return plan
