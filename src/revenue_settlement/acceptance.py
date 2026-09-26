"""贯通来源登记、冻结分摊、额度预占、确认核销、撤销结转、人工改账与三视图的离线验收。"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .clock import FrozenClock
from .service import RevenueService


CYCLE = "2026-09"


def _sources() -> list[dict[str, object]]:
    return [
        {
            "source_id": "mkt", "name": "月度市场电费", "kind": "MARKET_ENERGY", "cycle_id": CYCLE,
            "eligible_unit_ids": [], "valid_from": "2026-09-01", "valid_to": "2026-09-30",
            "use_order": 10, "limit_type": "NONE",
            "price_slots": [
                {"slot_id": "peak", "label": "高峰时段", "starts_at": "2026-09-01T08:00:00Z",
                 "ends_at": "2026-09-01T20:00:00Z", "price_cny_per_mwh": "450", "eligible_unit_ids": []},
                {"slot_id": "valley", "label": "低谷时段", "starts_at": "2026-09-01T20:00:00Z",
                 "ends_at": "2026-09-02T08:00:00Z", "price_cny_per_mwh": "280", "eligible_unit_ids": []},
            ],
        },
        {
            "source_id": "gp", "name": "保障性收购", "kind": "GUARANTEED_PURCHASE", "cycle_id": CYCLE,
            "eligible_unit_ids": [], "valid_from": "2026-09-01", "valid_to": "2026-09-30",
            "use_order": 20, "limit_type": "MWH", "cap_amount": "100", "unit_price_cny_per_mwh": "430",
        },
        {
            "source_id": "gc", "name": "绿证收益", "kind": "GREEN_CERTIFICATE", "cycle_id": CYCLE,
            "eligible_unit_ids": ["U1", "U2"], "valid_from": "2026-09-01", "valid_to": "2026-09-30",
            "use_order": 30, "limit_type": "NONE", "unit_price_cny_per_mwh": "30",
        },
        {
            "source_id": "ps", "name": "调峰奖励", "kind": "PEAK_SHAVING", "cycle_id": CYCLE,
            "eligible_unit_ids": ["U2"], "valid_from": "2026-09-01", "valid_to": "2026-09-30",
            "use_order": 40, "limit_type": "NONE", "unit_price_cny_per_mwh": "15",
        },
        {
            "source_id": "cc", "name": "限电补偿", "kind": "CURTAILMENT_COMPENSATION", "cycle_id": CYCLE,
            "eligible_unit_ids": [], "valid_from": "2026-09-01", "valid_to": "2026-09-30",
            "use_order": 50, "limit_type": "MWH", "cap_amount": "500",
            "curtailment_rate_cny_per_mwh": "120",
        },
    ]


def run(workspace: Path) -> dict[str, object]:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    service = RevenueService(connection, FrozenClock(datetime(2026, 9, 26, 8, 0, tzinfo=timezone.utc)))
    for user_id, role, station in (
        ("station-fs1", "station", "FS1"),
        ("ops", "operations", None),
        ("audit", "auditor", None),
    ):
        service.create_user(user_id, user_id, role, station)
    for source in _sources():
        service.register_source("ops", source)

    service.freeze_measurement("station-fs1", {
        "version_id": "mv-2026-09", "cycle_id": CYCLE, "station_id": "FS1",
        "period_start": "2026-09-01T00:00:00Z", "period_end": "2026-09-30T23:59:59Z",
        "entries": [
            {"unit_id": "U1", "slot_id": "peak", "energy_mwh": "120", "curtailment_mwh": "10"},
            {"unit_id": "U1", "slot_id": "valley", "energy_mwh": "80", "curtailment_mwh": "0"},
            {"unit_id": "U2", "slot_id": "peak", "energy_mwh": "60", "curtailment_mwh": "5"},
        ],
    })
    settlement = service.calculate_settlement("ops", "mv-2026-09")
    # 同一兆瓦时只计一次：电量池来源按 use_order 占用，市场电费吃满后保障额度仅在余量上生效。
    pool_sources = {(line["source_id"], line["slot_id"]) for line in settlement["applications"]
                    if line["basis"] == "energy" and line["source_id"] in ("mkt", "gp")}
    assert ("mkt", "peak") in pool_sources and ("gp", "peak") not in pool_sources

    confirmed = service.confirm_measurement("ops", "mv-2026-09", {
        "idempotency_key": "confirm-2026-09",
        "actual_entries": [
            {"unit_id": "U1", "slot_id": "peak", "energy_mwh": "100", "curtailment_mwh": "10"},
            {"unit_id": "U1", "slot_id": "valley", "energy_mwh": "80", "curtailment_mwh": "0"},
            {"unit_id": "U2", "slot_id": "peak", "energy_mwh": "60", "curtailment_mwh": "5"},
        ],
    })
    # 部分确认：U1 高峰少 20 MWh，对应市场电费与绿证各释放 20 MWh；损失电量在确认后才计列补偿。
    released: dict[str, str] = {}
    for row in confirmed["applications"]:
        if row["unit_id"] == "U1" and row["slot_id"] == "peak" and float(row["released_mwh"]) > 0:
            released[row["source_id"]] = row["released_mwh"]
    assert released == {"mkt": "20.000", "gc": "20.000"}
    assert {row["unit_id"]: row["settled_mwh"] for row in confirmed["loss_applications"]} == {"U1": "10.000", "U2": "5.000"}

    # 人工改账：场站申请、经营（不同人员）复核后产生新版本。
    target = confirmed["applications"][0]["application_id"]
    adjustment = service.request_adjustment("station-fs1", target, {
        "idempotency_key": "adj-1", "delta_mwh": "-1", "delta_cny": "-450", "reason": "考核表计修正",
    })
    review = service.review_adjustment("ops", adjustment["adjustment_id"], True, "复核属实")
    assert review["new_revision"] == 3

    station = service.station_view("station-fs1", CYCLE)
    operations = service.operations_view("ops", CYCLE)
    audit = service.audit_view("audit", CYCLE)
    explanation = service.explain_application(
        "ops", confirmed["loss_applications"][0]["application_id"]
    )
    result = {
        "status": "ok",
        "workspace": workspace.name,
        "settlement_run_id": settlement["run_id"],
        "reserved_lines": len(settlement["applications"]),
        "confirmed_energy_lines": len(confirmed["applications"]),
        "loss_lines": confirmed["loss_applications"],
        "adjustment_revision": review["new_revision"],
        "station_view_hidden_source_fields": [
            field for field in ("cap_amount", "unit_price_cny_per_mwh", "price_slots")
            if all(field not in source for source in station["sources"])
        ],
        "station_view_hidden_application_fields": [
            field for field in ("unit_price_cny", "amount_cny", "settled_cny")
            if all(field not in app for app in station["applications"])
        ],
        "operations_totals": operations["totals"],
        "audit_entities": {
            "sources": len(audit["sources"]),
            "measurements": len(audit["measurements"]),
            "applications": len(audit["applications"]),
            "manual_adjustments": len(audit["manual_adjustments"]),
        },
        "explanation": explanation["reasons"],
        "audit": service.audit_chain("audit"),
    }
    connection.close()
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行绿电收益与补偿联合归集离线验收")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    print(json.dumps(run(args.workspace), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
