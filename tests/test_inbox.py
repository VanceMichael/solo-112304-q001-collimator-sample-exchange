import json
import tempfile
import unittest
from pathlib import Path

from inbox.buyer import build_requests, position_actions
from inbox.engine import Inbox
from inbox.model import Basis, Order, Part, content_digest, parse_time, request_id
from inbox.review import BASIS_MISSING, BASIS_OK, BASIS_SUPERSEDED, basis_state, build_review
from inbox.source import Gone


def make_config(**kw):
    config = {"revision": "W1", "now": "2026-12-01T08:00:00+08:00",
              "capacity": 3, "rollover": "2026-12-01T08:07:00+08:00"}
    config.update(kw)
    return config


def make_part(key, round_="R1", slot="A", axis="face", basis="B7",
              until="2026-12-01T08:03:00+08:00", copies=()):
    return {"key": key, "href": f"/raw/{key.replace('/', '_')}.csv", "round": round_,
            "slot": slot, "axis": axis, "basis": basis, "until": until,
            "copies": list(copies)}


def make_order(oid="O1", deadline="2026-12-01T08:12:00+08:00", priority=1,
               lines=(("A", "R1", "face", 1, "A-01"),)):
    return {"id": oid, "deadline": deadline, "priority": priority,
            "lines": [list(line) for line in lines]}


CSV_A = "position,pitch_um,diagnostic\n1,42,diag-x\n2,43,diag-x\n"
CSV_B = "position,pitch_um,diagnostic\n1,41,diag-x\n2,43,diag-x\n"


class FakeSource:
    """不触网的来源替身，逐端点预置资料。"""

    def __init__(self, config=None, parts=(), bases=(), orders=(), validity=None,
                 raws=None, reject=(), lose_next=False):
        self._config = config or make_config()
        self._parts = list(parts)
        self._bases = list(bases)
        self._orders = list(orders)
        self._validity = validity
        self.raws = dict(raws or {})
        self.reject = set(reject)
        self.lose_next = lose_next
        self.fetched = []
        self.posted = []
        self.receipts = {}
        self._seq = 0

    def config(self):
        return self._config

    def index(self):
        return {"parts": self._parts}

    def bases(self):
        return {"records": self._bases}

    def orders(self):
        return {"orders": self._orders}

    def validity(self):
        return self._validity

    def get_raw(self, href):
        self.fetched.append(href)
        value = self.raws[href]
        if value == "gone":
            raise Gone(href)
        return value

    def buyer_receipt(self, rid):
        return self.receipts.get(rid)

    def buyer_submit(self, payload):
        self._seq += 1
        rows = [{"item": item,
                 "status": "rejected" if item in self.reject else "accepted"}
                for item in payload["items"]]
        receipt = {"request": payload["request"], "receipt": f"BUY-{self._seq:05d}",
                   "operation": payload["operation"], "rows": rows,
                   "details": payload["details"]}
        self.receipts[payload["request"]] = receipt
        self.posted.append(payload)
        if self.lose_next:
            self.lose_next = False
            return 503, {"message": "暂未取得合作端回应"}
        return 200, receipt


class InboxCase(unittest.TestCase):
    def make_inbox(self, source):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return Inbox("http://fake", str(Path(tmp.name) / "state.json"), source=source)


class CollectTests(InboxCase):
    def test_quota_follows_order_urgency_and_part_expiry(self):
        parts = [make_part("G9/q50/1", round_="R4", slot="C", basis="B9",
                           until="2026-12-01T08:04:00+08:00"),
                 make_part("G4/b18/2", copies=["G8/c22/7"]),
                 make_part("G8/c22/7", until="2026-12-01T08:18:00+08:00"),
                 make_part("G4/b19/3", round_="R3", slot="B", basis="B8",
                           until="2026-12-01T08:04:00+08:00"),
                 make_part("G4/b19/2", round_="R2", axis="reverse",
                           until="2026-12-01T08:20:00+08:00")]
        orders = [make_order("O41", lines=[("A", "R1", "face", 1, "A-01"),
                                           ("A", "R1", "face", 2, "A-02"),
                                           ("B", "R3", "face", 3, "B-03")]),
                  make_order("O42", deadline="2026-12-01T08:06:00+08:00", priority=2,
                             lines=[("C", "R4", "face", 4, "C-04")]),
                  make_order("O43", deadline="2026-12-01T08:35:00+08:00",
                             lines=[("A", "R2", "reverse", 1, "A-11")])]
        raws = {p["href"]: CSV_A for p in parts}
        inbox = self.make_inbox(FakeSource(parts=parts, orders=orders, raws=raws))
        inbox.sync()
        inbox.collect()
        # 配额 3 份：先硬期限最早的 O42，再 O41 的两个组；同组取期限最近的原件
        self.assertEqual(inbox.source.fetched,
                         ["/raw/G9_q50_1.csv", "/raw/G4_b18_2.csv", "/raw/G4_b19_3.csv"])
        self.assertEqual(inbox.store.data["quota"], {"revision": "W1", "used": 3})
        self.assertEqual(set(inbox.store.data["originals"]),
                         {"G9/q50/1", "G4/b18/2", "G4/b19/3"})

    def test_retry_same_content_keeps_original_source(self):
        part = make_part("G4/b18/2")
        inbox = self.make_inbox(FakeSource(parts=[part], orders=[make_order()],
                                           raws={part["href"]: CSV_A}))
        inbox.sync()
        inbox.collect()
        inbox.collect()  # 已持有可用版本，不再重复取
        self.assertEqual(inbox.source.fetched, [part["href"]])
        parsed = Part.from_dict(part)
        inbox._fetch_one(parsed, "W1", parse_time("2026-12-01T08:01:00+08:00"))
        original = inbox.store.data["originals"][part["key"]]
        self.assertEqual(len(original["versions"]), 1)  # 沿用原始出处
        self.assertTrue(any(e["kind"] == "retry" for e in inbox.store.data["events"]))

    def test_conflicting_content_blocks_key_until_declaration(self):
        part = make_part("G4/b18/2")
        bases = [{"id": "B7", "axes": ["face"], "status": "issued"}]
        source = FakeSource(parts=[part], bases=bases, orders=[make_order()],
                            raws={part["href"]: CSV_A})
        inbox = self.make_inbox(source)
        inbox.sync()
        inbox.collect()
        source.raws[part["href"]] = CSV_B
        inbox._fetch_one(Part.from_dict(part), "W1",
                         parse_time("2026-12-01T08:02:00+08:00"))
        original = inbox.store.data["originals"][part["key"]]
        self.assertTrue(original["contested"])
        self.assertEqual(len(original["versions"]), 2)
        # 键被停用：检测组不可签收且不可恢复，交质检排定重测
        reviews = inbox.review()
        review = reviews[("A", "R1", "face")]
        self.assertFalse(review.signable)
        self.assertIsNotNone(review.unrecoverable)
        # 检具声明有效指纹后，对应版本恢复可用
        source._validity = {"key": part["key"], "state": "valid",
                            "valid_digest": content_digest(CSV_A)}
        inbox.sync()
        review = inbox.review()[("A", "R1", "face")]
        self.assertTrue(review.signable)
        self.assertEqual(review.held[1]["digest"], content_digest(CSV_A))
        # 声明存疑则继续停用
        source._validity = {"key": part["key"], "state": "disputed",
                            "valid_digest": None}
        inbox.sync()
        self.assertFalse(inbox.review()[("A", "R1", "face")].signable)

    def test_overwritten_block_falls_back_to_copy_else_remeasure(self):
        primary = make_part("G4/b18/2", copies=["G8/c22/7"])
        copy = make_part("G8/c22/7", until="2026-12-01T08:18:00+08:00")
        source = FakeSource(parts=[primary, copy], orders=[make_order()],
                            raws={primary["href"]: "gone", copy["href"]: CSV_B})
        inbox = self.make_inbox(source)
        inbox.sync()
        inbox.collect()
        # 上游覆盖后改取其他会话的同轮副本；410 未导出原件，不计配额
        self.assertEqual(inbox.source.fetched, [primary["href"], copy["href"]])
        self.assertEqual(inbox.store.data["quota"]["used"], 1)
        self.assertIn("G8/c22/7", inbox.store.data["originals"])
        self.assertEqual(inbox.store.data["remeasure"], [])
        # 副本也缺证（亦被覆盖）时安排该轮重测
        source2 = FakeSource(parts=[primary, copy], orders=[make_order()],
                             raws={primary["href"]: "gone", copy["href"]: "gone"})
        inbox2 = self.make_inbox(source2)
        inbox2.sync()
        inbox2.collect()
        self.assertEqual(inbox2.store.data["quota"]["used"], 0)
        self.assertEqual(inbox2.store.data["remeasure"][0]["key"], ["A", "R1", "face"])

    def test_same_slot_other_round_cannot_fill_gap(self):
        part = make_part("G4/b19/2", round_="R2", axis="reverse")
        order = make_order("O1", lines=[("A", "R1", "face", 1, "A-01")])
        inbox = self.make_inbox(FakeSource(parts=[part], orders=[order],
                                           raws={part["href"]: CSV_A}))
        inbox.sync()
        inbox.collect()
        self.assertEqual(inbox.store.data["originals"], {})
        self.assertEqual(inbox.store.data["remeasure"][0]["key"], ["A", "R1", "face"])


class BasisTests(unittest.TestCase):
    def bases(self, *records):
        return [Basis.from_dict(r) for r in records]

    def test_basis_states(self):
        bases = self.bases({"id": "B7", "axes": ["face"], "status": "issued"},
                           {"id": "B7.1", "axes": ["face"], "status": "issued",
                            "supersedes": "B7", "applies": ["R1"]},
                           {"id": "B8", "axes": ["face"], "status": "draft"})
        self.assertEqual(basis_state("B9", "face", "R1", bases), (BASIS_MISSING, None))
        self.assertEqual(basis_state("B8", "face", "R1", bases), (BASIS_MISSING, None))
        self.assertEqual(basis_state("B7", "reverse", "R2", bases)[0], "axis")
        self.assertEqual(basis_state("B7", "face", "R1", bases),
                         (BASIS_SUPERSEDED, "B7.1"))
        self.assertEqual(basis_state("B7", "face", "R2", bases), (BASIS_OK, None))


class ExportTests(InboxCase):
    def make_signed_inbox(self, reject=()):
        part = make_part("G4/b18/2")
        bases = [{"id": "B7", "axes": ["face", "reverse"], "status": "issued"}]
        source = FakeSource(parts=[part], bases=bases, orders=[make_order()],
                            raws={part["href"]: CSV_A}, reject=reject)
        inbox = self.make_inbox(source)
        inbox.sync()
        inbox.collect()
        return inbox, source

    def test_late_basis_blocks_then_allows_export(self):
        part = make_part("G4/b19/3", round_="R3", slot="B", basis="B8")
        order = make_order("O41", lines=[("B", "R3", "face", 3, "B-03")])
        csv3 = "position,pitch_um,diagnostic\n3,44,diag-x\n"
        source = FakeSource(parts=[part], orders=[order], raws={part["href"]: csv3})
        inbox = self.make_inbox(source)
        inbox.run()
        # 初审时基准尚未入库：读数不外发
        self.assertEqual(source.posted, [])
        self.assertIn("已取", inbox.status())
        # 基准晚到后按新的导出请求提交，不回填到旧评审
        source._bases = [{"id": "B8", "axes": ["face"], "status": "issued"}]
        inbox.run()
        self.assertEqual(len(source.posted), 1)
        self.assertEqual(source.posted[0]["operation"], "submit")
        self.assertEqual(source.posted[0]["items"], ["B-03"])
        self.assertEqual(source.posted[0]["details"]["B-03"]["source"]["basis"], "B8")

    def test_partial_rejection_leads_to_supplement_with_ref(self):
        inbox, source = self.make_signed_inbox(reject={"A-01"})
        inbox.export()
        self.assertEqual(source.posted[0]["operation"], "submit")
        inbox.export()
        self.assertEqual(len(source.posted), 2)
        supplement = source.posted[1]
        self.assertEqual(supplement["operation"], "supplement")
        self.assertEqual(supplement["items"], ["A-01"])
        self.assertEqual(supplement["details"]["A-01"]["ref"], "BUY-00001")

    def test_retroactive_basis_amends_accepted_positions(self):
        inbox, source = self.make_signed_inbox()
        inbox.export()
        self.assertEqual(source.posted[0]["operation"], "submit")
        # 工位宣布新基准追溯适用于已外发轮次
        source._bases.append({"id": "B7.1", "axes": ["face"], "status": "issued",
                              "supersedes": "B7", "applies": ["R1"]})
        inbox.sync()
        inbox.export()
        self.assertEqual(len(source.posted), 2)
        amend = source.posted[1]
        self.assertEqual(amend["operation"], "amend")
        self.assertEqual(amend["details"]["A-01"]["state"], "withdrawn")
        self.assertIn("B7.1", amend["details"]["A-01"]["reason"])
        self.assertEqual(amend["details"]["A-01"]["ref"], "BUY-00001")
        # 旧回执及当时依据保留在案
        self.assertEqual(inbox.store.data["exports"][0]["receipt"]["receipt"],
                         "BUY-00001")
        self.assertEqual(inbox.store.data["remeasure"][0]["key"], ["A", "R1", "face"])
        # 再运行不重复发更正
        inbox.export()
        self.assertEqual(len(source.posted), 2)

    def test_rejected_position_gets_supplement_not_amend_after_invalidation(self):
        inbox, source = self.make_signed_inbox(reject={"A-01"})
        inbox.export()  # 买方退回
        source._bases.append({"id": "B7.1", "axes": ["face"], "status": "issued",
                              "supersedes": "B7", "applies": ["R1"]})
        inbox.run()
        # 被退回的位置从未签收，失效时不发更正；待重测后补件
        self.assertEqual([p["operation"] for p in source.posted], ["submit"])

    def test_request_id_depends_on_content(self):
        details = {"A-01": {"position": 1, "pitch_um": 42}}
        first = request_id("O41", "submit", ["A-01"], details)
        self.assertEqual(first, request_id("O41", "submit", ["A-01"], details))
        changed = request_id("O41", "submit", ["A-01"],
                             {"A-01": {"position": 1, "pitch_um": 41}})
        self.assertNotEqual(first, changed)

    def test_export_is_idempotent_and_buyer_sees_no_diagnostics(self):
        inbox, source = self.make_signed_inbox()
        inbox.export()
        inbox.export()
        self.assertEqual(len(source.posted), 1)  # 同一请求重跑沿用原回执
        payload = json.dumps(source.posted[0]["details"], ensure_ascii=False)
        self.assertNotIn("diag", payload)
        detail = source.posted[0]["details"]["A-01"]
        self.assertEqual(detail["pitch_um"], 42)
        self.assertEqual(detail["source"]["key"], "G4/b18/2")

    def test_lost_receipt_is_recovered_by_polling(self):
        inbox, source = self.make_signed_inbox()
        source.lose_next = True
        inbox.export()
        record = inbox.store.data["exports"][0]
        self.assertIsNone(record["receipt"])  # 暂未取得合作端回应
        inbox.export()
        self.assertEqual(inbox.store.data["exports"][0]["receipt"]["receipt"],
                         "BUY-00001")
        self.assertEqual(len(source.posted), 1)  # 未重复提交


if __name__ == "__main__":
    unittest.main()
