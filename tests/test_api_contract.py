"""Contract checks with the FastAPI lifespan disabled; no feeds or model calls."""
import unittest

from fastapi.testclient import TestClient
from engine.server import app
from engine.state import STATE


class APIContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Do not enter TestClient as a context manager: lifespan starts live loops.
        cls.client = TestClient(app)

    def test_health_shape(self):
        response = self.client.get('/health')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['status'], 'ok')
        self.assertEqual(data['service'], 'pythia-oracle')
        self.assertIsInstance(data['config'], dict)

    def test_config_shape(self):
        response = self.client.get('/config')
        self.assertEqual(response.status_code, 200)
        self.assertIsInstance(response.json().get('horizons'), list)

    def test_predictions_shape(self):
        previous = STATE.predictions
        try:
            STATE.predictions = []
            response = self.client.get('/predictions')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()['predictions'], [])
            self.assertIsInstance(response.json()['horizons'], list)
        finally:
            STATE.predictions = previous


if __name__ == '__main__':
    unittest.main()
