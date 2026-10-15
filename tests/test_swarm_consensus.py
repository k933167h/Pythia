"""Exercise council consensus, dissent and failure fallback with a fake oracle."""
import unittest
from unittest.mock import patch

from engine.models import Prediction, WorldBrief
from engine.swarm import deliberate
from engine.state import STATE


class FakeOracle:
    model = 'fake-local'

    async def _complete(self, messages, max_tokens=None, model=None):
        system = messages[0]['content']
        if 'Strategist' in system:
            return '[{"i":0,"p":80,"note":"strategic"}]'
        if 'Economist' in system:
            return '[{"i":0,"p":60,"note":"economic"}]'
        return '[]'

    def _extract_objects(self, text):
        import json
        return [json.dumps(item) for item in json.loads(text)]


class DivergentOracle(FakeOracle):
    async def _complete(self, messages, max_tokens=None, model=None):
        if 'Strategist' in messages[0]['content']:
            return '[{"i":0,"p":90,"note":"high"}]'
        return '[{"i":0,"p":20,"note":"low"}]'


class SilentOracle(FakeOracle):
    async def _complete(self, messages, max_tokens=None, model=None):
        if 'Economist' in messages[0]['content']:
            raise RuntimeError('simulated model timeout')
        return await super()._complete(messages, max_tokens=max_tokens, model=model)


class SwarmConsensusTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.previous = STATE.deliberation

    async def asyncTearDown(self):
        STATE.set_deliberation(self.previous)

    async def test_consensus_and_base_probability(self):
        p = Prediction(statement='Test event', horizon='24h', probability=0.4)
        with patch('engine.swarm._persona_weights', return_value={}):
            out = await deliberate(FakeOracle(), WorldBrief(text='test'), [p], personas=['Strategist', 'Economist'])
        self.assertIs(out[0], p)
        self.assertEqual(p.base_probability, 0.4)
        self.assertAlmostEqual(p.probability, 0.7)
        self.assertFalse(p.split)
        self.assertEqual({a.name for a in p.agents}, {'Strategist', 'Economist'})
        self.assertFalse(STATE.deliberation['active'])

    async def test_weighted_consensus(self):
        p = Prediction(statement='Test event', horizon='week', probability=0.5)
        with patch('engine.swarm._persona_weights', return_value={'Strategist': 2.0, 'Economist': 1.0}):
            await deliberate(FakeOracle(), None, [p], personas=['Strategist', 'Economist'])
        self.assertAlmostEqual(p.probability, 0.73)

    async def test_no_personas_keeps_prediction(self):
        p = Prediction(statement='Test event', horizon='month', probability=0.5)
        result = await deliberate(FakeOracle(), None, [p], personas=[])
        self.assertIs(result[0], p)
        self.assertEqual(p.probability, 0.5)
        self.assertEqual(p.agents, [])

    async def test_large_disagreement_sets_split(self):
        p = Prediction(statement='Test event', horizon='year', probability=0.5)
        with patch('engine.swarm._persona_weights', return_value={}):
            await deliberate(DivergentOracle(), None, [p], personas=['Strategist', 'Economist'])
        self.assertTrue(p.split)
        self.assertAlmostEqual(p.probability, 0.55)

    async def test_one_persona_failure_keeps_other_vote(self):
        p = Prediction(statement='Test event', horizon='week', probability=0.4)
        with patch('engine.swarm._persona_weights', return_value={}):
            await deliberate(SilentOracle(), None, [p], personas=['Strategist', 'Economist'])
        self.assertEqual(p.probability, 0.8)
        self.assertFalse(p.split)
        self.assertEqual([a.name for a in p.agents], ['Strategist'])
        self.assertEqual(STATE.deliberation['voices']['Economist']['status'], 'silent')


if __name__ == '__main__':
    unittest.main()
