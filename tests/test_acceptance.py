import unittest

from night_market_foundation.acceptance import run


class AcceptanceTest(unittest.TestCase):
    def test_offline_acceptance(self):
        result = run()
        self.assertEqual("ok", result["status"])
        self.assertTrue(result["audit_valid"])
        self.assertFalse(result["first_replayed"])
        self.assertTrue(result["second_replayed"])
        self.assertEqual(5, result["records"])
        self.assertTrue(result["batch_replayed"])
        self.assertTrue(result["fork_detected"])
        self.assertTrue(result["handover_completed"])
        self.assertEqual("zone-huang", result["current_zone"])
        self.assertTrue(result["history_preserved"])
        self.assertEqual(["digest_mismatch", "unknown_plaque"], result["queue_kinds"])
        self.assertTrue(result["late_event_supplemented"])
        self.assertTrue(result["anonymous"])


if __name__ == "__main__":
    unittest.main()
