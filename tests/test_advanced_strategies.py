import unittest

import advanced_strategies


class AdvancedStrategyTests(unittest.TestCase):
    def test_analyst_forecasts_preserve_fiscal_year_labels(self):
        payload = {"data": {"yearlyForecast": {"rows": [
            {"fiscalEnd": "Sep 2027", "consensusEPSForecast": 9.5, "noOfEstimates": 12,
             "up": 2, "down": 0},
            {"fiscalEnd": "Sep 2028", "consensusEPSForecast": 10.8, "noOfEstimates": 7},
        ]}}}
        result = advanced_strategies.parse_analyst_forecasts(payload)
        self.assertEqual(result[0]["fiscal_end"], "Sep 2027")
        self.assertEqual(result[0]["eps"], 9.5)
        self.assertEqual(result[0]["revisions_up_4w"], 2)
        self.assertEqual(result[0]["revisions_down_4w"], 0)

    def test_revision_uses_conservative_four_week_proxy_when_archive_is_missing(self):
        condition = advanced_strategies._revision_condition(
            "eps_revision", "预测未下修", None,
            {"eps": 5.0, "revisions_up_4w": 3, "revisions_down_4w": 0}, None)
        self.assertEqual(condition["status"], "PASS")
        self.assertEqual(condition["actual"]["evidence"], "proxy")

    def test_revision_proxy_fails_when_any_analyst_cut_estimate(self):
        condition = advanced_strategies._revision_condition(
            "eps_revision", "预测未下修", None,
            {"eps": 5.0, "revisions_up_4w": 4, "revisions_down_4w": 1}, None)
        self.assertEqual(condition["status"], "FAIL")

    def test_unknown_required_condition_makes_strategy_insufficient(self):
        conditions = [
            advanced_strategies._condition("price", "价格", "PASS", 12, "> 10"),
            advanced_strategies._condition("estimate", "预测", "UNKNOWN", None, "不下修"),
        ]
        result = advanced_strategies._result(conditions, "matched")
        self.assertEqual(result["status"], "insufficient")
        self.assertEqual(result["checks"]["estimate"], "UNKNOWN")

    def test_estimate_snapshot_keys_values_by_fiscal_year(self):
        snapshot = advanced_strategies.estimate_snapshot([{
            "symbol": "TEST", "eps_forecasts": [
                {"fiscal_end": "Dec 2027", "eps": 5.1},
                {"fiscal_end": "Dec 2028", "eps": 5.8},
            ],
        }])
        self.assertEqual(snapshot["stocks"]["TEST"]["Dec 2027"], 5.1)


if __name__ == "__main__":
    unittest.main()
