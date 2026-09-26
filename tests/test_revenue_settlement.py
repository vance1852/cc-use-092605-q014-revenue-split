from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timezone
from decimal import Decimal

from revenue_settlement.api import JsonApplication
from revenue_settlement.clock import FrozenClock
from revenue_settlement.engine import (
    DemandLine,
    ReservationSlice,
    SourceCandidate,
    apportion,
    carry_forward,
    settle,
)
from revenue_settlement.errors import Conflict, Forbidden, InvalidState, ValidationFailed
from revenue_settlement.service import RevenueService


def source(source_id: str, source_type: str, order: int, *, scope=(), cap=None,
           price=None, time_prices=None, valid_from="2026-09-01", valid_to="2026-09-30"):
    return SourceCandidate(
        source_id=source_id, source_type=source_type, usage_order=order,
        scope_unit_ids=tuple(scope), valid_from=valid_from, valid_to=valid_to,
        cap_mwh=None if cap is None else Decimal(cap),
        available_mwh=None if cap is None else Decimal(cap),
        unit_price_cny=None if price is None else Decimal(price),
        time_prices={k: Decimal(v) for k, v in (time_prices or {}).items()},
    )


def line(line_id, unit="U1", kind="generated", bucket="FLAT", mwh="10", confirmed=False):
    return DemandLine(line_id, unit, kind, bucket, Decimal(mwh), confirmed)


class EngineTests(unittest.TestCase):
    def test_usage_order_picks_first_covering_source(self) -> None:
        sources = [
            source("low", "guaranteed", 20, scope=["U1"], cap="100", price="0.40"),
            source("high", "guaranteed", 10, scope=["U1"], cap="100", price="0.50"),
        ]
        result = apportion([line("L1")], sources, period_start="2026-09-01", period_end="2026-09-30")
        self.assertEqual(result["allocations"][0].source_id, "high")

    def test_market_uses_time_bucket_price(self) -> None:
        market = source("mkt", "market", 10, time_prices={"PEAK": "0.45", "FLAT": "0.38", "VALLEY": "0.25"})
        peak = apportion([line("L1", bucket="PEAK")], [market], period_start="2026-09-01", period_end="2026-09-30")
        valley = apportion([line("L2", bucket="VALLEY")], [market], period_start="2026-09-01", period_end="2026-09-30")
        self.assertEqual(peak["allocations"][0].unit_price_cny, Decimal("0.45"))
        self.assertEqual(valley["allocations"][0].amount_cny, Decimal("2.50"))

    def test_capacity_skips_exhausted_source(self) -> None:
        sources = [
            source("capped", "guaranteed", 10, cap="5", price="0.40"),
            source("market", "market", 20, time_prices={"PEAK": "0.30", "FLAT": "0.30", "VALLEY": "0.30"}),
        ]
        result = apportion([line("L1", mwh="8")], sources, period_start="2026-09-01", period_end="2026-09-30")
        self.assertEqual(result["allocations"][0].source_id, "market")
        self.assertIn("不足", result["explanations"]["L1"]["skipped"][0]["reasons"][0])

    def test_scope_and_validity_filter_sources(self) -> None:
        sources = [
            source("scoped", "guaranteed", 10, scope=["OTHER"], cap="100", price="0.40"),
            source("expired", "guaranteed", 20, cap="100", price="0.40",
                   valid_from="2026-08-01", valid_to="2026-08-15"),
            source("market", "market", 30, time_prices={"PEAK": "0.30", "FLAT": "0.30", "VALLEY": "0.30"}),
        ]
        result = apportion([line("L1")], sources, period_start="2026-09-01", period_end="2026-09-30")
        self.assertEqual(result["allocations"][0].source_id, "market")
        skipped = {item["source_id"] for item in result["explanations"]["L1"]["skipped"]}
        self.assertEqual(skipped, {"scoped", "expired"})

    def test_curtailment_requires_confirmed_loss(self) -> None:
        sources = [
            source("curt", "curtailment_comp", 5, cap="100", price="0.30"),
            source("market", "market", 40, time_prices={"PEAK": "0.30", "FLAT": "0.30", "VALLEY": "0.30"}),
        ]
        pending = apportion([line("L1", kind="curtailed", confirmed=False)], sources,
                            period_start="2026-09-01", period_end="2026-09-30")
        self.assertEqual(pending["pending_loss"][0]["line_id"], "L1")
        confirmed = apportion([line("L1", kind="curtailed", confirmed=True)], sources,
                              period_start="2026-09-01", period_end="2026-09-30")
        self.assertEqual(confirmed["allocations"][0].source_id, "curt")

    def test_generated_energy_never_uses_curtailment_source(self) -> None:
        sources = [source("curt", "curtailment_comp", 5, cap="100", price="0.30")]
        with self.assertRaises(ValueError):
            apportion([line("L1")], sources, period_start="2026-09-01", period_end="2026-09-30")

    def test_settle_partial_releases_tail(self) -> None:
        reservations = [
            ReservationSlice("r1", "s1", Decimal("40")),
            ReservationSlice("r2", "s2", Decimal("60")),
        ]
        result = settle(Decimal("70"), reservations)
        self.assertEqual(result["settled"][0]["settled_mwh"], Decimal("40.000"))
        self.assertEqual(result["settled"][0]["released_mwh"], Decimal("0.000"))
        self.assertEqual(result["settled"][1]["settled_mwh"], Decimal("30.000"))
        self.assertEqual(result["settled"][1]["released_mwh"], Decimal("30.000"))

    def test_carry_forward_moves_to_target_sources(self) -> None:
        target = [
            source("gtd2", "guaranteed", 10, cap="30", price="0.41",
                   valid_from="2026-10-01", valid_to="2026-10-31"),
            source("mkt2", "market", 40,
                   time_prices={"PEAK": "0.39", "FLAT": "0.39", "VALLEY": "0.39"},
                   valid_from="2026-10-01", valid_to="2026-10-31"),
        ]
        result = carry_forward([ReservationSlice("r1", "gtd1", Decimal("50"))], target,
                               period_start="2026-10-01", period_end="2026-10-31",
                               unit_id="U1", energy_kind="generated")
        carried = {(item["source_id"], Decimal(str(item["mwh"]))) for item in result["carried"]}
        self.assertEqual(carried, {("gtd2", Decimal("30.000")), ("mkt2", Decimal("20.000"))})
        self.assertEqual(result["released_mwh"], Decimal("0.000"))


class ServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 20, 8, 0, tzinfo=timezone.utc))
        self.service = RevenueService(self.connection, self.clock)
        for uid, role, station in (
            ("st1", "station", "S1"), ("st2", "station", "S2"),
            ("biz", "business", None), ("fin", "finance", None), ("aud", "auditor", None),
        ):
            self.service.create_user(uid, uid, role, station)
        self.service.register_unit("st1", {"unit_id": "U1", "station_id": "S1", "name": "1号机", "capacity_mw": "10"})
        self.service.register_unit("st2", {"unit_id": "U2", "station_id": "S2", "name": "2号机", "capacity_mw": "8"})
        self.service.register_period("st1", {"period_id": "P1", "start_date": "2026-09-01", "end_date": "2026-09-30"})
        self.service.register_period("st1", {"period_id": "P2", "start_date": "2026-10-01", "end_date": "2026-10-31"})

    def tearDown(self) -> None:
        self.connection.close()

    def _sources(self) -> None:
        self.service.register_source("biz", {"source_id": "GTD", "source_type": "guaranteed", "usage_order": 10,
                                             "scope_unit_ids": ["U1"], "valid_from": "2026-09-01", "valid_to": "2026-09-30",
                                             "cap_mwh": "100", "unit_price_cny": "0.40"})
        self.service.register_source("biz", {"source_id": "MKT", "source_type": "market", "usage_order": 40,
                                             "scope_unit_ids": "*", "valid_from": "2026-09-01", "valid_to": "2026-09-30",
                                             "time_prices": {"PEAK": "0.45", "FLAT": "0.38", "VALLEY": "0.25"}})
        self.service.register_source("biz", {"source_id": "CURT", "source_type": "curtailment_comp", "usage_order": 5,
                                             "scope_unit_ids": "*", "valid_from": "2026-09-01", "valid_to": "2026-09-30",
                                             "cap_mwh": "500", "unit_price_cny": "0.30"})

    def _freeze(self, version="MV1", period="P1"):
        return self.service.freeze_metering("st1", {"metering_version_id": version, "period_id": period, "lines": [
            {"line_id": "L1", "unit_id": "U1", "energy_kind": "generated", "time_bucket": "PEAK", "mwh": "50"},
        ]})

    def test_role_permissions_enforced(self) -> None:
        with self.assertRaises(Forbidden):
            self.service.register_source("st1", {"source_id": "X", "source_type": "market", "usage_order": 1,
                                                 "valid_from": "2026-09-01", "valid_to": "2026-09-30",
                                                 "time_prices": {"PEAK": "1", "FLAT": "1", "VALLEY": "1"}})
        with self.assertRaises(Forbidden):
            self.service.freeze_metering("biz", {"metering_version_id": "M", "period_id": "P1", "lines": [
                {"line_id": "L1", "unit_id": "U1", "energy_kind": "generated", "time_bucket": "PEAK", "mwh": "1"}]})

    def test_station_cannot_freeze_other_station_metering(self) -> None:
        with self.assertRaises(Forbidden):
            self.service.freeze_metering("st2", {"metering_version_id": "MX", "period_id": "P1", "lines": [
                {"line_id": "L1", "unit_id": "U1", "energy_kind": "generated", "time_bucket": "PEAK", "mwh": "1"}]})

    def test_market_source_requires_all_time_prices(self) -> None:
        with self.assertRaises(ValidationFailed):
            self.service.register_source("biz", {"source_id": "BAD", "source_type": "market", "usage_order": 1,
                                                 "valid_from": "2026-09-01", "valid_to": "2026-09-30",
                                                 "time_prices": {"PEAK": "0.45", "FLAT": "0.38"}})

    def test_apportion_reserves_quota_and_is_idempotent(self) -> None:
        self._sources()
        self._freeze()
        run = self.service.apportion("biz", "MV1", "key-1")
        self.assertEqual(run["allocations"][0]["source_id"], "GTD")
        self.assertEqual(self.service.source_quota("biz", "GTD")["balance_mwh"], "50.000")
        # 重复分摊回放原批次，不重复预占额度。
        replay = self.service.apportion("biz", "MV1", "key-1")
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["run_id"], run["run_id"])
        self.assertEqual(self.service.source_quota("biz", "GTD")["balance_mwh"], "50.000")

    def test_apportion_same_key_different_payload_rejected(self) -> None:
        self._sources()
        self._freeze()
        self.service.apportion("biz", "MV1", "shared-key")
        # 另一周期冻结内容不同的版本；幂等判定先于来源校验。
        self.service.freeze_metering("st1", {"metering_version_id": "MV-OTHER", "period_id": "P2", "lines": [
            {"line_id": "L1", "unit_id": "U1", "energy_kind": "generated", "time_bucket": "PEAK", "mwh": "33"},
        ]})
        # 同一复用编号锚定不同计量版本（内容不同）时拒绝。
        with self.assertRaises(Conflict):
            self.service.apportion("biz", "MV-OTHER", "shared-key")

    def test_loss_must_be_confirmed_before_apportion(self) -> None:
        self._sources()
        self.service.freeze_metering("st1", {"metering_version_id": "MVL", "period_id": "P1", "lines": [
            {"line_id": "L1", "unit_id": "U1", "energy_kind": "generated", "time_bucket": "PEAK", "mwh": "50"},
            {"line_id": "L2", "unit_id": "U1", "energy_kind": "curtailed", "time_bucket": "FLAT", "mwh": "10"},
        ]})
        with self.assertRaises(InvalidState):
            self.service.apportion("biz", "MVL", "k")
        self.service.confirm_loss("st1", "MVL", ["L2"])
        run = self.service.apportion("biz", "MVL", "k")
        self.assertEqual({a["source_id"] for a in run["allocations"]}, {"GTD", "CURT"})

    def test_settle_releases_tail_and_replays(self) -> None:
        self._sources()
        self._freeze()
        self.service.apportion("biz", "MV1", "app")
        result = self.service.confirm_metering("biz", "MV1", {"L1": "40"}, "set")
        self.assertEqual(result["state"], "partially_settled")
        self.assertEqual(result["released_mwh"], "10.000")
        self.assertEqual(self.service.source_quota("biz", "GTD")["balance_mwh"], "60.000")
        replay = self.service.confirm_metering("biz", "MV1", {"L1": "40"}, "set")
        self.assertTrue(replay["replayed"])
        with self.assertRaises(Conflict):
            self.service.confirm_metering("biz", "MV1", {"L1": "41"}, "set")

    def test_cancel_releases_all_reservations(self) -> None:
        self._sources()
        self._freeze()
        run = self.service.apportion("biz", "MV1", "app")
        self.service.cancel_run("biz", run["run_id"])
        self.assertEqual(self.service.source_quota("biz", "GTD")["balance_mwh"], "100.000")
        with self.assertRaises(InvalidState):
            self.service.cancel_run("biz", run["run_id"])
        # 撤销后可重新分摊。
        again = self.service.apportion("biz", "MV1", "app-2")
        self.assertFalse(again["replayed"])

    def test_failover_carries_to_open_period_only(self) -> None:
        self._sources()
        self.service.register_source("biz", {"source_id": "GTD2", "source_type": "guaranteed", "usage_order": 10,
                                             "scope_unit_ids": ["U1"], "valid_from": "2026-10-01", "valid_to": "2026-10-31",
                                             "cap_mwh": "30", "unit_price_cny": "0.41"})
        self.service.register_source("biz", {"source_id": "MKT2", "source_type": "market", "usage_order": 40,
                                             "scope_unit_ids": "*", "valid_from": "2026-10-01", "valid_to": "2026-10-31",
                                             "time_prices": {"PEAK": "0.46", "FLAT": "0.39", "VALLEY": "0.26"}})
        self._freeze()
        run = self.service.apportion("biz", "MV1", "app")
        result = self.service.fail_run("biz", run["run_id"], "P2", "fail")
        detail = self.service.run_detail("biz", result["new_run_id"])
        carried = {r["source_id"]: r["reserved_mwh"] for r in detail["reservations"]}
        self.assertEqual(carried, {"GTD2": "30.000", "MKT2": "20.000"})
        # 已释放结转，不能再对原版本分摊。
        with self.assertRaises(InvalidState):
            self.service.apportion("biz", "MV1", "app-late")

    def test_adjustment_requires_different_reviewer_and_creates_version(self) -> None:
        self._sources()
        self._freeze()
        run = self.service.apportion("biz", "MV1", "app")
        self.service.confirm_metering("biz", "MV1", {"L1": "40"}, "set")
        adjustment = self.service.request_adjustment(
            "biz", {"run_id": run["run_id"], "reason": "补记", "line_deltas": {"L1": "5"}})
        # 经营无权复核；即便有权也必须与申请人不同。
        with self.assertRaises(Forbidden):
            self.service.review_adjustment("biz", adjustment["adjustment_id"], True)
        reviewed = self.service.review_adjustment("fin", adjustment["adjustment_id"], True)
        self.assertEqual(reviewed["state"], "reviewed")
        self.assertIn("new_run_id", reviewed)
        detail = self.service.run_detail("biz", reviewed["new_run_id"])
        self.assertEqual(detail["state"], "adjusted")

    def test_views_hide_unauthorized_fields_and_stations(self) -> None:
        self._sources()
        self._freeze()
        self.service.apportion("biz", "MV1", "app")
        station_view = self.service.explain_revenue("st1", "L1")
        self.assertNotIn("unit_price_cny", station_view)
        self.assertNotIn("amount_cny", station_view)
        business_view = self.service.explain_revenue("biz", "L1")
        self.assertIn("unit_price_cny", business_view)
        # 场站 2 无权查看场站 1 的收益。
        with self.assertRaises(Forbidden):
            self.service.explain_revenue("st2", "L1")

    def test_audit_chain_detects_tampering(self) -> None:
        self._sources()
        self._freeze()
        self.service.apportion("biz", "MV1", "app")
        self.assertTrue(self.service.audit_chain("aud")["valid"])
        self.connection.execute("UPDATE revenue_audit_events SET payload_json='{}' WHERE event_id=1")
        self.assertFalse(self.service.audit_chain("aud")["valid"])

    def test_api_boundary(self) -> None:
        app = JsonApplication(self.service)
        self.assertEqual(app.handle("GET", "/health").status, 200)
        missing_actor = app.handle("POST", "/sources", {}, b"{}")
        self.assertEqual(missing_actor.status, 422)
        forbidden = app.handle("POST", "/periods", {"X-Actor-Id": "biz"},
                               b'{"period_id":"PX","start_date":"2026-09-01","end_date":"2026-09-30"}')
        self.assertEqual(forbidden.status, 403)


if __name__ == "__main__":
    unittest.main()
