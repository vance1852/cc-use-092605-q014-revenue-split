from __future__ import annotations

import json
import sqlite3
import unittest
from datetime import datetime, timezone
from decimal import Decimal

from revenue_settlement.clock import FrozenClock
from revenue_settlement.errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from revenue_settlement.service import RevenueService


CYCLE = "2026-09"


def market_source(**over):
    base = {
        "source_id": "mkt", "name": "市场电费", "kind": "MARKET_ENERGY", "cycle_id": CYCLE,
        "eligible_unit_ids": [], "valid_from": "2026-09-01", "valid_to": "2026-09-30",
        "use_order": 10, "limit_type": "NONE",
        "price_slots": [
            {"slot_id": "peak", "label": "高峰", "starts_at": "2026-09-01T08:00:00Z",
             "ends_at": "2026-09-01T20:00:00Z", "price_cny_per_mwh": "450", "eligible_unit_ids": []},
            {"slot_id": "valley", "label": "低谷", "starts_at": "2026-09-01T20:00:00Z",
             "ends_at": "2026-09-02T08:00:00Z", "price_cny_per_mwh": "280", "eligible_unit_ids": []},
        ],
    }
    base.update(over)
    return base


def fixed_source(source_id, kind, order, *, eligible=(), limit_type="NONE", cap=None,
                 price="30", rate=None):
    raw = {
        "source_id": source_id, "name": source_id, "kind": kind, "cycle_id": CYCLE,
        "eligible_unit_ids": list(eligible), "valid_from": "2026-09-01", "valid_to": "2026-09-30",
        "use_order": order, "limit_type": limit_type,
    }
    if cap is not None:
        raw["cap_amount"] = cap
    if rate is not None:
        raw["curtailment_rate_cny_per_mwh"] = rate
    else:
        raw["unit_price_cny_per_mwh"] = price
    return raw


MEASUREMENT = {
    "version_id": "v1", "cycle_id": CYCLE, "station_id": "FS1",
    "period_start": "2026-09-01T00:00:00Z", "period_end": "2026-09-30T23:59:59Z",
    "entries": [
        {"unit_id": "U1", "slot_id": "peak", "energy_mwh": "120", "curtailment_mwh": "10"},
        {"unit_id": "U1", "slot_id": "valley", "energy_mwh": "80", "curtailment_mwh": "0"},
        {"unit_id": "U2", "slot_id": "peak", "energy_mwh": "60", "curtailment_mwh": "5"},
    ],
}

CONFIRM_PAYLOAD = {
    "idempotency_key": "cf1",
    "actual_entries": [
        {"unit_id": "U1", "slot_id": "peak", "energy_mwh": "100", "curtailment_mwh": "10"},
        {"unit_id": "U1", "slot_id": "valley", "energy_mwh": "80", "curtailment_mwh": "0"},
        {"unit_id": "U2", "slot_id": "peak", "energy_mwh": "60", "curtailment_mwh": "5"},
    ],
}


class RevenueServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 26, 8, 0, tzinfo=timezone.utc))
        self.service = RevenueService(self.connection, self.clock)
        self.service.create_user("st1", "场站甲", "station", "FS1")
        self.service.create_user("st2", "场站乙", "station", "FS2")
        self.service.create_user("ops", "经营", "operations")
        self.service.create_user("aud", "审计", "auditor")
        self.service.register_source("ops", market_source())
        self.service.register_source(
            "ops", fixed_source("gp", "GUARANTEED_PURCHASE", 20, limit_type="MWH", cap="100", price="430")
        )
        self.service.register_source("ops", fixed_source("gc", "GREEN_CERTIFICATE", 30, eligible=["U1"], price="30"))
        self.service.register_source("ops", fixed_source("ps", "PEAK_SHAVING", 40, eligible=["U2"], price="15"))
        self.service.register_source(
            "ops", fixed_source("cc", "CURTAILMENT_COMPENSATION", 50, limit_type="MWH", cap="500", rate="120")
        )

    def tearDown(self) -> None:
        self.connection.close()

    # ── 登记与权限 ─────────────────────────────────────────────

    def test_source_validation_by_kind(self) -> None:
        with self.assertRaisesRegex(ValidationFailed, "计价时段"):
            self.service.register_source("ops", {k: v for k, v in market_source().items() if k != "price_slots"})
        with self.assertRaisesRegex(ValidationFailed, "限电补偿"):
            self.service.register_source("ops", {
                "source_id": "bad", "name": "x", "kind": "CURTAILMENT_COMPENSATION", "cycle_id": CYCLE,
                "eligible_unit_ids": [], "valid_from": "2026-09-01", "valid_to": "2026-09-30",
                "use_order": 60, "limit_type": "NONE"})
        with self.assertRaises(Forbidden):
            self.service.register_source("st1", market_source(source_id="mkt2"))

    def test_station_account_requires_station_id(self) -> None:
        with self.assertRaises(ValidationFailed):
            self.service.create_user("x", "x", "station")

    def test_source_detail_is_masked_for_station_role(self) -> None:
        full = self.service.source_view("ops", "mkt")
        self.assertIn("price_slots", full)
        self.assertIn("cap_amount", full)
        masked = self.service.source_view("st1", "mkt")
        for field in ("price_slots", "cap_amount", "unit_price_cny_per_mwh", "curtailment_rate_cny_per_mwh"):
            self.assertNotIn(field, masked)
        with self.assertRaises(NotFound):
            self.service.source_view("ops", "missing")

    def test_freeze_is_station_scoped(self) -> None:
        foreign = dict(MEASUREMENT, version_id="v-foreign", station_id="FS1")
        with self.assertRaises(Forbidden):
            self.service.freeze_measurement("st2", foreign)
        with self.assertRaises(Forbidden):
            self.service.freeze_measurement("ops", MEASUREMENT)

    # ── 分摊与预占 ─────────────────────────────────────────────

    def _freeze_and_calculate(self, version_id="v1", measurement=None):
        self.service.freeze_measurement("st1", measurement or dict(MEASUREMENT, version_id=version_id))
        return self.service.calculate_settlement("ops", version_id)

    def test_settlement_prices_by_slot_and_reserves_cap(self) -> None:
        run = self._freeze_and_calculate()
        lines = {(line["source_id"], line["unit_id"], line["slot_id"]): line for line in run["applications"]}
        self.assertEqual(lines[("mkt", "U1", "peak")]["amount_cny"], "54000.00")
        self.assertEqual(lines[("mkt", "U1", "valley")]["amount_cny"], "22400.00")
        # 绿证只适用 U1，调峰只适用 U2。
        self.assertIn(("gc", "U1", "peak"), lines)
        self.assertNotIn(("gc", "U2", "peak"), lines)
        self.assertIn(("ps", "U2", "peak"), lines)
        # 冻结阶段不产生限电补偿。
        self.assertFalse(any(line["source_id"] == "cc" for line in run["applications"]))
        # 所有能量行落库后处于 reserved。
        states = {row["state"] for row in
                  self.connection.execute("SELECT state FROM revenue_applications").fetchall()}
        self.assertEqual(states, {"reserved"})

    def test_cap_caps_reservation_and_run_is_deterministic(self) -> None:
        # 给市场电费加 100 MWh 上限：保障收购顺序在后，只覆盖未覆盖电量。
        capped = market_source(limit_type="MWH", cap_amount="100")
        self.service.revise_source("ops", 1, capped)
        run = self._freeze_and_calculate()
        totals: dict[str, Decimal] = {}
        for line in run["applications"]:
            totals[line["source_id"]] = totals.get(line["source_id"], Decimal(0)) + Decimal(line["quantity_mwh"])
        self.assertEqual(totals["mkt"], Decimal("100.000"))
        self.assertEqual(totals["gp"], Decimal("100.000"))
        # 总电量 260，市场与保障各占 100 后，U2 高峰 60 MWh 无来源可覆盖。
        self.assertEqual(run["unallocated_energy"], [
            {"unit_id": "U2", "slot_id": "peak", "quantity_mwh": "60.000"},
        ])
        # 相同输入重放返回同一运行。
        again = self.service.calculate_settlement("ops", "v1")
        self.assertTrue(again["replayed"])
        self.assertEqual(again["run_id"], run["run_id"])

    def test_rule_change_supersedes_old_run_and_releases_reservations(self) -> None:
        first = self._freeze_and_calculate()
        old_run_id = first["run_id"]
        # 经营调整市场高峰价（依据新版本），未确认周期允许重算，旧运行绑定旧快照。
        new_market = market_source(price_slots=[
            {"slot_id": "peak", "label": "高峰", "starts_at": "2026-09-01T08:00:00Z",
             "ends_at": "2026-09-01T20:00:00Z", "price_cny_per_mwh": "460", "eligible_unit_ids": []},
            {"slot_id": "valley", "label": "低谷", "starts_at": "2026-09-01T20:00:00Z",
             "ends_at": "2026-09-02T08:00:00Z", "price_cny_per_mwh": "280", "eligible_unit_ids": []},
        ])
        self.service.revise_source("ops", 1, new_market)
        second = self.service.calculate_settlement("ops", "v1")
        self.assertNotEqual(second["run_id"], old_run_id)
        self.assertFalse(second["replayed"])
        # 高峰单价变为 460，新运行反映新规则。
        peak = next(line for line in second["applications"]
                    if line["source_id"] == "mkt" and line["slot_id"] == "peak")
        self.assertEqual(peak["unit_price_cny"], "460")
        old_run = self.connection.execute(
            "SELECT state,result_json FROM settlement_runs WHERE run_id=?", (old_run_id,)
        ).fetchone()
        self.assertEqual(old_run["state"], "superseded")
        self.assertIn("450", old_run["result_json"])  # 旧运行快照保留旧单价，可审计
        # 旧预占全部释放，不占用额度；新运行重新预占。
        old_states = self.connection.execute(
            "SELECT state,count(*) n FROM revenue_applications WHERE run_id=? GROUP BY state",
            (old_run_id,),
        ).fetchall()
        self.assertEqual({row["state"]: row["n"] for row in old_states}, {"released": 6})

    def test_revise_uses_optimistic_lock_and_keeps_cycle(self) -> None:
        with self.assertRaises(InvalidState):
            self.service.revise_source("ops", 99, market_source())
        with self.assertRaises(ValidationFailed):
            self.service.revise_source("ops", 1, market_source(cycle_id="2026-10"))
        self.service.revise_source("ops", 1, market_source())
        with self.assertRaises(InvalidState):
            self.service.revise_source("ops", 1, market_source())

    def test_rules_do_not_affect_confirmed_period(self) -> None:
        self._freeze_and_calculate()
        self.service.confirm_measurement("ops", "v1", CONFIRM_PAYLOAD)
        with self.assertRaises(InvalidState):
            self.service.calculate_settlement("ops", "v1")

    # ── 确认核销与幂等 ─────────────────────────────────────────

    def test_confirmation_settles_releases_and_compensates_loss(self) -> None:
        self._freeze_and_calculate()
        result = self.service.confirm_measurement("ops", "v1", CONFIRM_PAYLOAD)
        released = {row["source_id"]: row["released_mwh"] for row in result["applications"]
                    if Decimal(row["released_mwh"]) > 0}
        self.assertEqual(released, {"mkt": "20.000", "gc": "20.000"})
        loss = {row["unit_id"]: row["settled_cny"] for row in result["loss_applications"]}
        self.assertEqual(loss, {"U1": "1200.00", "U2": "600.00"})
        version = self.connection.execute(
            "SELECT state FROM measurement_versions WHERE version_id='v1'"
        ).fetchone()
        self.assertEqual(version["state"], "confirmed")

    def test_confirmation_idempotency_returns_original_after_confirmed(self) -> None:
        self._freeze_and_calculate()
        first = self.service.confirm_measurement("ops", "v1", CONFIRM_PAYLOAD)
        second = self.service.confirm_measurement("ops", "v1", CONFIRM_PAYLOAD)
        self.assertEqual(first, second)
        changed = json.loads(json.dumps(CONFIRM_PAYLOAD))
        changed["actual_entries"][0]["energy_mwh"] = "99"
        with self.assertRaises(Conflict):
            self.service.confirm_measurement("ops", "v1", changed)

    def test_confirm_without_run_is_rejected(self) -> None:
        self.service.freeze_measurement("st1", dict(MEASUREMENT, version_id="v-norun"))
        with self.assertRaises(InvalidState):
            self.service.confirm_measurement("ops", "v-norun", CONFIRM_PAYLOAD)

    def test_revoked_line_does_not_consume_confirmation_energy(self) -> None:
        run = self._freeze_and_calculate(version_id="v3", measurement=dict(MEASUREMENT, version_id="v3"))
        # 撤销 U1 低谷市场行后再全额确认：该行保持释放，不会分走核销电量。
        valley = next(line for line in run["applications"]
                      if line["source_id"] == "mkt" and line["unit_id"] == "U1" and line["slot_id"] == "valley")
        self.service.revoke_application("ops", valley["application_id"], "低谷依据撤销")
        result = self.service.confirm_measurement("ops", "v3", dict(CONFIRM_PAYLOAD, idempotency_key="cf3"))
        settled_ids = {row["application_id"] for row in result["applications"]}
        self.assertNotIn(valley["application_id"], settled_ids)
        row = self.connection.execute(
            "SELECT state,released_mwh FROM revenue_applications WHERE application_id=?",
            (valley["application_id"],),
        ).fetchone()
        self.assertEqual(row["state"], "released")
        self.assertEqual(row["released_mwh"], "80.000")
        # 其他行（如 U1 高峰市场）照常全额核销。
        peak = next(row for row in result["applications"]
                    if row["source_id"] == "mkt" and row["unit_id"] == "U1")
        self.assertEqual(peak["settled_mwh"], "100.000")

    # ── 撤销与执行失败 ─────────────────────────────────────────

    def test_revoke_releases_and_failure_carries_forward(self) -> None:
        run = self._freeze_and_calculate(version_id="v2", measurement=dict(MEASUREMENT, version_id="v2"))
        market_app = next(line for line in run["applications"] if line["source_id"] == "mkt")
        revoked = self.service.revoke_application("ops", market_app["application_id"], "依据作废")
        self.assertEqual(revoked["state"], "released")
        with self.assertRaises(ValidationFailed):
            self.service.revoke_application("ops", market_app["application_id"], "  ")
        with self.assertRaises(InvalidState):
            self.service.revoke_application("ops", market_app["application_id"], "再次撤销")
        ps_app = next(line for line in run["applications"] if line["source_id"] == "ps")
        failed = self.service.mark_execution_failed("ops", ps_app["application_id"], "拨付通道异常")
        self.assertEqual(failed["state"], "carried_forward")
        self.assertEqual(failed["carried_mwh"], "60.000")
        self.assertEqual(failed["carried_cny"], "900.00")
        # 结转量继续占用周期额度。
        overview = self.service.operations_view("ops", CYCLE)
        self.assertEqual(overview["totals"]["carried_mwh"], "60.000")
        self.assertEqual(overview["totals"]["carried_cny"], "900.00")
        with self.assertRaises(Forbidden):
            self.service.revoke_application("st1", ps_app["application_id"], "场站无权")

    # ── 人工改账 ───────────────────────────────────────────────

    def test_adjustment_requires_different_reviewer_and_creates_revision(self) -> None:
        self._freeze_and_calculate()
        confirmed = self.service.confirm_measurement("ops", "v1", CONFIRM_PAYLOAD)
        target = confirmed["applications"][0]["application_id"]
        before = self.connection.execute(
            "SELECT revision FROM revenue_applications WHERE application_id=?", (target,)
        ).fetchone()["revision"]
        adjustment = self.service.request_adjustment("st1", target, {
            "idempotency_key": "adj1", "delta_mwh": "-2", "delta_cny": "-900", "reason": "表计修正",
        })
        # 提交人不能复核自己的改账。
        with self.assertRaises(Forbidden):
            self.service.review_adjustment("st1", adjustment["adjustment_id"], True, "自审")
        # 场站角色没有复核权限。
        with self.assertRaises(Forbidden):
            self.service.review_adjustment("st2", adjustment["adjustment_id"], True, "他站复核")
        review = self.service.review_adjustment("ops", adjustment["adjustment_id"], True, "属实")
        self.assertEqual(review["new_revision"], before + 1)
        row = self.connection.execute(
            "SELECT settled_mwh,settled_cny,revision FROM revenue_applications WHERE application_id=?",
            (target,),
        ).fetchone()
        self.assertEqual(row["settled_mwh"], "98.000")
        self.assertEqual(row["settled_cny"], "44100.00")
        self.assertEqual(row["revision"], before + 1)
        # 重复申请返回原受理结果。
        again = self.service.request_adjustment("st1", target, {
            "idempotency_key": "adj1", "delta_mwh": "-2", "delta_cny": "-900", "reason": "表计修正",
        })
        self.assertEqual(again, {"adjustment_id": adjustment["adjustment_id"], "state": "pending"})
        # 复核一次性有效。
        with self.assertRaises(InvalidState):
            self.service.review_adjustment("ops", adjustment["adjustment_id"], False, "重复")

    def test_adjustment_rejection_leaves_application_untouched(self) -> None:
        self._freeze_and_calculate()
        confirmed = self.service.confirm_measurement("ops", "v1", CONFIRM_PAYLOAD)
        target = confirmed["applications"][0]["application_id"]
        before = self.connection.execute(
            "SELECT settled_mwh,revision FROM revenue_applications WHERE application_id=?", (target,)
        ).fetchone()
        adjustment = self.service.request_adjustment("st1", target, {
            "idempotency_key": "adj-r", "delta_mwh": "5", "delta_cny": "0", "reason": "存疑",
        })
        review = self.service.review_adjustment("ops", adjustment["adjustment_id"], False, "依据不足")
        self.assertEqual(review["state"], "rejected")
        after = self.connection.execute(
            "SELECT settled_mwh,revision FROM revenue_applications WHERE application_id=?", (target,)
        ).fetchone()
        self.assertEqual(dict(after), dict(before))

    def test_adjustment_cannot_make_balance_negative(self) -> None:
        self._freeze_and_calculate()
        confirmed = self.service.confirm_measurement("ops", "v1", CONFIRM_PAYLOAD)
        target = confirmed["applications"][0]["application_id"]
        with self.assertRaises(ValidationFailed):
            self.service.request_adjustment("st1", target, {
                "idempotency_key": "adj-x", "delta_mwh": "-99999", "delta_cny": "0", "reason": "x",
            })

    # ── 三视图与可解释性 ───────────────────────────────────────

    def test_views_hide_unauthorized_fields_and_isolate_stations(self) -> None:
        self._freeze_and_calculate()
        self.service.confirm_measurement("ops", "v1", CONFIRM_PAYLOAD)
        station = self.service.station_view("st1", CYCLE)
        self.assertTrue(all("cap_amount" not in s for s in station["sources"]))
        self.assertTrue(all("price_slots" not in s for s in station["sources"]))
        self.assertTrue(all("amount_cny" not in a for a in station["applications"]))
        other = self.service.station_view("st2", CYCLE)
        self.assertEqual(other["applications"], [])
        self.assertEqual(other["measurements"], [])
        operations = self.service.operations_view("ops", CYCLE)
        gp = next(s for s in operations["sources"] if s["source_id"] == "gp")
        self.assertIn("remaining_cap_mwh", gp)
        cc = next(s for s in operations["sources"] if s["source_id"] == "cc")
        self.assertEqual(cc["consumed_mwh"], "15.000")
        self.assertEqual(cc["remaining_cap_mwh"], "485.000")
        with self.assertRaises(Forbidden):
            self.service.audit_view("ops", CYCLE)
        audit = self.service.audit_view("aud", CYCLE)
        self.assertTrue(audit["manual_adjustments"] == [])
        self.assertEqual(len(audit["measurements"]), 1)

    def test_explain_states_why_revenue_landed_on_source(self) -> None:
        self._freeze_and_calculate()
        confirmed = self.service.confirm_measurement("ops", "v1", CONFIRM_PAYLOAD)
        loss_id = confirmed["loss_applications"][0]["application_id"]
        explanation = self.service.explain_application("ops", loss_id)
        joined = "\n".join(explanation["reasons"])
        self.assertIn("CURTAILMENT_COMPENSATION", joined)
        self.assertIn("有效期", joined)
        self.assertIn("确认的损失电量", joined)
        self.assertIn("120", joined)
        self.assertGreaterEqual(len(explanation["lifecycle"]), 1)
        # 场站能看本站解释但看不到金额；他站无权。
        station_view = self.service.explain_application("st1", loss_id)
        self.assertNotIn("amount_cny", station_view)
        with self.assertRaises(Forbidden):
            self.service.explain_application("st2", loss_id)
        with self.assertRaises(NotFound):
            self.service.explain_application("ops", 99999)

    def test_audit_chain_detects_tampering(self) -> None:
        self._freeze_and_calculate()
        self.assertTrue(self.service.audit_chain("aud")["valid"])
        self.connection.execute("UPDATE rev_audit_events SET payload_json='{}' WHERE event_id=1")
        self.assertFalse(self.service.audit_chain("aud")["valid"])
        with self.assertRaises(Forbidden):
            self.service.audit_chain("ops")


if __name__ == "__main__":
    unittest.main()
