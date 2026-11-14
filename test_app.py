from __future__ import annotations

import json
import unittest
from unittest.mock import patch

import app as crop_app


class FakeResponse:
    def __init__(self, payload: dict):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class PowerEndpointTests(unittest.TestCase):
    def setUp(self):
        crop_app.app.config.update(TESTING=True)
        self.client = crop_app.app.test_client()
        with crop_app._cache_lock:
            crop_app._cache.clear()

    def sample_payload(self):
        return {
            "properties": {
                "parameter": {
                    "T2M": {"20240101": 20, "20240102": -999},
                    "T2M_MAX": {"20240101": 30, "20240102": -999},
                    "T2M_MIN": {"20240101": 10, "20240102": 8},
                    "PRECTOTCORR": {"20240101": 2, "20240102": 1},
                    "ALLSKY_SFC_SW_DWN": {"20240101": 15, "20240102": -999},
                    "RH2M": {"20240101": 60, "20240102": 50},
                }
            }
        }

    @patch.object(crop_app, "urlopen")
    def test_summarizes_measurements_and_ignores_power_missing_values(self, mock_urlopen):
        mock_urlopen.return_value = FakeResponse(self.sample_payload())
        response = self.client.get(
            "/api/power?latitude=37.5&longitude=-120.5&start=2024-01-01&end=2024-01-02"
        )

        self.assertEqual(response.status_code, 200)
        result = response.get_json()
        self.assertEqual(result["summary"]["T2M"]["value"], 20)
        self.assertEqual(result["summary"]["T2M"]["days_available"], 1)
        self.assertEqual(result["summary"]["PRECTOTCORR"]["value"], 3)
        self.assertIsNone(result["daily"][1]["T2M"])
        self.assertEqual(result["source"], "NASA POWER Daily API (AG community, UTC)")
        mock_urlopen.assert_called_once()

    @patch.object(crop_app, "urlopen")
    def test_rejects_invalid_coordinates_without_calling_nasa(self, mock_urlopen):
        response = self.client.get(
            "/api/power?latitude=91&longitude=-120&start=2024-01-01&end=2024-01-02"
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("Latitude", response.get_json()["error"])
        mock_urlopen.assert_not_called()

    @patch.object(crop_app, "urlopen")
    def test_caches_identical_requests(self, mock_urlopen):
        mock_urlopen.return_value = FakeResponse(self.sample_payload())
        url = "/api/power?latitude=37.5&longitude=-120.5&start=2024-01-01&end=2024-01-02"

        self.assertEqual(self.client.get(url).status_code, 200)
        self.assertEqual(self.client.get(url).status_code, 200)
        mock_urlopen.assert_called_once()


if __name__ == "__main__":
    unittest.main()
