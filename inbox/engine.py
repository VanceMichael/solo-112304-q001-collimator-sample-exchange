"""收件工具编排：同步来源、配额取件、买方导出、重测登记。"""
from __future__ import annotations

from . import buyer, planner, report
from .model import Basis, Order, Part, content_digest, parse_readings, parse_time
from .review import build_review
from .source import Gone, Source
from .store import Store


class InboxError(Exception):
    """收件工具运行异常。"""


def _normalize_validity(payload) -> dict:
    """检具有效性声明统一为 {来源键: 声明}。"""
    if not payload:
        return {}
    if isinstance(payload, list):
        records = payload
    elif "records" in payload:
        records = payload["records"]
    elif "key" in payload:
        records = [payload]
    else:
        records = []
    return {rec["key"]: rec for rec in records}


class Inbox:
    def __init__(self, origin: str, store_path, source=None):
        self.source = source if source is not None else Source(origin)
        self.store = Store(store_path)

    def _synced(self) -> dict:
        synced = self.store.data.get("synced")
        if not synced:
            raise InboxError("尚未同步来源，请先执行 sync（或 run）。")
        return synced

    # ---- 同步来源快照与配额波次 ----
    def sync(self):
        cfg = self.source.config()
        previous = self.store.data.get("synced") or {}
        synced = {
            "config": cfg,
            "parts": self.source.index()["parts"],
            "bases": self.source.bases()["records"],
            "orders": self.source.orders()["orders"],
            "validity": _normalize_validity(self.source.validity()),
        }
        quota = self.store.data["quota"]
        if quota.get("revision") != cfg["revision"]:
            self.store.data["quota"] = {"revision": cfg["revision"], "used": 0}
            self.store.event("quota", f"进入波次 {cfg['revision']}，"
                                      f"导出配额 {cfg['capacity']} 份")
        if previous and previous.get("bases") != synced["bases"]:
            self.store.event("bases", "基准记录已更新，重新评审持有读数与已外发项")
        if synced["validity"] and previous.get("validity") != synced["validity"]:
            self.store.event("validity", "检具更新了来源键有效性声明")
        self.store.data["synced"] = synced
        self.store.save()
        return synced

    # ---- 评审 ----
    def review(self):
        synced = self._synced()
        cfg = synced["config"]
        return build_review(
            orders=[Order.from_dict(o) for o in synced["orders"]],
            parts=[Part.from_dict(p) for p in synced["parts"]],
            originals=self.store.data["originals"],
            validity=synced.get("validity", {}),
            bases=[Basis.from_dict(b) for b in synced["bases"]],
            now=parse_time(cfg["now"]),
            gone=set(self.store.data.get("gone", [])),
        )

    # ---- 取件 ----
    def collect(self):
        synced = self._synced()
        cfg = synced["config"]
        now = parse_time(cfg["now"])
        wave = cfg["revision"]
        while True:
            reviews = self.review()
            self._update_remeasure(reviews, now)
            left = cfg["capacity"] - self.store.data["quota"].get("used", 0)
            plan = planner.plan_fetches(reviews, left)
            if not plan:
                break
            progressed = False
            for part in plan:
                if cfg["capacity"] - self.store.data["quota"].get("used", 0) <= 0:
                    break
                outcome = self._fetch_one(part, wave, now)
                if outcome in ("new", "conflict", "gone"):
                    progressed = True
            self.store.save()
            if not progressed:
                break
        self.store.save()

    def _fetch_one(self, part, wave, now) -> str:
        originals = self.store.data["originals"]
        try:
            text = self.source.get_raw(part.href)
        except Gone:
            self.store.data.setdefault("gone", []).append(part.key)
            self.store.event("gone",
                             f"{part.key} 上游已覆盖，改查其他会话是否留有同轮有效副本",
                             key=part.key)
            return "gone"
        quota = self.store.data["quota"]
        quota["used"] = quota.get("used", 0) + 1  # 每次导出均计一份配额
        digest = content_digest(text)
        rows = [dict(position=r.position, pitch_um=r.pitch_um, diagnostic=r.diagnostic)
                for r in parse_readings(text)]
        original = originals.get(part.key)
        if original and any(v["digest"] == digest for v in original["versions"]):
            self.store.event("retry", f"{part.key} 重试取到同一原件，沿用原始出处",
                             key=part.key)
            return "retry"
        version = {"digest": digest, "wave": wave,
                   "fetched_at": now.isoformat(), "rows": rows}
        if original is None:
            originals[part.key] = {
                "key": part.key, "href": part.href, "round": part.round,
                "slot": part.slot, "axis": part.axis, "basis": part.basis,
                "until": part.until, "copies": list(part.copies),
                "versions": [version], "contested": False,
            }
            self.store.event("fetch",
                             f"取回 {part.key}（轮次 {part.round}，基准 {part.basis}）",
                             key=part.key, wave=wave)
            return "new"
        original["versions"].append(version)
        original["contested"] = True
        self.store.event("conflict",
                         f"{part.key} 收到内容不同的原件，停止使用该键直至检具声明",
                         key=part.key)
        return "conflict"

    def _update_remeasure(self, reviews, now):
        current = {tuple(entry["key"]): entry for entry in self.store.data["remeasure"]}
        updated = []
        for gkey, review in reviews.items():
            if review.unrecoverable is None:
                continue
            if gkey in current:
                updated.append(current[gkey])
                continue
            entry = {"key": list(gkey), "reason": review.unrecoverable,
                     "since": now.isoformat(),
                     "positions": [line.buyer for _, line in review.lines]}
            updated.append(entry)
            self.store.event("remeasure",
                             f"检测组 {gkey} 不可恢复，交质检排定重测：{review.unrecoverable}")
        self.store.data["remeasure"] = updated

    # ---- 买方导出 ----
    def export(self):
        synced = self._synced()
        cfg = synced["config"]
        now = parse_time(cfg["now"])
        self._poll_pending()
        reviews = self.review()
        self._update_remeasure(reviews, now)
        actions = buyer.position_actions(reviews, self.store.data["exports"])
        for batch in buyer.build_requests(actions):
            self._submit(batch, cfg["revision"], now)
        self.store.save()

    def _submit(self, batch, wave, now):
        rid = batch["request"]
        if any(rec["request"] == rid for rec in self.store.data["exports"]):
            return  # 同一内容已有记录，沿用原回执
        existing = self.source.buyer_receipt(rid)
        if existing is not None:
            self._record(batch, existing, wave, now)
            return
        payload = {"request": rid, "operation": batch["operation"],
                   "items": batch["items"], "details": batch["details"]}
        status, body = self.source.buyer_submit(payload)
        if status == 200:
            self._record(batch, body, wave, now)
        elif status == 503:
            self._record(batch, None, wave, now)  # 回执暂失，下轮补查
            self.store.event("lost", f"请求 {rid} 暂未取得合作端回应")
        else:
            self.store.event("error", f"请求 {rid} 返回 {status}: {body}")

    def _record(self, batch, receipt, wave, now):
        self.store.data["exports"].append({
            "request": batch["request"], "operation": batch["operation"],
            "items": batch["items"], "details": batch["details"],
            "receipt": receipt, "wave": wave, "at": now.isoformat(),
        })
        if receipt:
            self.store.event("receipt",
                             f"请求 {batch['request']} 取得回执 {receipt['receipt']}")

    def _poll_pending(self):
        for rec in self.store.data["exports"]:
            if rec.get("receipt") is not None:
                continue
            receipt = self.source.buyer_receipt(rec["request"])
            if receipt is not None:
                rec["receipt"] = receipt
                self.store.event("receipt",
                                 f"补查到请求 {rec['request']} 回执 {receipt['receipt']}")

    # ---- 视图 ----
    def status(self) -> str:
        return report.render(self.store)

    def run(self):
        self.sync()
        self.collect()
        self.export()
        return self.status()
