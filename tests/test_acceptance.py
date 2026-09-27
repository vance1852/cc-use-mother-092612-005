import unittest

from night_market_foundation.acceptance import run


class AcceptanceTest(unittest.TestCase):
    def test_offline_acceptance(self):
        result = run()
        self.assertEqual("ok", result["status"])
        self.assertTrue(result["audit_valid"])
        self.assertFalse(result["first_replayed"])
        self.assertTrue(result["second_replayed"])
        self.assertEqual(1, result["records"])

    def test_signage_acceptance_flow(self):
        signage = run()["signage"]
        self.assertEqual(2, signage["batch_accepted"])
        self.assertEqual(1, signage["batch_replayed"])
        self.assertEqual(1, signage["mismatch_queue_opened"])
        # 应用重启后未结巡检保持原处理状态
        self.assertEqual("claimed", signage["status_after_restart"])
        self.assertEqual("resolved", signage["final_status"])
        # 换位交接完成后标牌位于新专区
        self.assertEqual("deployed", signage["placement_status"])
        self.assertEqual("zone-wellness", signage["placement_zone"])
        # 匿名统计区分有效扫描与摘要不符
        self.assertEqual(1, signage["valid_scans"])
        self.assertEqual(1, signage["mismatch_scans"])


if __name__ == "__main__":
    unittest.main()
