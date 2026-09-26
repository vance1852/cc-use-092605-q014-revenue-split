"""收益归集领域用例：来源登记、冻结分摊、额度预占、核销结转、改账复核与视图。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from decimal import Decimal
from typing import Any, Mapping

from .contracts import (
    AdjustmentInput,
    MeteringVersionInput,
    PeriodRegistration,
    SourceRegistration,
    UnitRegistration,
)
from .engine import (
    Allocation,
    DemandLine,
    ReservationSlice,
    SourceCandidate,
    ZERO,
    apportion,
    canonical_json,
    carry_forward,
    decimal_text,
    digest,
    quantize_money,
    quantize_volume,
)
from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .storage import initialize, transaction


ROLE_PERMISSIONS = {
    # 场站：登记机组、冻结与确认计量、确认限电损失
    "station": {
        "unit.write", "period.write", "metering.freeze", "metering.confirm",
        "report.read",
    },
    # 经营：登记结算依据、执行分摊、核销、撤销、结转与改账申请
    "business": {
        "source.write", "reservation.run", "settlement.write", "reservation.cancel",
        "reservation.fail", "carry.run", "adjustment.request", "report.read",
    },
    # 财务：复核改账
    "finance": {"adjustment.review", "report.read"},
    # 审计：只读全量与哈希链
    "auditor": {"report.read", "audit.read"},
}

# 台账动作对在占额度的符号：预占/转入增加占用，释放/转出减少占用，核销不返还。
LEDGER_SIGN = {
    "reserve": Decimal("1"),
    "carry_in": Decimal("1"),
    "release": Decimal("-1"),
    "carry_out": Decimal("-1"),
    "settle": ZERO,
}

# 各角色可见字段（最小授权）。"*" 表示全量。
VIEW_FIELDS = {
    # 场站看不到单价与金额等商务敏感字段，只看电量与落账理由。
    "station": {"unit_id", "station_id", "energy_kind", "time_bucket", "mwh", "source_id",
                "source_type", "reserved_mwh", "state", "reason", "skipped", "period_id", "line_id"},
    "business": {"*"},
    "finance": {"run_id", "source_id", "source_type", "reserved_mwh", "settled_mwh", "released_mwh",
                "unit_price_cny", "amount_cny", "state", "reason", "period_id", "adjustment"},
    "auditor": {"*"},
}


class RevenueService:
    def __init__(self, connection: sqlite3.Connection, clock=None) -> None:
        self.connection = connection
        self._clock = clock
        initialize(connection)

    def _now(self) -> str:
        if self._clock is None:
            from .clock import SystemClock

            self._clock = SystemClock()
        from .clock import utc_text

        return utc_text(self._clock.now())

    # ---- 用户、权限与审计 -------------------------------------------------

    def create_user(
        self, user_id: str, display_name: str, role: str, station_id: str | None = None
    ) -> dict[str, Any]:
        if role not in ROLE_PERMISSIONS:
            raise ValidationFailed("未知角色")
        if not user_id.strip() or not display_name.strip():
            raise ValidationFailed("用户编号和名称不能为空")
        if role == "station" and not station_id:
            raise ValidationFailed("场站账号必须归属场站")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO revenue_users(user_id,display_name,role,station_id,created_at) "
                    "VALUES(?,?,?,?,?)",
                    (user_id.strip(), display_name.strip(), role, station_id, self._now()),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("用户已经存在") from exc
        return {"user_id": user_id.strip(), "role": role, "station_id": station_id}

    def _user(self, user_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM revenue_users WHERE user_id=?", (user_id,)
        ).fetchone()
        if row is None:
            raise NotFound("用户不存在")
        if not row["active"]:
            raise Forbidden("用户已停用")
        return row

    def _require(self, user_id: str, permission: str) -> sqlite3.Row:
        user = self._user(user_id)
        if permission not in ROLE_PERMISSIONS[user["role"]]:
            raise Forbidden(f"角色 {user['role']} 无权执行 {permission}")
        return user

    def _audit(
        self,
        entity_type: str,
        entity_id: str,
        event_type: str,
        actor_id: str,
        payload: Mapping[str, Any],
    ) -> None:
        previous = self.connection.execute(
            "SELECT event_hash FROM revenue_audit_events ORDER BY event_id DESC LIMIT 1"
        ).fetchone()
        previous_hash = "0" * 64 if previous is None else previous["event_hash"]
        body = {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "event_type": event_type,
            "actor_id": actor_id,
            "payload": payload,
            "created_at": self._now(),
            "previous_hash": previous_hash,
        }
        event_hash = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
        self.connection.execute(
            "INSERT INTO revenue_audit_events(entity_type,entity_id,event_type,actor_id,payload_json,"
            "previous_hash,event_hash,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                entity_type,
                entity_id,
                event_type,
                actor_id,
                canonical_json(payload),
                previous_hash,
                event_hash,
                body["created_at"],
            ),
        )

    # ---- 基础登记：机组、周期、来源批次 -----------------------------------

    def register_unit(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        actor = self._require(actor_id, "unit.write")
        unit = UnitRegistration.from_dict(raw)
        if actor["station_id"] is not None and actor["station_id"] != unit.station_id:
            raise Forbidden("场站账号只能登记本场站机组")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO units(unit_id,station_id,name,capacity_mw,created_at) VALUES(?,?,?,?,?)",
                    (unit.unit_id, unit.station_id, unit.name, decimal_text(unit.capacity_mw), self._now()),
                )
                self._audit("unit", unit.unit_id, "unit.registered", actor_id, {"station_id": unit.station_id})
        except sqlite3.IntegrityError as exc:
            raise Conflict("机组编号已经存在") from exc
        return {"unit_id": unit.unit_id, "station_id": unit.station_id}

    def register_period(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "period.write")
        period = PeriodRegistration.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO periods(period_id,start_date,end_date,created_at) VALUES(?,?,?,?)",
                    (period.period_id, period.start_date, period.end_date, self._now()),
                )
                self._audit(
                    "period", period.period_id, "period.registered", actor_id,
                    {"start_date": period.start_date, "end_date": period.end_date},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("周期编号或日期区间已经存在") from exc
        return {"period_id": period.period_id, "start_date": period.start_date, "end_date": period.end_date}

    def register_source(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "source.write")
        source = SourceRegistration.from_dict(raw)
        for unit_id in source.scope_unit_ids:
            if self.connection.execute("SELECT 1 FROM units WHERE unit_id=?", (unit_id,)).fetchone() is None:
                raise ValidationFailed(f"适用机组不存在: {unit_id}")
        scope_text = ",".join(source.scope_unit_ids)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO revenue_sources(source_id,source_type,usage_order,scope_unit_ids,"
                    "valid_from,valid_to,cap_mwh,unit_price_cny,time_prices_json,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        source.source_id,
                        source.source_type,
                        source.usage_order,
                        scope_text,
                        source.valid_from,
                        source.valid_to,
                        None if source.cap_mwh is None else decimal_text(source.cap_mwh),
                        None if source.unit_price_cny is None else decimal_text(source.unit_price_cny),
                        canonical_json({key: decimal_text(value) for key, value in source.time_prices.items()}),
                        actor_id,
                        self._now(),
                    ),
                )
                self._audit(
                    "source", source.source_id, "source.registered", actor_id,
                    {
                        "source_type": source.source_type,
                        "usage_order": source.usage_order,
                        "scope": scope_text or "*",
                        "valid_from": source.valid_from,
                        "valid_to": source.valid_to,
                        "cap_mwh": None if source.cap_mwh is None else decimal_text(source.cap_mwh),
                    },
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("来源编号已经存在") from exc
        return self.source(source.source_id)

    def source(self, source_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM revenue_sources WHERE source_id=?", (source_id,)
        ).fetchone()
        if row is None:
            raise NotFound("结算依据来源不存在")
        data = dict(row)
        data["time_prices"] = json.loads(data.pop("time_prices_json"))
        data["scope_unit_ids"] = [unit for unit in data["scope_unit_ids"].split(",") if unit]
        return data

    # ---- 额度台账 ---------------------------------------------------------

    def _occupied(self, source_id: str) -> Decimal:
        row = self.connection.execute(
            "SELECT COALESCE(SUM(CAST(delta_mwh AS REAL)),0) AS used "
            "FROM source_quota_ledger WHERE source_id=?",
            (source_id,),
        ).fetchone()
        return quantize_volume(Decimal(str(row["used"])))

    def _quota_balance(self, source_id: str) -> Decimal | None:
        row = self.connection.execute(
            "SELECT cap_mwh FROM revenue_sources WHERE source_id=?", (source_id,)
        ).fetchone()
        if row is None or row["cap_mwh"] is None:
            return None
        return quantize_volume(Decimal(row["cap_mwh"]) - self._occupied(source_id))

    def _ledger(self, source_id: str, reservation_id: str | None, run_id: int | None,
                movement: str, mwh: Decimal, actor_id: str, note: str = "") -> None:
        """登记一条额度台账。mwh 始终为非负数量，符号由动作决定。"""
        if mwh < ZERO:
            raise ValueError("台账数量不能为负")
        cap_row = self.connection.execute(
            "SELECT cap_mwh FROM revenue_sources WHERE source_id=?", (source_id,)
        ).fetchone()
        signed = quantize_volume(mwh) * LEDGER_SIGN[movement]
        if cap_row is not None and cap_row["cap_mwh"] is not None:
            balance = quantize_volume(Decimal(cap_row["cap_mwh"]) - self._occupied(source_id) - signed)
            balance_text = decimal_text(balance)
        else:
            balance_text = ""
        self.connection.execute(
            "INSERT INTO source_quota_ledger(source_id,reservation_id,run_id,movement,delta_mwh,"
            "balance_mwh,actor_id,note,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (source_id, reservation_id, run_id, movement, decimal_text(signed),
             balance_text, actor_id, note, self._now()),
        )

    # ---- 计量冻结与损失确认 ----------------------------------------------

    def freeze_metering(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        actor = self._require(actor_id, "metering.freeze")
        version = MeteringVersionInput.from_dict(raw)
        period = self.connection.execute(
            "SELECT * FROM periods WHERE period_id=?", (version.period_id,)
        ).fetchone()
        if period is None:
            raise NotFound("发电周期不存在")
        if period["state"] != "open":
            raise InvalidState("周期已关闭，不能再冻结计量版本")
        for line in version.lines:
            unit = self.connection.execute(
                "SELECT station_id FROM units WHERE unit_id=?", (line.unit_id,)
            ).fetchone()
            if unit is None:
                raise ValidationFailed(f"计量行机组不存在: {line.unit_id}")
            if actor["station_id"] is not None and actor["station_id"] != unit["station_id"]:
                raise Forbidden("场站账号只能冻结本场站机组的计量")
        content = canonical_json([
            {"line_id": line.line_id, "unit_id": line.unit_id, "energy_kind": line.energy_kind,
             "time_bucket": line.time_bucket, "mwh": decimal_text(line.mwh)}
            for line in sorted(version.lines, key=lambda item: item.line_id)
        ])
        content_sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
        prior = self.connection.execute(
            "SELECT metering_version_id,state FROM metering_versions WHERE period_id=? ORDER BY rowid",
            (version.period_id,),
        ).fetchall()
        if any(row["state"] == "confirmed" for row in prior):
            raise InvalidState("周期已有确认计量版本，应走人工改账流程")
        if prior:
            pending = self.connection.execute(
                "SELECT r.run_id FROM allocation_runs r WHERE r.metering_version_id=? "
                "AND r.state='reserved' LIMIT 1",
                (prior[-1]["metering_version_id"],),
            ).fetchone()
            if pending is not None:
                raise InvalidState("上一冻结版本存在未核销预占批次，请先撤销或处理后再冻结新版本")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO metering_versions(metering_version_id,period_id,content_sha256,"
                    "supersedes_version_id,submitted_by,submitted_at) VALUES(?,?,?,?,?,?)",
                    (
                        version.metering_version_id,
                        version.period_id,
                        content_sha256,
                        prior[-1]["metering_version_id"] if prior else None,
                        actor_id,
                        self._now(),
                    ),
                )
                for line in version.lines:
                    self.connection.execute(
                        "INSERT INTO metering_lines(metering_version_id,line_id,unit_id,energy_kind,"
                        "time_bucket,mwh,loss_confirmed) VALUES(?,?,?,?,?,?,?)",
                        (
                            version.metering_version_id,
                            line.line_id,
                            line.unit_id,
                            line.energy_kind,
                            line.time_bucket,
                            decimal_text(line.mwh),
                            1 if line.energy_kind == "generated" else 0,
                        ),
                    )
                if prior:
                    self.connection.execute(
                        "UPDATE metering_versions SET state='superseded' "
                        "WHERE metering_version_id=? AND state='frozen'",
                        (prior[-1]["metering_version_id"],),
                    )
                self._audit(
                    "metering_version", version.metering_version_id, "metering.frozen", actor_id,
                    {"period_id": version.period_id, "sha256": content_sha256, "lines": len(version.lines)},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("计量版本编号或内容重复") from exc
        return {
            "metering_version_id": version.metering_version_id,
            "period_id": version.period_id,
            "state": "frozen",
            "sha256": content_sha256,
        }

    def confirm_loss(self, actor_id: str, metering_version_id: str, line_ids: list[str]) -> dict[str, Any]:
        """确认限电损失电量；限电补偿只承接已确认的损失。"""
        self._require(actor_id, "metering.confirm")
        version = self._metering_version(metering_version_id)
        if version["state"] != "frozen":
            raise InvalidState("只有冻结态计量版本可以确认损失")
        if not line_ids:
            raise ValidationFailed("line_ids 不能为空")
        confirmed: list[str] = []
        with transaction(self.connection, immediate=True):
            for line_id in line_ids:
                row = self.connection.execute(
                    "SELECT * FROM metering_lines WHERE metering_version_id=? AND line_id=?",
                    (metering_version_id, line_id),
                ).fetchone()
                if row is None:
                    raise NotFound(f"计量行不存在: {line_id}")
                if row["energy_kind"] != "curtailed":
                    raise ValidationFailed(f"只有限电损失电量可以确认损失: {line_id}")
                if not row["loss_confirmed"]:
                    self.connection.execute(
                        "UPDATE metering_lines SET loss_confirmed=1 WHERE metering_version_id=? AND line_id=?",
                        (metering_version_id, line_id),
                    )
                    confirmed.append(line_id)
            self._audit(
                "metering_version", metering_version_id, "loss.confirmed", actor_id,
                {"lines": confirmed},
            )
        return {"metering_version_id": metering_version_id, "confirmed_lines": confirmed}

    def _metering_version(self, version_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM metering_versions WHERE metering_version_id=?", (version_id,)
        ).fetchone()
        if row is None:
            raise NotFound("计量版本不存在")
        return row

    def _period(self, period_id: str) -> sqlite3.Row:
        row = self.connection.execute("SELECT * FROM periods WHERE period_id=?", (period_id,)).fetchone()
        if row is None:
            raise NotFound("发电周期不存在")
        return row

    # ---- 冻结版本分摊与额度预占 ------------------------------------------

    def _active_sources_for(self, period: sqlite3.Row) -> list[sqlite3.Row]:
        return self.connection.execute(
            "SELECT * FROM revenue_sources WHERE state='active' "
            "AND valid_from<=? AND valid_to>=? ORDER BY usage_order,source_id",
            (period["end_date"], period["start_date"]),
        ).fetchall()

    def _source_candidate(self, row: sqlite3.Row) -> SourceCandidate:
        scope = tuple(unit for unit in row["scope_unit_ids"].split(",") if unit)
        return SourceCandidate(
            source_id=row["source_id"],
            source_type=row["source_type"],
            usage_order=int(row["usage_order"]),
            scope_unit_ids=scope,
            valid_from=row["valid_from"],
            valid_to=row["valid_to"],
            cap_mwh=None if row["cap_mwh"] is None else Decimal(row["cap_mwh"]),
            available_mwh=self._quota_balance(row["source_id"]),
            unit_price_cny=None if row["unit_price_cny"] is None else Decimal(row["unit_price_cny"]),
            time_prices={key: Decimal(value) for key, value in json.loads(row["time_prices_json"]).items()},
        )

    def apportion(self, actor_id: str, metering_version_id: str, idempotency_key: str) -> dict[str, Any]:
        self._require(actor_id, "reservation.run")
        response = self._idempotent("apportion", idempotency_key,
                                    {"metering_version_id": metering_version_id})
        if response is not None:
            return response
        version = self._metering_version(metering_version_id)
        if version["state"] != "frozen":
            raise InvalidState("只能对冻结态计量版本执行分摊")
        # 同一版本同一时刻只允许一个待核销批次，杜绝同一兆瓦时被重复预占。
        active = self.connection.execute(
            "SELECT run_id FROM allocation_runs WHERE metering_version_id=? AND state='reserved'",
            (metering_version_id,),
        ).fetchone()
        if active is not None:
            return self._replay_run(int(active["run_id"]))
        # 已结转的版本电量已随失败批次转移，禁止再次分摊造成重复计量。
        carried = self.connection.execute(
            "SELECT run_id FROM allocation_runs WHERE metering_version_id=? AND state='carried'",
            (metering_version_id,),
        ).fetchone()
        if carried is not None:
            raise InvalidState("该计量版本的电量已随执行失败批次结转，不能再次分摊")
        period = self._period(version["period_id"])
        lines_rows = self.connection.execute(
            "SELECT * FROM metering_lines WHERE metering_version_id=? ORDER BY line_id",
            (metering_version_id,),
        ).fetchall()
        source_rows = self._active_sources_for(period)
        if not source_rows:
            raise InvalidState("周期内没有有效结算依据来源")
        candidates = [self._source_candidate(row) for row in source_rows]
        demand = [
            DemandLine(
                line_id=row["line_id"],
                unit_id=row["unit_id"],
                energy_kind=row["energy_kind"],
                time_bucket=row["time_bucket"],
                mwh=Decimal(row["mwh"]),
                loss_confirmed=bool(row["loss_confirmed"]),
            )
            for row in lines_rows
        ]
        result = apportion(demand, candidates, period_start=period["start_date"],
                           period_end=period["end_date"])
        if result["pending_loss"]:
            raise InvalidState(
                "存在尚未确认损失的限电电量，请先确认损失再分摊: "
                + ",".join(item["line_id"] for item in result["pending_loss"])
            )
        allocations: list[Allocation] = result["allocations"]  # type: ignore[assignment]
        input_value = {
            "content_sha256": version["content_sha256"],
            "lines": [dict(row) for row in lines_rows],
            "sources": [
                {"source_id": c.source_id, "type": c.source_type, "order": c.usage_order,
                 "scope": c.scope_unit_ids, "valid_from": c.valid_from, "valid_to": c.valid_to,
                 "balance": None if c.available_mwh is None else decimal_text(c.available_mwh),
                 "unit_price": None if c.unit_price_cny is None else decimal_text(c.unit_price_cny),
                 "time_prices": {k: decimal_text(v) for k, v in c.time_prices.items()}}
                for c in candidates
            ],
        }
        input_sha256 = digest(input_value)
        # 同输入且仍有效的批次直接回放；已撤销/已失败的同输入批次不阻挡重新分摊。
        existing = self.connection.execute(
            "SELECT run_id FROM allocation_runs WHERE metering_version_id=? AND input_sha256=? "
            "AND state NOT IN ('cancelled','failed') ORDER BY run_id DESC LIMIT 1",
            (metering_version_id, input_sha256),
        ).fetchone()
        if existing is not None:
            return self._replay_run(int(existing["run_id"]))

        result_payload = {
            "metering_version_id": metering_version_id,
            "period_id": period["period_id"],
            "allocations": [
                {
                    "line_id": item.line_id,
                    "source_id": item.source_id,
                    "mwh": decimal_text(item.mwh),
                    "unit_price_cny": decimal_text(item.unit_price_cny),
                    "amount_cny": decimal_text(item.amount_cny),
                    "reason": item.reason,
                }
                for item in allocations
            ],
            "pending_loss": result["pending_loss"],
            "explanations": result["explanations"],
        }
        with transaction(self.connection, immediate=True):
            self._assert_quota_sufficient(allocations)
            cursor = self.connection.execute(
                "INSERT INTO allocation_runs(metering_version_id,period_id,input_sha256,result_json,"
                "created_by,created_at) VALUES(?,?,?,?,?,?)",
                (metering_version_id, period["period_id"], input_sha256,
                 canonical_json(result_payload), actor_id, self._now()),
            )
            run_id = int(cursor.lastrowid)
            for index, item in enumerate(allocations, start=1):
                reservation_id = f"rsv-{run_id}-{index}"
                line = next(row for row in lines_rows if row["line_id"] == item.line_id)
                self.connection.execute(
                    "INSERT INTO reservations(reservation_id,run_id,metering_version_id,period_id,line_id,"
                    "source_id,unit_id,energy_kind,reserved_mwh,unit_price_cny,amount_cny,reason,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        reservation_id, run_id, metering_version_id, period["period_id"], item.line_id,
                        item.source_id, line["unit_id"], line["energy_kind"], decimal_text(item.mwh),
                        decimal_text(item.unit_price_cny), decimal_text(item.amount_cny), item.reason, self._now(),
                    ),
                )
                self._ledger(item.source_id, reservation_id, run_id, "reserve", item.mwh, actor_id,
                             "冻结计量分摊预占")
            self._audit(
                "allocation_run", str(run_id), "apportion.reserved", actor_id,
                {"metering_version_id": metering_version_id, "reservations": len(allocations),
                 "input_sha256": input_sha256},
            )
            response = {"run_id": run_id, "state": "reserved", **result_payload, "replayed": False}
            self._store_idempotent("apportion", idempotency_key,
                                   {"metering_version_id": metering_version_id}, response)
        return response

    def _replay_run(self, run_id: int) -> dict[str, Any]:
        run = self._run(run_id)
        return {"run_id": run_id, "state": run["state"], **json.loads(run["result_json"]), "replayed": True}

    def _assert_quota_sufficient(self, allocations: list[Allocation]) -> None:
        totals: dict[str, Decimal] = {}
        for item in allocations:
            totals[item.source_id] = totals.get(item.source_id, ZERO) + item.mwh
        for source_id, need in totals.items():
            balance = self._quota_balance(source_id)
            if balance is not None and need > balance:
                raise Conflict(
                    f"来源 {source_id} 剩余额度 {decimal_text(balance)} 不足 {decimal_text(quantize_volume(need))}"
                )

    def _run(self, run_id: int) -> sqlite3.Row:
        row = self.connection.execute("SELECT * FROM allocation_runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise NotFound("分摊批次不存在")
        return row

    def _reservations(self, run_id: int, *, states: tuple[str, ...] | None = None) -> list[sqlite3.Row]:
        sql = "SELECT * FROM reservations WHERE run_id=? ORDER BY reservation_id"
        params: list[Any] = [run_id]
        if states:
            placeholders = ",".join("?" for _ in states)
            sql += f" AND state IN ({placeholders})"
            params.extend(states)
        return self.connection.execute(sql, params).fetchall()

    # ---- 计量确认后核销 ---------------------------------------------------

    def confirm_metering(
        self, actor_id: str, metering_version_id: str, actual_lines: Mapping[str, str],
        idempotency_key: str
    ) -> dict[str, Any]:
        """计量确认：按各来源预占电量核销。

        实际少于预占的尾差释放回来源额度；实际多于冻结电量的部分标记未分配。
        重复核销请求返回原结果；同一复用编号提交不同内容时拒绝。
        """
        self._require(actor_id, "settlement.write")
        version = self._metering_version(metering_version_id)
        run_row = self.connection.execute(
            "SELECT * FROM allocation_runs WHERE metering_version_id=? "
            "ORDER BY CASE state WHEN 'reserved' THEN 0 ELSE 1 END, run_id DESC LIMIT 1",
            (metering_version_id,),
        ).fetchone()
        if run_row is None:
            raise InvalidState("该计量版本没有分摊批次")
        run_id = int(run_row["run_id"])
        # 幂等锚定复用编号与业务内容（版本+实际电量），与内部批次号无关。
        request = {
            "metering_version_id": metering_version_id,
            "actual_lines": {key: decimal_text(Decimal(str(value))) for key, value in
                             sorted(actual_lines.items())},
        }
        # 幂等判定优先于状态：重复请求回放原结果，同号不同内容直接拒绝。
        response = self._idempotent("settlement", idempotency_key, request)
        if response is not None:
            return response
        if version["state"] != "frozen":
            raise InvalidState("只有冻结态计量版本可以确认核销")
        if run_row["state"] != "reserved":
            raise InvalidState("该计量版本没有待核销的预占分摊批次")
        frozen_lines = {
            row["line_id"]: Decimal(row["mwh"])
            for row in self.connection.execute(
                "SELECT line_id,mwh FROM metering_lines WHERE metering_version_id=?",
                (metering_version_id,),
            ).fetchall()
        }
        unknown = set(request["actual_lines"]) - set(frozen_lines)
        if unknown:
            raise ValidationFailed(f"实际电量包含未知计量行: {','.join(sorted(unknown))}")
        missing = set(frozen_lines) - set(request["actual_lines"])
        if missing:
            raise ValidationFailed(f"缺少计量行实际电量: {','.join(sorted(missing))}")

        reserved_rows = self._reservations(run_id, states=("reserved",))
        slices: list[tuple[sqlite3.Row, Decimal, Decimal]] = []
        released_total = ZERO
        unallocated_total = ZERO
        for row in reserved_rows:
            frozen = frozen_lines[row["line_id"]]
            actual = Decimal(request["actual_lines"][row["line_id"]])
            if actual < ZERO:
                raise ValidationFailed("实际电量不能为负数")
            reserved = Decimal(row["reserved_mwh"])
            settled_mwh = quantize_volume(reserved * min(actual, frozen) / frozen) if frozen else ZERO
            released = quantize_volume(reserved - settled_mwh)
            released_total += released
            unallocated_total += quantize_volume(max(ZERO, actual - frozen))
            slices.append((row, settled_mwh, released))
        run_state = "partially_settled" if released_total > ZERO or unallocated_total > ZERO else "settled"

        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "INSERT INTO settlements(run_id,actual_version_id,actual_mwh,kind,idempotency_key,"
                "actor_id,created_at) VALUES(?,?,?,?,?,?,?)",
                (
                    run_id, metering_version_id,
                    decimal_text(sum((Decimal(value) for value in request["actual_lines"].values()), ZERO)),
                    "partial" if run_state == "partially_settled" else "full",
                    idempotency_key, actor_id, self._now(),
                ),
            )
            settlement_id = int(cursor.lastrowid)
            for row, settled_mwh, released in slices:
                self.connection.execute(
                    "INSERT INTO settlement_slices(settlement_id,reservation_id,source_id,settled_mwh,released_mwh) "
                    "VALUES(?,?,?,?,?)",
                    (settlement_id, row["reservation_id"], row["source_id"],
                     decimal_text(settled_mwh), decimal_text(released)),
                )
                self.connection.execute(
                    "UPDATE reservations SET state=? WHERE reservation_id=?",
                    ("partially_settled" if released > ZERO else "settled", row["reservation_id"]),
                )
                if settled_mwh > ZERO:
                    # 核销不返还额度：预占占用转为已消耗占用。
                    self._ledger(row["source_id"], row["reservation_id"], run_id, "settle",
                                 settled_mwh, actor_id, f"计量确认核销 {decimal_text(settled_mwh)} MWh")
                if released > ZERO:
                    self._ledger(row["source_id"], row["reservation_id"], run_id, "release",
                                 released, actor_id, "部分确认释放尾差")
            self.connection.execute(
                "UPDATE allocation_runs SET state=? WHERE run_id=?", (run_state, run_id)
            )
            self.connection.execute(
                "UPDATE metering_versions SET state=?,confirmed_at=? WHERE metering_version_id=?",
                ("confirmed", self._now(), metering_version_id),
            )
            payload = {
                "settlement_id": settlement_id,
                "run_id": run_id,
                "metering_version_id": metering_version_id,
                "state": run_state,
                "released_mwh": decimal_text(quantize_volume(released_total)),
                "unallocated_mwh": decimal_text(quantize_volume(unallocated_total)),
                "slices": [
                    {"reservation_id": row["reservation_id"], "source_id": row["source_id"],
                     "settled_mwh": decimal_text(settled), "released_mwh": decimal_text(released)}
                    for row, settled, released in slices
                ],
            }
            self._store_idempotent("settlement", idempotency_key, request, payload)
            self._audit("settlement", str(settlement_id), "metering.confirmed", actor_id,
                        {"run_id": run_id, "state": run_state,
                         "released_mwh": payload["released_mwh"],
                         "unallocated_mwh": payload["unallocated_mwh"]})
        payload["replayed"] = False
        return payload

    # ---- 撤销与执行失败：释放或结转 --------------------------------------

    def cancel_run(self, actor_id: str, run_id: int) -> dict[str, Any]:
        """整批撤销：全部预占释放回来源额度。仅 reserved 批次可撤销。"""
        self._require(actor_id, "reservation.cancel")
        run = self._run(run_id)
        if run["state"] != "reserved":
            raise InvalidState("只有未核销的预占批次可以撤销")
        rows = self._reservations(run_id, states=("reserved",))
        with transaction(self.connection, immediate=True):
            for row in rows:
                self.connection.execute(
                    "UPDATE reservations SET state='released' WHERE reservation_id=?",
                    (row["reservation_id"],),
                )
                self._ledger(row["source_id"], row["reservation_id"], run_id, "release",
                             Decimal(row["reserved_mwh"]), actor_id, "整批撤销释放")
            self.connection.execute(
                "UPDATE allocation_runs SET state='cancelled' WHERE run_id=?", (run_id,)
            )
            self._audit("allocation_run", str(run_id), "run.cancelled", actor_id,
                        {"released": len(rows)})
        return {"run_id": run_id, "state": "cancelled", "released_reservations": len(rows)}

    def _source_price(self, source_id: str) -> Decimal:
        row = self.connection.execute(
            "SELECT source_type,unit_price_cny,time_prices_json FROM revenue_sources WHERE source_id=?",
            (source_id,),
        ).fetchone()
        if row is None:
            raise NotFound("结算依据来源不存在")
        if row["source_type"] == "market":
            prices = json.loads(row["time_prices_json"])
            return Decimal(prices["FLAT"])
        return Decimal(row["unit_price_cny"])

    def fail_run(self, actor_id: str, run_id: int, target_period_id: str, idempotency_key: str) -> dict[str, Any]:
        """执行失败：未核销预占按目标周期来源规则结转，无来源承接部分释放。

        只允许结转到尚未确认（open）的周期；已确认周期的规则后续变更不影响本结果。
        """
        self._require(actor_id, "reservation.fail")
        run = self._run(run_id)
        request = {"run_id": run_id, "target_period_id": target_period_id}
        # 幂等判定优先于状态：重复请求回放原结果，同号不同内容直接拒绝。
        response = self._idempotent("failover", idempotency_key, request)
        if response is not None:
            return response
        if run["state"] != "reserved":
            raise InvalidState("只有未核销的预占批次可以标记执行失败")
        target = self._period(target_period_id)
        if target["state"] != "open":
            raise InvalidState("只能结转到尚未关闭确认的周期")
        rows = self._reservations(run_id, states=("reserved",))
        candidates = [self._source_candidate(row) for row in self._active_sources_for(target)]

        groups: dict[tuple[str, str], list[sqlite3.Row]] = {}
        for row in rows:
            groups.setdefault((row["unit_id"], row["energy_kind"]), []).append(row)
        outcomes: dict[tuple[str, str], dict[str, Any]] = {}
        for (unit_id, energy_kind), group_rows in groups.items():
            slices = [
                ReservationSlice(row["reservation_id"], row["source_id"], Decimal(row["reserved_mwh"]))
                for row in group_rows
            ]
            outcomes[(unit_id, energy_kind)] = carry_forward(
                slices, candidates,
                period_start=target["start_date"], period_end=target["end_date"],
                unit_id=unit_id, energy_kind=energy_kind,
            )

        quota_need: dict[str, Decimal] = {}
        for outcome in outcomes.values():
            for item in outcome["carried"]:
                quota_need[item["source_id"]] = (
                    quota_need.get(item["source_id"], ZERO) + Decimal(str(item["mwh"]))
                )
        for source_id, need in quota_need.items():
            balance = self._quota_balance(source_id)
            if balance is not None and need > balance:
                raise Conflict(f"目标来源 {source_id} 额度不足以承接结转")

        with transaction(self.connection, immediate=True):
            new_run_cursor = self.connection.execute(
                "INSERT INTO allocation_runs(metering_version_id,period_id,input_sha256,result_json,"
                "state,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                (run["metering_version_id"], target_period_id,
                 digest({"failover": request, "run_input": run["input_sha256"]}),
                 canonical_json({"origin_run_id": run_id, "failover": True}),
                 "carried", actor_id, self._now()),
            )
            new_run_id = int(new_run_cursor.lastrowid)
            carry_index = 0
            for (unit_id, energy_kind), outcome in outcomes.items():
                group_rows = groups[(unit_id, energy_kind)]
                carried_by_origin: dict[str, Decimal] = {}
                for item in outcome["carried"]:
                    origin_id = item["from_reservation_id"]
                    mwh = quantize_volume(Decimal(str(item["mwh"])))
                    carried_by_origin[origin_id] = carried_by_origin.get(origin_id, ZERO) + mwh
                    carry_index += 1
                    new_reservation_id = f"rsv-{new_run_id}-carry-{carry_index}"
                    price = self._source_price(item["source_id"])
                    self.connection.execute(
                        "INSERT INTO reservations(reservation_id,run_id,metering_version_id,period_id,line_id,"
                        "source_id,unit_id,energy_kind,reserved_mwh,unit_price_cny,amount_cny,reason,state,created_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            new_reservation_id, new_run_id, run["metering_version_id"], target_period_id,
                            f"carry:{origin_id}", item["source_id"], unit_id, energy_kind,
                            decimal_text(mwh), decimal_text(price),
                            decimal_text(quantize_money(mwh * price)),
                            f"由执行失败批次 {run_id} 结转", "carried", self._now(),
                        ),
                    )
                    # 转出原来源占用，转入目标来源占用。
                    origin = next(row for row in group_rows if row["reservation_id"] == origin_id)
                    self._ledger(origin["source_id"], origin_id, run_id, "carry_out", mwh,
                                 actor_id, f"结转至周期 {target_period_id} 来源 {item['source_id']}")
                    self._ledger(item["source_id"], new_reservation_id, new_run_id, "carry_in",
                                 mwh, actor_id, f"自批次 {run_id} 结转")
                for row in group_rows:
                    leftover = quantize_volume(
                        Decimal(row["reserved_mwh"]) - carried_by_origin.get(row["reservation_id"], ZERO)
                    )
                    if leftover > ZERO:
                        self.connection.execute(
                            "UPDATE reservations SET state='released' WHERE reservation_id=?",
                            (row["reservation_id"],),
                        )
                        self._ledger(row["source_id"], row["reservation_id"], run_id, "release",
                                     leftover, actor_id, "无目标来源承接，执行失败释放")
            self.connection.execute(
                "UPDATE allocation_runs SET state='failed' WHERE run_id=?", (run_id,)
            )
            payload = {
                "run_id": run_id,
                "state": "failed",
                "target_period_id": target_period_id,
                "new_run_id": new_run_id,
                "replayed": False,
            }
            self._store_idempotent("failover", idempotency_key, request, payload)
            self._audit("allocation_run", str(run_id), "run.failed_carried", actor_id,
                        {"target_period_id": target_period_id, "new_run_id": new_run_id})
        return payload

    # ---- 人工改账：不同人员复核并生成新版本 ------------------------------

    def request_adjustment(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "adjustment.request")
        adjustment = AdjustmentInput.from_dict(raw)
        run = self._run(adjustment.run_id)
        if run["state"] not in ("settled", "partially_settled", "adjusted"):
            raise InvalidState("只有已核销批次可以申请人工改账")
        existing = self.connection.execute(
            "SELECT adjustment_id FROM manual_adjustments WHERE run_id=? AND state='requested'",
            (adjustment.run_id,),
        ).fetchone()
        if existing is not None:
            raise InvalidState("该批次已有待复核改账申请")
        line_ids = {row["line_id"] for row in self._reservations(adjustment.run_id)}
        for line_id in adjustment.line_deltas:
            if line_id not in line_ids:
                raise ValidationFailed(f"改账计量行不属于该批次: {line_id}")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "INSERT INTO manual_adjustments(run_id,reason,requested_by,state,created_at) "
                "VALUES(?,?,?,'requested',?)",
                (adjustment.run_id, adjustment.reason, actor_id, self._now()),
            )
            adjustment_id = int(cursor.lastrowid)
            for line_id, delta in adjustment.line_deltas.items():
                self.connection.execute(
                    "INSERT INTO adjustment_items(adjustment_id,line_id,delta_mwh) VALUES(?,?,?)",
                    (adjustment_id, line_id, decimal_text(delta)),
                )
            self._audit("manual_adjustment", str(adjustment_id), "adjustment.requested", actor_id,
                        {"run_id": adjustment.run_id, "lines": len(adjustment.line_deltas)})
        return {"adjustment_id": adjustment_id, "run_id": adjustment.run_id, "state": "requested"}

    def review_adjustment(self, actor_id: str, adjustment_id: int, approve: bool) -> dict[str, Any]:
        """复核改账：复核人必须与申请人不同；批准后生成新版本批次。"""
        self._require(actor_id, "adjustment.review")
        row = self.connection.execute(
            "SELECT * FROM manual_adjustments WHERE adjustment_id=?", (adjustment_id,)
        ).fetchone()
        if row is None:
            raise NotFound("改账申请不存在")
        if row["state"] != "requested":
            raise InvalidState("改账申请已处理")
        if row["requested_by"] == actor_id:
            raise Forbidden("改账必须由申请人之外的人员复核")
        if not approve:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "UPDATE manual_adjustments SET state='rejected',reviewed_by=? WHERE adjustment_id=?",
                    (actor_id, adjustment_id),
                )
                self._audit("manual_adjustment", str(adjustment_id), "adjustment.rejected", actor_id, {})
            return {"adjustment_id": adjustment_id, "state": "rejected"}

        items = self.connection.execute(
            "SELECT * FROM adjustment_items WHERE adjustment_id=? ORDER BY item_id", (adjustment_id,)
        ).fetchall()
        origin_run = self._run(int(row["run_id"]))
        origin_reservations = self._reservations(int(row["run_id"]))
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "INSERT INTO allocation_runs(metering_version_id,period_id,input_sha256,result_json,"
                "state,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                (
                    origin_run["metering_version_id"], origin_run["period_id"],
                    digest({"adjustment": adjustment_id, "origin_run": row["run_id"]}),
                    canonical_json({"origin_run_id": int(row["run_id"]), "adjustment_id": adjustment_id}),
                    "adjusted", actor_id, self._now(),
                ),
            )
            new_run_id = int(cursor.lastrowid)
            for index, item in enumerate(items, start=1):
                origin = next(r for r in origin_reservations if r["line_id"] == item["line_id"])
                delta = Decimal(item["delta_mwh"])
                mwh = quantize_volume(abs(delta))
                price = Decimal(origin["unit_price_cny"])
                self.connection.execute(
                    "INSERT INTO reservations(reservation_id,run_id,metering_version_id,period_id,line_id,"
                    "source_id,unit_id,energy_kind,reserved_mwh,unit_price_cny,amount_cny,reason,state,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        f"rsv-{new_run_id}-adj-{index}", new_run_id,
                        origin_run["metering_version_id"], origin_run["period_id"],
                        item["line_id"], origin["source_id"], origin["unit_id"], origin["energy_kind"],
                        decimal_text(mwh), decimal_text(price),
                        decimal_text(quantize_money(mwh * price)),
                        f"人工改账新版本，复核人 {actor_id}，申请 {row['requested_by']}",
                        "adjusted", self._now(),
                    ),
                )
                balance = self._quota_balance(origin["source_id"])
                if delta > ZERO:
                    if balance is not None and mwh > balance:
                        raise Conflict(
                            f"来源 {origin['source_id']} 额度不足以补记改账 {decimal_text(mwh)} MWh"
                        )
                    # 补记电量已实际发生，按消耗占用额度（reserve 增加占用，后续不再返还）。
                    self._ledger(origin["source_id"], None, new_run_id, "reserve", mwh,
                                 actor_id, f"改账 {adjustment_id} 补记并核销")
                else:
                    self._ledger(origin["source_id"], None, new_run_id, "release", mwh,
                                 actor_id, f"改账 {adjustment_id} 冲回额度")
            self.connection.execute(
                "UPDATE allocation_runs SET state='adjusted' WHERE run_id=?", (row["run_id"],)
            )
            self.connection.execute(
                "UPDATE manual_adjustments SET state='reviewed',reviewed_by=?,new_run_id=? "
                "WHERE adjustment_id=?",
                (actor_id, new_run_id, adjustment_id),
            )
            self._audit("manual_adjustment", str(adjustment_id), "adjustment.reviewed", actor_id,
                        {"new_run_id": new_run_id, "reviewed_by": actor_id,
                         "requested_by": row["requested_by"]})
        return {"adjustment_id": adjustment_id, "state": "reviewed", "new_run_id": new_run_id}

    # ---- 幂等存储 ---------------------------------------------------------

    def _idempotent(self, scope: str, key: str, request: Mapping[str, Any]) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT request_sha256,response_json FROM revenue_idempotency "
            "WHERE scope=? AND idempotency_key=?",
            (scope, key),
        ).fetchone()
        if row is None:
            return None
        if row["request_sha256"] != digest(request):
            raise Conflict("复用编号提交了不同内容，请求已拒绝")
        return {**json.loads(row["response_json"]), "replayed": True}

    def _store_idempotent(self, scope: str, key: str, request: Mapping[str, Any],
                          response: Mapping[str, Any]) -> None:
        self.connection.execute(
            "INSERT INTO revenue_idempotency(scope,idempotency_key,request_sha256,response_json,created_at) "
            "VALUES(?,?,?,?,?)",
            (scope, key, digest(request), canonical_json(response), self._now()),
        )

    # ---- 视图：场站、经营、财务、审计各自隐藏无权信息 ---------------------

    def _station_of(self, unit_id: str) -> str:
        row = self.connection.execute(
            "SELECT station_id FROM units WHERE unit_id=?", (unit_id,)
        ).fetchone()
        if row is None:
            raise NotFound("机组不存在")
        return row["station_id"]

    def _restrict(self, user: sqlite3.Row, record: dict[str, Any]) -> dict[str, Any]:
        allowed = VIEW_FIELDS[user["role"]]
        if user["role"] == "station" and user["station_id"] is not None:
            if record.get("station_id") not in (None, user["station_id"]):
                raise Forbidden("场站账号不能查看其他场站收益")
        return {key: value for key, value in record.items() if "*" in allowed or key in allowed}

    def explain_revenue(self, actor_id: str, line_id: str, run_id: int | None = None) -> dict[str, Any]:
        """解释一笔电量为何落到某个来源：命中原因与每个被跳过来源的具体理由。"""
        user = self._require(actor_id, "report.read")
        if run_id is None:
            row = self.connection.execute(
                "SELECT run_id FROM reservations WHERE line_id=? ORDER BY created_at DESC LIMIT 1",
                (line_id,),
            ).fetchone()
            if row is None:
                raise NotFound("找不到该计量行的分摊记录")
            run_id = int(row["run_id"])
        run = self._run(run_id)
        reservation = self.connection.execute(
            "SELECT * FROM reservations WHERE run_id=? AND line_id=? ORDER BY reservation_id LIMIT 1",
            (run_id, line_id),
        ).fetchone()
        if reservation is None:
            raise NotFound("该计量行在此批次中没有分摊记录")
        explanation = json.loads(run["result_json"]).get("explanations", {}).get(line_id)
        source = self.connection.execute(
            "SELECT source_type FROM revenue_sources WHERE source_id=?",
            (reservation["source_id"],),
        ).fetchone()
        record = {
            "run_id": run_id,
            "period_id": run["period_id"],
            "line_id": line_id,
            "unit_id": reservation["unit_id"],
            "station_id": self._station_of(reservation["unit_id"]),
            "energy_kind": reservation["energy_kind"],
            "source_id": reservation["source_id"],
            "source_type": source["source_type"] if source else None,
            "reserved_mwh": reservation["reserved_mwh"],
            "unit_price_cny": reservation["unit_price_cny"],
            "amount_cny": reservation["amount_cny"],
            "state": reservation["state"],
            "reason": reservation["reason"],
            "skipped": [] if explanation is None else explanation["skipped"],
        }
        return self._restrict(user, record)

    def run_detail(self, actor_id: str, run_id: int) -> dict[str, Any]:
        user = self._require(actor_id, "report.read")
        run = self._run(run_id)
        rows: list[dict[str, Any]] = []
        for reservation in self._reservations(run_id):
            record = {
                "reservation_id": reservation["reservation_id"],
                "run_id": run_id,
                "period_id": run["period_id"],
                "line_id": reservation["line_id"],
                "unit_id": reservation["unit_id"],
                "station_id": self._station_of(reservation["unit_id"]),
                "energy_kind": reservation["energy_kind"],
                "time_bucket": None,
                "source_id": reservation["source_id"],
                "source_type": self.connection.execute(
                    "SELECT source_type FROM revenue_sources WHERE source_id=?",
                    (reservation["source_id"],),
                ).fetchone()["source_type"],
                "reserved_mwh": reservation["reserved_mwh"],
                "mwh": reservation["reserved_mwh"],
                "unit_price_cny": reservation["unit_price_cny"],
                "amount_cny": reservation["amount_cny"],
                "state": reservation["state"],
                "reason": reservation["reason"],
            }
            try:
                rows.append(self._restrict(user, record))
            except Forbidden:
                continue
        result = json.loads(run["result_json"])
        return {
            "run_id": run_id,
            "period_id": run["period_id"],
            "metering_version_id": run["metering_version_id"],
            "state": run["state"],
            "total_amount_cny": decimal_text(
                quantize_money(sum((Decimal(r["amount_cny"]) for r in self._reservations(run_id)), ZERO))
            ),
            "pending_loss": result.get("pending_loss", []),
            "reservations": rows,
        }

    def source_quota(self, actor_id: str, source_id: str) -> dict[str, Any]:
        """经营/审计视图：来源额度台账与余额。"""
        self._require(actor_id, "report.read")
        source = self.source(source_id)
        balance = self._quota_balance(source_id)
        ledger = self.connection.execute(
            "SELECT movement,delta_mwh,balance_mwh,note,created_at,reservation_id,run_id "
            "FROM source_quota_ledger WHERE source_id=? ORDER BY ledger_id",
            (source_id,),
        ).fetchall()
        return {
            "source_id": source_id,
            "source_type": source["source_type"],
            "cap_mwh": source["cap_mwh"],
            "balance_mwh": None if balance is None else decimal_text(balance),
            "ledger": [dict(row) for row in ledger],
        }

    def audit_chain(self, actor_id: str) -> dict[str, Any]:
        self._require(actor_id, "audit.read")
        rows = self.connection.execute(
            "SELECT * FROM revenue_audit_events ORDER BY event_id"
        ).fetchall()
        previous_hash = "0" * 64
        valid = True
        for row in rows:
            body = {
                "entity_type": row["entity_type"],
                "entity_id": row["entity_id"],
                "event_type": row["event_type"],
                "actor_id": row["actor_id"],
                "payload": json.loads(row["payload_json"]),
                "created_at": row["created_at"],
                "previous_hash": row["previous_hash"],
            }
            calculated = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
            if row["previous_hash"] != previous_hash or row["event_hash"] != calculated:
                valid = False
                break
            previous_hash = row["event_hash"]
        return {"valid": valid, "events": len(rows), "head_hash": previous_hash}
