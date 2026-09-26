from __future__ import annotations

import json
import sqlite3
import unittest

from revenue_settlement.api import JsonApplication
from revenue_settlement.service import RevenueService


def post(app, path, payload, actor="ops"):
    return app.handle("POST", path, {"X-Actor-Id": actor}, json.dumps(payload, ensure_ascii=False).encode())


class RevenueApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.app = JsonApplication(RevenueService(self.connection))
        post(self.app, "/users", {"user_id": "st1", "display_name": "场站", "role": "station", "station_id": "FS1"})
        post(self.app, "/users", {"user_id": "ops", "display_name": "经营", "role": "operations"})
        post(self.app, "/users", {"user_id": "aud", "display_name": "审计", "role": "auditor"})

    def tearDown(self) -> None:
        self.connection.close()

    def test_health_and_actor_guard(self) -> None:
        self.assertEqual(self.app.handle("GET", "/health").status, 200)
        response = self.app.handle("GET", "/views/operations?cycle_id=c1")
        self.assertEqual(response.status, 422)
        self.assertEqual(response.body["error"]["code"], "validation_failed")

    def test_source_to_settlement_round_trip(self) -> None:
        source = {
            "source_id": "mkt", "name": "市场", "kind": "MARKET_ENERGY", "cycle_id": "c1",
            "eligible_unit_ids": [], "valid_from": "2026-09-01", "valid_to": "2026-09-30",
            "use_order": 10, "limit_type": "NONE",
            "price_slots": [{
                "slot_id": "peak", "label": "高峰", "starts_at": "2026-09-01T08:00:00Z",
                "ends_at": "2026-09-01T20:00:00Z", "price_cny_per_mwh": "450", "eligible_unit_ids": [],
            }],
        }
        created = post(self.app, "/sources", source)
        self.assertEqual(created.status, 201)
        # 场站无权登记依据。
        denied = post(self.app, "/sources", dict(source, source_id="mkt2"), actor="st1")
        self.assertEqual(denied.status, 403)
        # 修订新版本（乐观锁）。
        revised = dict(source, price_slots=[{
            "slot_id": "peak", "label": "高峰", "starts_at": "2026-09-01T08:00:00Z",
            "ends_at": "2026-09-01T20:00:00Z", "price_cny_per_mwh": "460", "eligible_unit_ids": [],
        }])
        revised["expected_revision"] = 1
        response = self.app.handle("PUT", "/sources/mkt", {"X-Actor-Id": "ops"},
                                   json.dumps(revised).encode())
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["revision"], 2)

        measurement = {
            "version_id": "v1", "cycle_id": "c1", "station_id": "FS1",
            "period_start": "2026-09-01T00:00:00Z", "period_end": "2026-09-30T23:59:59Z",
            "entries": [{"unit_id": "U1", "slot_id": "peak", "energy_mwh": "100", "curtailment_mwh": "0"}],
        }
        frozen = post(self.app, "/measurements", measurement, actor="st1")
        self.assertEqual(frozen.status, 201)
        run = post(self.app, "/measurements/v1/settlements", {})
        self.assertEqual(run.status, 200)
        self.assertEqual(run.body["applications"][0]["unit_price_cny"], "460")
        confirmed = post(self.app, "/measurements/v1/confirm", {
            "idempotency_key": "k1",
            "actual_entries": [{"unit_id": "U1", "slot_id": "peak", "energy_mwh": "90", "curtailment_mwh": "0"}],
        })
        self.assertEqual(confirmed.status, 200)
        self.assertEqual(confirmed.body["applications"][0]["released_mwh"], "10.000")

        application_id = run.body["applications"][0]["application_id"]
        detail = self.app.handle("GET", f"/applications/{application_id}", {"X-Actor-Id": "ops"})
        self.assertEqual(detail.status, 200)
        self.assertIn("reasons", detail.body)

        station = self.app.handle("GET", "/views/station?cycle_id=c1", {"X-Actor-Id": "st1"})
        self.assertEqual(station.status, 200)
        self.assertNotIn("amount_cny", station.body["applications"][0])
        audit = self.app.handle("GET", "/views/audit?cycle_id=c1", {"X-Actor-Id": "aud"})
        self.assertEqual(audit.status, 200)
        chain = self.app.handle("GET", "/audit/chain", {"X-Actor-Id": "aud"})
        self.assertTrue(chain.body["valid"])

    def test_bad_json_shape(self) -> None:
        response = self.app.handle("POST", "/sources", {"X-Actor-Id": "ops"}, b"not-json")
        self.assertEqual(response.status, 422)
        self.assertEqual(response.body["error"]["code"], "validation_failed")


if __name__ == "__main__":
    unittest.main()
