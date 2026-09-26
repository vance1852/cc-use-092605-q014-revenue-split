"""贯通来源登记、冻结分摊、额度预占、核销结转、改账复核与视图的离线验收。"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .clock import FrozenClock
from .service import RevenueService


def run(workspace: Path) -> dict[str, object]:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    service = RevenueService(connection, FrozenClock(datetime(2026, 9, 20, 8, 0, tzinfo=timezone.utc)))
    service.create_user("station-east", "东场站", "station", "east")
    service.create_user("biz", "经营", "business")
    service.create_user("fin", "财务", "finance")
    service.create_user("audit", "审计", "auditor")
    service.register_unit("station-east", {"unit_id": "wtg-01", "station_id": "east", "name": "1号机组", "capacity_mw": "18"})
    service.register_unit("station-east", {"unit_id": "wtg-02", "station_id": "east", "name": "2号机组", "capacity_mw": "16"})
    service.register_period("station-east", {"period_id": "2026-09", "start_date": "2026-09-01", "end_date": "2026-09-30"})
    service.register_period("station-east", {"period_id": "2026-10", "start_date": "2026-10-01", "end_date": "2026-10-31"})

    # 同一发电周期的多张结算依据：批次、适用范围、有效期、上限与使用顺序。
    service.register_source("biz", {"source_id": "curt-0901", "source_type": "curtailment_comp", "usage_order": 5,
                                    "scope_unit_ids": "*", "valid_from": "2026-09-01", "valid_to": "2026-09-30",
                                    "cap_mwh": "500", "unit_price_cny": "0.30"})
    service.register_source("biz", {"source_id": "gtd-0901", "source_type": "guaranteed", "usage_order": 10,
                                    "scope_unit_ids": ["wtg-01"], "valid_from": "2026-09-01", "valid_to": "2026-09-30",
                                    "cap_mwh": "100", "unit_price_cny": "0.40"})
    service.register_source("biz", {"source_id": "grn-0901", "source_type": "green_certificate", "usage_order": 20,
                                    "scope_unit_ids": ["wtg-02"], "valid_from": "2026-09-01", "valid_to": "2026-09-30",
                                    "cap_mwh": "50", "unit_price_cny": "0.05"})
    service.register_source("biz", {"source_id": "peak-0901", "source_type": "peak_reward", "usage_order": 30,
                                    "scope_unit_ids": ["wtg-01"], "valid_from": "2026-09-01", "valid_to": "2026-09-30",
                                    "cap_mwh": "20", "unit_price_cny": "0.10"})
    service.register_source("biz", {"source_id": "mkt-0901", "source_type": "market", "usage_order": 40,
                                    "scope_unit_ids": "*", "valid_from": "2026-09-01", "valid_to": "2026-09-30",
                                    "time_prices": {"PEAK": "0.45", "FLAT": "0.38", "VALLEY": "0.25"}})

    # 冻结计量版本：限电损失确认前不参与分摊。
    service.freeze_metering("station-east", {"metering_version_id": "mv-2026-09-a", "period_id": "2026-09", "lines": [
        {"line_id": "ln-1", "unit_id": "wtg-01", "energy_kind": "generated", "time_bucket": "PEAK", "mwh": "50"},
        {"line_id": "ln-2", "unit_id": "wtg-02", "energy_kind": "generated", "time_bucket": "FLAT", "mwh": "40"},
        {"line_id": "ln-3", "unit_id": "wtg-01", "energy_kind": "curtailed", "time_bucket": "FLAT", "mwh": "10"},
    ]})
    service.confirm_loss("station-east", "mv-2026-09-a", ["ln-3"])
    apportion = service.apportion("biz", "mv-2026-09-a", "apportion-2026-09-a")
    replayed = service.apportion("biz", "mv-2026-09-a", "apportion-2026-09-a")

    # 计量确认后按实际电量核销：ln-1 实际 40，尾差 10 释放回保障性收购额度。
    settlement = service.confirm_metering(
        "biz", "mv-2026-09-a", {"ln-1": "40", "ln-2": "40", "ln-3": "10"}, "settle-2026-09-a")
    settlement_replay = service.confirm_metering(
        "biz", "mv-2026-09-a", {"ln-1": "40", "ln-2": "40", "ln-3": "10"}, "settle-2026-09-a")

    # 人工改账：经营申请、财务复核，生成新版本批次。
    adjustment = service.request_adjustment(
        "biz", {"run_id": apportion["run_id"], "reason": "计量复核后补记 5 MWh", "line_deltas": {"ln-1": "5"}})
    reviewed = service.review_adjustment("fin", adjustment["adjustment_id"], True)

    # 另一周期演示执行失败结转：8 月批次失败，预占结转到 10 月来源。
    service.register_period("station-east", {"period_id": "2026-08", "start_date": "2026-08-01", "end_date": "2026-08-31"})
    service.register_source("biz", {"source_id": "gtd-0801", "source_type": "guaranteed", "usage_order": 10,
                                    "scope_unit_ids": ["wtg-01"], "valid_from": "2026-08-01", "valid_to": "2026-08-31",
                                    "cap_mwh": "60", "unit_price_cny": "0.40"})
    service.register_source("biz", {"source_id": "gtd-1001", "source_type": "guaranteed", "usage_order": 10,
                                    "scope_unit_ids": ["wtg-01"], "valid_from": "2026-10-01", "valid_to": "2026-10-31",
                                    "cap_mwh": "30", "unit_price_cny": "0.41"})
    service.register_source("biz", {"source_id": "mkt-1001", "source_type": "market", "usage_order": 40,
                                    "scope_unit_ids": "*", "valid_from": "2026-10-01", "valid_to": "2026-10-31",
                                    "time_prices": {"PEAK": "0.46", "FLAT": "0.39", "VALLEY": "0.26"}})
    service.freeze_metering("station-east", {"metering_version_id": "mv-2026-08-a", "period_id": "2026-08", "lines": [
        {"line_id": "ln-9", "unit_id": "wtg-01", "energy_kind": "generated", "time_bucket": "FLAT", "mwh": "50"},
    ]})
    second = service.apportion("biz", "mv-2026-08-a", "apportion-2026-08-a")
    failover = service.fail_run("biz", second["run_id"], "2026-10", "failover-2026-08-a")

    result = {
        "status": "ok",
        "apportion_run": apportion["run_id"],
        "apportion_replayed": replayed["replayed"],
        "allocations": apportion["allocations"],
        "settlement_state": settlement["state"],
        "settlement_replayed": settlement_replay["replayed"],
        "released_mwh": settlement["released_mwh"],
        "adjustment_new_run": reviewed["new_run_id"],
        "failover_new_run": failover["new_run_id"],
        "quota_gtd": service.source_quota("biz", "gtd-0901")["balance_mwh"],
        "station_explanation": service.explain_revenue("station-east", "ln-1"),
        "business_explanation": service.explain_revenue("biz", "ln-1"),
        "audit": service.audit_chain("audit"),
        "workspace": workspace.name,
    }
    connection.close()
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行统一收益归集服务离线验收")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    print(json.dumps(run(args.workspace), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
