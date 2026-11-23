import unittest

from source_fixture import load


class ExchangeInputs(unittest.TestCase):
    def test_related_source_records(self):
        exchange = load()
        initial = exchange["initial"]
        changes = exchange["changes"]
        parts = initial["/gauges/index"]["body"]["parts"]
        self.assertEqual(parts[0]["copies"], [parts[1]["key"]])
        self.assertEqual(parts[0]["round"], parts[1]["round"])
        self.assertNotEqual(parts[0]["axis"], parts[2]["axis"])
        self.assertEqual(changes[0]["routes"]["/gauges/raw/b18-2.csv"]["status"], 410)
        self.assertEqual(changes[1]["routes"]["/gauges/bases"]["body"]["records"][-1]["applies"], ["R1"])
