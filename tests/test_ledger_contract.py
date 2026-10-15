"""Offline ledger regression tests; temporary files only, no network."""
import tempfile
import unittest
from pathlib import Path

from engine.ledger import Ledger, HORIZON_MS
from engine.models import Prediction, WorldBrief, now_ms


class LedgerContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'ledger.jsonl'
        self.ledger = Ledger(path=self.path)

    def prediction(self, **kwargs):
        return Prediction(statement='Sample measurable event', horizon='24h', probability=0.8, **kwargs)

    def test_persist_and_rehydrate(self):
        p = self.prediction()
        self.ledger.record_forecasts([p], None)
        restored = Ledger(path=self.path)
        self.assertIn(p.id, restored.forecasts)
        self.assertEqual(restored.forecasts[p.id]['probability'], 0.8)

    def test_duplicate_forecast_is_not_appended(self):
        p = self.prediction()
        self.ledger.record_forecasts([p], None)
        self.ledger.record_forecasts([p], None)
        self.assertEqual(len(self.ledger.forecasts), 1)
        self.assertEqual(len(self.path.read_text().splitlines()), 1)

    def test_due_after_horizon(self):
        p = self.prediction(ts=now_ms() - HORIZON_MS['24h'] - 1000)
        self.ledger.record_forecasts([p], None)
        self.assertEqual(len(self.ledger.due()), 1)

    def test_resolution_and_brier_score(self):
        p = self.prediction()
        self.ledger.record_forecasts([p], None)
        self.ledger.resolve(p.id, 'yes', 1.0, 'independent evidence')
        restored = Ledger(path=self.path)
        score = restored.scorecard()
        self.assertEqual(score['resolved'], 1)
        self.assertAlmostEqual(score['brier'], 0.04)
        self.assertEqual(score['hit_rate'], 1.0)

    def test_brief_archive_deduplication(self):
        b = WorldBrief(top_events=['event 1'])
        self.ledger.maybe_record_brief(b)
        self.ledger.maybe_record_brief(b)
        self.assertEqual(len(self.ledger.briefs), 1)


if __name__ == '__main__':
    unittest.main()
