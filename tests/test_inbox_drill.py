import json
import tempfile
import unittest
from pathlib import Path
from threading import Thread
from urllib.request import Request, urlopen

from inbox.engine import Inbox
from source_fixture import Exchange


class DrillTest(unittest.TestCase):
    """按演练时间点推进：取件 → 覆盖补件 → 基准后补与追溯更正。"""

    def setUp(self):
        self.server = Exchange(("127.0.0.1", 0))
        self.addCleanup(self.server.server_close)
        self.origin = f"http://127.0.0.1:{self.server.server_port}"
        thread = Thread(target=self.server.serve_forever,
                        kwargs={"poll_interval": 0.02}, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(self.server.shutdown)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.inbox = Inbox(self.origin, str(Path(tmp.name) / "inbox.json"))

    def advance(self, index):
        request = Request(self.origin + "/__scene",
                          data=json.dumps({"index": index}).encode(),
                          headers={"Content-Type": "application/json"})
        with urlopen(request, timeout=2) as response:
            self.assertEqual(response.status, 200)

    def exports(self):
        return self.inbox.store.data["exports"]

    def ops(self, records):
        return [(r["operation"], r["items"]) for r in records]

    def test_full_drill(self):
        # 时间点 0：配额 3 份内取件；基准未入库的位置不外发
        status = self.inbox.run()
        self.assertEqual(set(self.inbox.store.data["originals"]),
                         {"G9/q50/1", "G4/b18/2", "G4/b19/3"})
        self.assertEqual(self.inbox.store.data["quota"], {"revision": "W1", "used": 3})
        self.assertEqual(self.ops(self.exports()),
                         [("submit", ["C-04"]), ("submit", ["A-01", "A-02"])])
        rows = {e["item"]: e["status"] for e in self.exports()[1]["receipt"]["rows"]}
        self.assertEqual(rows, {"A-01": "accepted", "A-02": "rejected"})
        self.assertNotIn("B-03", [i for r in self.exports() for i in r["items"]])
        self.assertIn("仍需补件", status)        # A-11 留待下一波次
        self.assertIn("基准 B8 未入库", status)  # B-03 已取但不可签收

        # 时间点 1：新波次补 A-11；被买方退回的 A-02 补件并引用旧回执
        self.advance(1)
        status = self.inbox.run()
        self.assertIn("G4/b19/2", self.inbox.store.data["originals"])
        self.assertEqual(self.inbox.store.data["quota"], {"revision": "W2", "used": 1})
        new = self.exports()[2:]
        self.assertEqual(self.ops(new),
                         [("supplement", ["A-02"]), ("submit", ["A-11"])])
        self.assertEqual(new[0]["details"]["A-02"]["ref"],
                         self.exports()[1]["receipt"]["receipt"])

        # 时间点 2：B8 后补使 B-03 可交；B7.1 追溯取代 B7，R1 位置更正作废
        self.advance(2)
        status = self.inbox.run()
        self.assertEqual(self.inbox.store.data["quota"]["used"], 1)  # 缺口不再占配额
        self.assertEqual(self.inbox.store.data["remeasure"][0]["key"],
                         ["A", "R1", "face"])
        new = self.exports()[4:]
        self.assertEqual(self.ops(new),
                         [("amend", ["A-01", "A-02"]), ("submit", ["B-03"])])
        self.assertEqual(new[0]["details"]["A-01"]["state"], "withdrawn")
        self.assertIn("B7.1", new[0]["details"]["A-01"]["reason"])
        self.assertEqual(new[0]["details"]["A-02"]["ref"],
                         self.exports()[2]["receipt"]["receipt"])
        self.assertIn("来不及取而需重测", status)
        self.assertIn("可以签收", status)

        # 买方资料只含约定读数与来源标识，不泄露检具诊断字段
        for rec in self.exports():
            self.assertNotIn("diag", json.dumps(rec["details"], ensure_ascii=False))

        # 各版本资料不共用回执号；同一导出请求重复运行不产生新回执
        receipts = [r["receipt"]["receipt"] for r in self.exports()]
        self.assertEqual(len(receipts), len(set(receipts)))
        count = len(self.exports())
        self.inbox.run()
        self.assertEqual(len(self.exports()), count)


if __name__ == "__main__":
    unittest.main()
