"""Original forecast horizons and swarm contracts, without live model calls."""
import unittest

from engine.config import CONFIG
from engine.ledger import HORIZON_MS
from engine.models import Prediction
from engine.swarm import PERSONAS, _persona_messages
from engine.pipeline import _carry_momentum


class ForecastSwarmContractTests(unittest.TestCase):
    def test_four_horizons(self):
        expected = {'24h', 'week', 'month', 'year'}
        self.assertEqual(set(HORIZON_MS), expected)
        self.assertEqual(set(CONFIG.horizons), expected)
        self.assertLess(HORIZON_MS['24h'], HORIZON_MS['week'])
        self.assertLess(HORIZON_MS['week'], HORIZON_MS['month'])
        self.assertLess(HORIZON_MS['month'], HORIZON_MS['year'])

    def test_persona_names_unique(self):
        names = [name for name, _ in PERSONAS]
        self.assertEqual(set(names), {'Strategist', 'Economist', 'Naturalist', 'Skeptic'})
        self.assertEqual(len(names), len(set(names)))
        self.assertTrue(all(lens for _, lens in PERSONAS))

    def test_persona_prompt_includes_forecast(self):
        p = Prediction(statement='Will an event happen?', horizon='week', probability=0.4)
        messages = _persona_messages(*PERSONAS[0], 'Brief context', [p])
        self.assertEqual([m['role'] for m in messages], ['system', 'user'])
        self.assertIn(p.statement, messages[1]['content'])
        self.assertIn('Brief context', messages[1]['content'])

    def test_momentum_same_horizon(self):
        old = Prediction(statement='A measurable event will occur', horizon='week', probability=0.3)
        current = Prediction(statement=old.statement, horizon='week', probability=0.7)
        other = Prediction(statement=old.statement, horizon='month', probability=0.7)
        _carry_momentum([old], [current, other])
        self.assertEqual(current.prev_probability, 0.3)
        self.assertIsNone(other.prev_probability)


if __name__ == '__main__':
    unittest.main()
