"""收益归集事务用例：来源登记、冻结分摊、额度预占、确认核销、撤销/结转、人工改账与三视图。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from decimal import Decimal
from typing import Any, Mapping, Sequence

from .allocation import (
    EnergyEntry,
    LossEntry,
    allocate,
    canonical_json,
    decimal_text,
    digest,
    distribute_confirmation,
    quantize_money,
    quantize_volume,
)
from .clock import SystemClock, utc_text
from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .models import MeasurementVersion, RevenueSource
from .storage import initialize, transaction


# 角色 -> 权限。场站只能登记/冻结本站数据并提出改账；经营管理来源、分摊、核销与改账复核；审计只读并验链。
ROLE_PERMISSIONS = {
    "station": {
        "measurement.freeze",
        "adjustment.request",
        "view.station",
    },
    "operations": {
        "source.write",
        "settlement.run",
        "measurement.confirm",
        "application.revoke",
        "application.fail",
        "adjustment.review",
        "view.operations",
    },
    "auditor": {"view.audit", "audit.read"},
}


class RevenueService:
    def __init__(self, connection: sqlite3.Connection, clock=None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        initialize(connection)

    # ── 基础设施工具 ──────────────────────────────────────────────

    def _now(self) -> str:
        return utc_text(self.clock.now())

    def _user(self, user_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM rev_users WHERE user_id=?", (user_id,)
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
            "SELECT event_hash FROM rev_audit_events ORDER BY event_id DESC LIMIT 1"
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
            "INSERT INTO rev_audit_events(entity_type,entity_id,event_type,actor_id,payload_json,"
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

    def _app_event(self, application_id: int, event_type: str, actor_id: str, payload: Mapping[str, Any]) -> None:
        self.connection.execute(
            "INSERT INTO application_events(application_id,event_type,actor_id,payload_json,created_at) "
            "VALUES(?,?,?,?,?)",
            (application_id, event_type, actor_id, canonical_json(payload), self._now()),
        )

    def _idempotency(self, scope: str, key: str, raw: Mapping[str, Any]):
        request_digest = digest(raw)
        stored = self.connection.execute(
            "SELECT request_sha256,response_json FROM rev_idempotency WHERE scope=? AND idempotency_key=?",
            (scope, key),
        ).fetchone()
        return request_digest, stored

    def create_user(
        self,
        user_id: str,
        display_name: str,
        role: str,
        station_id: str | None = None,
    ) -> dict[str, Any]:
        if role not in ROLE_PERMISSIONS:
            raise ValidationFailed("未知角色")
        if not user_id.strip() or not display_name.strip():
            raise ValidationFailed("用户编号和名称不能为空")
        if role == "station" and not station_id:
            raise ValidationFailed("场站账号必须绑定 station_id")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO rev_users(user_id,display_name,role,station_id,created_at) VALUES(?,?,?,?,?)",
                    (user_id.strip(), display_name.strip(), role, station_id, self._now()),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("用户已经存在") from exc
        return {"user_id": user_id.strip(), "role": role, "station_id": station_id}

    # ── 1. 结算依据（来源批次）登记 ───────────────────────────────

    def register_source(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "source.write")
        source = RevenueSource.from_dict(raw)
        definition = canonical_json(raw)
        content_sha256 = hashlib.sha256(definition.encode("utf-8")).hexdigest()
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO revenue_sources(source_id,name,kind,cycle_id,station_id,eligible_units_json,"
                    "valid_from,valid_to,use_order,limit_type,cap_amount,unit_price_cny_per_mwh,"
                    "curtailment_rate_cny_per_mwh,definition_json,content_sha256,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        source.source_id,
                        source.name,
                        source.kind,
                        source.cycle_id,
                        source.station_id,
                        canonical_json(list(source.eligible_unit_ids)),
                        source.valid_from,
                        source.valid_to,
                        source.use_order,
                        source.limit_type,
                        decimal_text(source.cap_amount) if source.cap_amount is not None else None,
                        decimal_text(source.unit_price_cny_per_mwh)
                        if source.unit_price_cny_per_mwh is not None
                        else None,
                        decimal_text(source.curtailment_rate_cny_per_mwh)
                        if source.curtailment_rate_cny_per_mwh is not None
                        else None,
                        definition,
                        content_sha256,
                        actor_id,
                        self._now(),
                    ),
                )
                for slot in source.price_slots:
                    self.connection.execute(
                        "INSERT INTO revenue_source_slots(source_id,slot_id,label,starts_at,ends_at,"
                        "price_cny_per_mwh,eligible_units_json) VALUES(?,?,?,?,?,?,?)",
                        (
                            source.source_id,
                            slot.slot_id,
                            slot.label,
                            slot.starts_at,
                            slot.ends_at,
                            decimal_text(slot.price_cny_per_mwh),
                            canonical_json(list(slot.eligible_unit_ids)),
                        ),
                    )
                self._audit("revenue_source", source.source_id, "source.registered", actor_id, {
                    "kind": source.kind,
                    "cycle_id": source.cycle_id,
                    "use_order": source.use_order,
                    "limit_type": source.limit_type,
                    "cap_amount": decimal_text(source.cap_amount) if source.cap_amount is not None else None,
                    "sha256": content_sha256,
                })
        except sqlite3.IntegrityError as exc:
            raise Conflict("同一发电周期内来源编号冲突") from exc
        return self.source_detail(source.source_id)

    def source_detail(self, source_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM revenue_sources WHERE source_id=?", (source_id,)
        ).fetchone()
        if row is None:
            raise NotFound("结算依据不存在")
        slots = self.connection.execute(
            "SELECT * FROM revenue_source_slots WHERE source_id=? ORDER BY starts_at,slot_id",
            (source_id,),
        ).fetchall()
        return self._source_dict(row, slots)

    def source_view(self, actor_id: str, source_id: str) -> dict[str, Any]:
        """来源详情按角色裁剪：场站看不到价格、上限与时段价。"""

        user = self._user(actor_id)
        detail = self.source_detail(source_id)
        if user["role"] != "station":
            return detail
        if detail["station_id"] is not None and detail["station_id"] != user["station_id"]:
            raise NotFound("结算依据不存在")
        for key in ("limit_type", "cap_amount", "unit_price_cny_per_mwh",
                    "curtailment_rate_cny_per_mwh", "price_slots", "revision"):
            detail.pop(key, None)
        return detail

    @staticmethod
    def _source_dict(row: sqlite3.Row, slots: Sequence[sqlite3.Row]) -> dict[str, Any]:
        return {
            "source_id": row["source_id"],
            "name": row["name"],
            "kind": row["kind"],
            "cycle_id": row["cycle_id"],
            "station_id": row["station_id"],
            "eligible_unit_ids": json.loads(row["eligible_units_json"]),
            "valid_from": row["valid_from"],
            "valid_to": row["valid_to"],
            "use_order": row["use_order"],
            "limit_type": row["limit_type"],
            "cap_amount": row["cap_amount"],
            "unit_price_cny_per_mwh": row["unit_price_cny_per_mwh"],
            "curtailment_rate_cny_per_mwh": row["curtailment_rate_cny_per_mwh"],
            "state": row["state"],
            "revision": row["revision"],
            "price_slots": [
                {
                    "slot_id": slot["slot_id"],
                    "label": slot["label"],
                    "starts_at": slot["starts_at"],
                    "ends_at": slot["ends_at"],
                    "price_cny_per_mwh": slot["price_cny_per_mwh"],
                    "eligible_unit_ids": json.loads(slot["eligible_units_json"]),
                }
                for slot in slots
            ],
        }

    def _active_sources(self, cycle_id: str, station_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM revenue_sources WHERE cycle_id=? AND state='active' "
            "AND (station_id IS NULL OR station_id=?) ORDER BY use_order,source_id",
            (cycle_id, station_id),
        ).fetchall()
        snapshot: list[dict[str, Any]] = []
        for row in rows:
            slots = self.connection.execute(
                "SELECT * FROM revenue_source_slots WHERE source_id=? ORDER BY starts_at,slot_id",
                (row["source_id"],),
            ).fetchall()
            snapshot.append(self._source_dict(row, slots))
        return snapshot

    def revise_source(self, actor_id: str, expected_revision: int, raw: Mapping[str, Any]) -> dict[str, Any]:
        """登记结算依据的新版本：编号与周期不变，乐观锁校验版本，旧运行继续绑定旧快照。"""

        self._require(actor_id, "source.write")
        source = RevenueSource.from_dict(raw)
        existing = self.connection.execute(
            "SELECT revision,state,cycle_id FROM revenue_sources WHERE source_id=?", (source.source_id,)
        ).fetchone()
        if existing is None:
            raise NotFound("结算依据不存在，不能修订")
        if existing["cycle_id"] != source.cycle_id:
            raise ValidationFailed("修订不能改变来源所属发电周期")
        if existing["state"] != "active":
            raise InvalidState("已停用的依据不能修订，请重新登记")
        if existing["revision"] != expected_revision:
            raise InvalidState("依据已被他人修改，请基于最新版本提交")
        definition = canonical_json(raw)
        content_sha256 = hashlib.sha256(definition.encode("utf-8")).hexdigest()
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE revenue_sources SET name=?,kind=?,station_id=?,eligible_units_json=?,"
                "valid_from=?,valid_to=?,use_order=?,limit_type=?,cap_amount=?,"
                "unit_price_cny_per_mwh=?,curtailment_rate_cny_per_mwh=?,definition_json=?,"
                "content_sha256=?,revision=revision+1 WHERE source_id=? AND revision=?",
                (
                    source.name,
                    source.kind,
                    source.station_id,
                    canonical_json(list(source.eligible_unit_ids)),
                    source.valid_from,
                    source.valid_to,
                    source.use_order,
                    source.limit_type,
                    decimal_text(source.cap_amount) if source.cap_amount is not None else None,
                    decimal_text(source.unit_price_cny_per_mwh)
                    if source.unit_price_cny_per_mwh is not None
                    else None,
                    decimal_text(source.curtailment_rate_cny_per_mwh)
                    if source.curtailment_rate_cny_per_mwh is not None
                    else None,
                    definition,
                    content_sha256,
                    source.source_id,
                    expected_revision,
                ),
            )
            self.connection.execute("DELETE FROM revenue_source_slots WHERE source_id=?", (source.source_id,))
            for slot in source.price_slots:
                self.connection.execute(
                    "INSERT INTO revenue_source_slots(source_id,slot_id,label,starts_at,ends_at,"
                    "price_cny_per_mwh,eligible_units_json) VALUES(?,?,?,?,?,?,?)",
                    (
                        source.source_id,
                        slot.slot_id,
                        slot.label,
                        slot.starts_at,
                        slot.ends_at,
                        decimal_text(slot.price_cny_per_mwh),
                        canonical_json(list(slot.eligible_unit_ids)),
                    ),
                )
            self._audit("revenue_source", source.source_id, "source.revised", actor_id, {
                "old_revision": expected_revision,
                "new_revision": expected_revision + 1,
                "sha256": content_sha256,
            })
        return self.source_detail(source.source_id)

    def retire_source(self, actor_id: str, source_id: str, expected_revision: int) -> dict[str, Any]:
        self._require(actor_id, "source.write")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE revenue_sources SET state='retired',revision=revision+1 "
                "WHERE source_id=? AND state='active' AND revision=?",
                (source_id, expected_revision),
            )
            if cursor.rowcount != 1:
                raise InvalidState("来源不是当前生效版本，无法停用")
            self._audit("revenue_source", source_id, "source.retired", actor_id, {})
        return {"source_id": source_id, "state": "retired", "revision": expected_revision + 1}

    # ── 2. 冻结计量版本 ──────────────────────────────────────────

    def freeze_measurement(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        user = self._require(actor_id, "measurement.freeze")
        version = MeasurementVersion.from_dict(raw)
        if user["station_id"] != version.station_id:
            raise Forbidden("场站只能冻结本站的计量数据")
        entries_json = canonical_json([
            {
                "unit_id": entry.unit_id,
                "slot_id": entry.slot_id,
                "energy_mwh": decimal_text(entry.energy_mwh),
                "curtailment_mwh": decimal_text(entry.curtailment_mwh),
            }
            for entry in version.entries
        ])
        content_sha256 = hashlib.sha256(entries_json.encode("utf-8")).hexdigest()
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO measurement_versions(version_id,cycle_id,station_id,period_start,period_end,"
                    "entries_json,content_sha256,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        version.version_id,
                        version.cycle_id,
                        version.station_id,
                        version.period_start,
                        version.period_end,
                        entries_json,
                        content_sha256,
                        actor_id,
                        self._now(),
                    ),
                )
                self._audit("measurement", version.version_id, "measurement.frozen", actor_id, {
                    "cycle_id": version.cycle_id,
                    "station_id": version.station_id,
                    "units": len(version.entries),
                    "sha256": content_sha256,
                })
        except sqlite3.IntegrityError as exc:
            raise Conflict("计量版本编号或内容已经存在") from exc
        return {"version_id": version.version_id, "state": "frozen", "sha256": content_sha256}

    def _measurement(self, version_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM measurement_versions WHERE version_id=?", (version_id,)
        ).fetchone()
        if row is None:
            raise NotFound("计量版本不存在")
        return row

    # ── 3. 针对冻结版本计算分摊并预占额度 ─────────────────────────

    @staticmethod
    def _period_days(version: sqlite3.Row) -> tuple[str, str]:
        return version["period_start"][:10], version["period_end"][:10]

    def _applicable_snapshot(self, version: sqlite3.Row) -> list[dict[str, Any]]:
        day_from, day_to = self._period_days(version)
        snapshot = []
        for source in self._active_sources(version["cycle_id"], version["station_id"]):
            if source["valid_from"] <= day_from and source["valid_to"] >= day_to:
                snapshot.append(source)
        return snapshot

    def _live_usage(self, cycle_id: str, exclude_run_id: int | None = None) -> dict[str, dict[str, Decimal]]:
        """汇总周期内仍占用额度的分摊行（撤销行不计）。"""

        clauses = ["cycle_id=?", "state IN ('reserved','settled','carried_forward')"]
        params: list[Any] = [cycle_id]
        if exclude_run_id is not None:
            clauses.append("run_id<>?")
            params.append(exclude_run_id)
        rows = self.connection.execute(
            "SELECT source_id,state,reserved_mwh,settled_mwh,carried_mwh,reserved_cny,settled_cny,carried_cny "
            f"FROM revenue_applications WHERE {' AND '.join(clauses)}",
            params,
        ).fetchall()
        usage: dict[str, dict[str, Decimal]] = {}
        for row in rows:
            entry = usage.setdefault(row["source_id"], {"mwh": Decimal(0), "cny": Decimal(0)})
            if row["state"] == "reserved":
                entry["mwh"] += Decimal(row["reserved_mwh"])
                entry["cny"] += Decimal(row["reserved_cny"])
            elif row["state"] == "settled":
                entry["mwh"] += Decimal(row["settled_mwh"])
                entry["cny"] += Decimal(row["settled_cny"])
            else:
                entry["mwh"] += Decimal(row["carried_mwh"])
                entry["cny"] += Decimal(row["carried_cny"])
        return usage

    def calculate_settlement(self, actor_id: str, version_id: str) -> dict[str, Any]:
        self._require(actor_id, "settlement.run")
        version = self._measurement(version_id)
        if version["state"] != "frozen":
            raise InvalidState("计量版本已确认，规则变更只影响尚未确认的周期")
        snapshot = self._applicable_snapshot(version)
        if not snapshot:
            raise InvalidState("周期内没有覆盖计量期间的生效结算依据")
        entries = [
            {
                "unit_id": entry["unit_id"],
                "slot_id": entry["slot_id"],
                "energy_mwh": Decimal(entry["energy_mwh"]),
            }
            for entry in json.loads(version["entries_json"])
        ]
        energy = [EnergyEntry(item["unit_id"], item["slot_id"], item["energy_mwh"]) for item in entries]
        input_value = {"version_sha256": version["content_sha256"], "sources": snapshot}
        input_sha256 = digest(input_value)
        existing = self.connection.execute(
            "SELECT run_id,result_json FROM settlement_runs WHERE version_id=? AND input_sha256=?",
            (version_id, input_sha256),
        ).fetchone()
        if existing is not None:
            result = json.loads(existing["result_json"])
            ids = self.connection.execute(
                "SELECT sequence_no,application_id FROM revenue_applications WHERE run_id=? ORDER BY sequence_no",
                (existing["run_id"],),
            ).fetchall()
            id_by_sequence = {row["sequence_no"]: int(row["application_id"]) for row in ids}
            for index, line in enumerate(result["applications"], start=1):
                line["application_id"] = id_by_sequence.get(index)
            return {"run_id": existing["run_id"], **result, "replayed": True}

        with transaction(self.connection, immediate=True):
            # 同版本旧运行（来源规则已变化）：连同其预占一并作废，额度释放给新运行。
            old_runs = self.connection.execute(
                "SELECT run_id FROM settlement_runs WHERE version_id=? AND state='current'",
                (version_id,),
            ).fetchall()
            for old in old_runs:
                self.connection.execute(
                    "UPDATE settlement_runs SET state='superseded' WHERE run_id=?", (old["run_id"],)
                )
                old_apps = self.connection.execute(
                    "SELECT application_id FROM revenue_applications WHERE run_id=? AND state='reserved'",
                    (old["run_id"],),
                ).fetchall()
                for app in old_apps:
                    self.connection.execute(
                        "UPDATE revenue_applications SET state='released',released_mwh=reserved_mwh,"
                        "updated_at=? WHERE application_id=?",
                        (self._now(), app["application_id"]),
                    )
                    self._app_event(app["application_id"], "reservation.replaced", actor_id,
                                    {"reason": "来源规则变更后重新分摊"})

            usage = self._live_usage(version["cycle_id"])
            result = allocate(snapshot, energy, (), usage)
            cursor = self.connection.execute(
                "INSERT INTO settlement_runs(version_id,cycle_id,sources_snapshot_json,input_sha256,"
                "result_json,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                (
                    version_id,
                    version["cycle_id"],
                    canonical_json(snapshot),
                    input_sha256,
                    canonical_json(result),
                    actor_id,
                    self._now(),
                ),
            )
            run_id = int(cursor.lastrowid)
            now = self._now()
            for sequence, line in enumerate(result["applications"], start=1):
                qty = Decimal(str(line["quantity_mwh"]))
                amount = Decimal(str(line["amount_cny"]))
                app_cursor = self.connection.execute(
                    "INSERT INTO revenue_applications(run_id,version_id,cycle_id,source_id,unit_id,slot_id,"
                    "basis,quantity_mwh,unit_price_cny,amount_cny,reserved_mwh,reserved_cny,"
                    "sequence_no,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        run_id,
                        version_id,
                        version["cycle_id"],
                        line["source_id"],
                        line["unit_id"],
                        line["slot_id"],
                        line["basis"],
                        decimal_text(qty),
                        line["unit_price_cny"],
                        decimal_text(amount),
                        decimal_text(qty),
                        decimal_text(amount),
                        sequence,
                        now,
                        now,
                    ),
                )
                line["application_id"] = int(app_cursor.lastrowid)
            self._audit("settlement_run", str(run_id), "settlement.calculated", actor_id, {
                "version_id": version_id,
                "applications": len(result["applications"]),
                "unallocated_energy": result["unallocated_energy"],
                "input_sha256": input_sha256,
            })
        return {"run_id": run_id, **result, "replayed": False}

    # ── 4. 计量确认后按实际电量核销 ──────────────────────────────

    @staticmethod
    def _parse_actual_entries(raw: Mapping[str, Any]) -> list[dict[str, Any]]:
        entries = raw.get("actual_entries")
        if not isinstance(entries, list) or not entries:
            raise ValidationFailed("actual_entries 必须是非空实际电量数组")
        parsed: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for item in entries:
            if not isinstance(item, Mapping):
                raise ValidationFailed("实际电量明细必须是对象")
            unit_id = str(item.get("unit_id", "")).strip()
            if not unit_id:
                raise ValidationFailed("actual_entries.unit_id 不能为空")
            slot = item.get("slot_id")
            slot_id = None if slot is None or slot == "" else str(slot)
            key = (unit_id, slot_id or "")
            if key in seen:
                raise ValidationFailed("同一机组同一时段实际电量不能重复")
            seen.add(key)
            energy = Decimal(str(item.get("energy_mwh", 0)))
            loss = Decimal(str(item.get("curtailment_mwh", 0)))
            if not energy.is_finite() or energy < 0 or not loss.is_finite() or loss < 0:
                raise ValidationFailed("电量必须是非负有限数")
            parsed.append({"unit_id": unit_id, "slot_id": slot_id, "energy_mwh": energy, "curtailment_mwh": loss})
        return parsed

    def confirm_measurement(
        self,
        actor_id: str,
        version_id: str,
        raw: Mapping[str, Any],
    ) -> dict[str, Any]:
        self._require(actor_id, "measurement.confirm")
        key = str(raw.get("idempotency_key", "")).strip()
        if not key:
            raise ValidationFailed("idempotency_key 不能为空")
        request_digest, stored = self._idempotency("confirm", key, {"version_id": version_id, **dict(raw)})
        # 重复核销请求无论版本当前处于什么状态，都返回首次处理的原结果。
        if stored is not None:
            if stored["request_sha256"] != request_digest:
                raise Conflict("幂等键对应不同确认内容")
            return json.loads(stored["response_json"])
        version = self._measurement(version_id)
        if version["state"] != "frozen":
            raise InvalidState("计量版本不是冻结状态")
        run = self.connection.execute(
            "SELECT * FROM settlement_runs WHERE version_id=? AND state='current' ORDER BY run_id DESC LIMIT 1",
            (version_id,),
        ).fetchone()
        if run is None:
            raise InvalidState("尚未生成分摊结果，不能确认")
        actual = self._parse_actual_entries(raw)
        actual_payload = [
            {
                "unit_id": item["unit_id"],
                "slot_id": item["slot_id"],
                "energy_mwh": decimal_text(item["energy_mwh"]),
                "curtailment_mwh": decimal_text(item["curtailment_mwh"]),
            }
            for item in actual
        ]

        reserved_rows = self.connection.execute(
            "SELECT * FROM revenue_applications WHERE run_id=? ORDER BY sequence_no",
            (run["run_id"],),
        ).fetchall()
        energy_rows = [row for row in reserved_rows if row["basis"] == "energy" and row["state"] == "reserved"]
        # 已撤销/已结转的行不参与核销；电量池行与属性收益行分别按完整实际电量核销
        # （绿证/调峰不因未被市场覆盖而少计）。
        snapshot = json.loads(run["sources_snapshot_json"])
        source_kinds = {item["source_id"]: item["kind"] for item in snapshot}
        pool_lines = [row for row in energy_rows if source_kinds.get(row["source_id"]) in ("MARKET_ENERGY", "GUARANTEED_PURCHASE")]
        addon_lines = [row for row in energy_rows if source_kinds.get(row["source_id"]) in ("GREEN_CERTIFICATE", "PEAK_SHAVING")]

        def confirmed_map(lines):
            confirmed: dict[tuple[str, str, str | None], Decimal] = {}
            wanted = {(row["unit_id"], row["slot_id"]) for row in lines}
            for item in actual:
                if (item["unit_id"], item["slot_id"]) in wanted or not wanted:
                    confirmed[("energy", item["unit_id"], item["slot_id"])] = item["energy_mwh"]
            return confirmed

        pool_distribution = distribute_confirmation(
            [dict(row) for row in pool_lines], confirmed_map(pool_lines)
        )
        addon_distribution = distribute_confirmation(
            [dict(row) for row in addon_lines], confirmed_map(addon_lines)
        )
        distribution = {item["application_id"]: item for item in pool_distribution + addon_distribution}

        result: dict[str, Any] = {"version_id": version_id, "applications": [], "loss_applications": []}
        now = self._now()
        with transaction(self.connection, immediate=True):
            total_energy = Decimal(0)
            total_loss = Decimal(0)
            for row in reserved_rows:
                decision = distribution.get(row["application_id"])
                if decision is None:
                    continue
                settled_mwh = Decimal(decision["settled_mwh"])
                released_mwh = Decimal(decision["released_mwh"])
                settled_cny = Decimal(decision["settled_cny"])
                state = "settled" if settled_mwh > 0 else "released"
                self.connection.execute(
                    "UPDATE revenue_applications SET state=?,settled_mwh=?,released_mwh=?,settled_cny=?,"
                    "revision=revision+1,updated_at=? WHERE application_id=? AND state='reserved'",
                    (
                        state,
                        decimal_text(settled_mwh),
                        decimal_text(released_mwh),
                        decimal_text(settled_cny),
                        now,
                        row["application_id"],
                    ),
                )
                self._app_event(row["application_id"], "application.settled", actor_id, {
                    "settled_mwh": decimal_text(settled_mwh),
                    "released_mwh": decimal_text(released_mwh),
                    "settled_cny": decimal_text(settled_cny),
                })
                result["applications"].append({
                    "application_id": row["application_id"],
                    "source_id": row["source_id"],
                    "unit_id": row["unit_id"],
                    "slot_id": row["slot_id"],
                    "state": state,
                    "settled_mwh": decimal_text(settled_mwh),
                    "released_mwh": decimal_text(released_mwh),
                    "settled_cny": decimal_text(settled_cny),
                })
                total_energy += settled_mwh

            # 限电补偿：确认损失电量后才计列，直接核销。
            loss_entries = [
                LossEntry(item["unit_id"], item["curtailment_mwh"])
                for item in actual
                if item["curtailment_mwh"] > 0
            ]
            if loss_entries:
                self.connection.executemany(
                    "INSERT INTO confirmed_loss(version_id,unit_id,curtailment_mwh) VALUES(?,?,?)",
                    [
                        (version_id, item["unit_id"], decimal_text(item["curtailment_mwh"]))
                        for item in actual
                        if item["curtailment_mwh"] > 0
                    ],
                )
                usage = self._live_usage(version["cycle_id"], exclude_run_id=run["run_id"])
                loss_result = allocate(snapshot, (), loss_entries, usage)
                sequence_start = len(reserved_rows)
                for offset, line in enumerate(loss_result["applications"], start=1):
                    qty = Decimal(str(line["quantity_mwh"]))
                    amount = Decimal(str(line["amount_cny"]))
                    cursor = self.connection.execute(
                        "INSERT INTO revenue_applications(run_id,version_id,cycle_id,source_id,unit_id,slot_id,"
                        "basis,quantity_mwh,unit_price_cny,amount_cny,state,reserved_mwh,settled_mwh,"
                        "reserved_cny,settled_cny,sequence_no,created_at,updated_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            run["run_id"],
                            version_id,
                            version["cycle_id"],
                            line["source_id"],
                            line["unit_id"],
                            None,
                            "loss",
                            decimal_text(qty),
                            line["unit_price_cny"],
                            decimal_text(amount),
                            "settled",
                            decimal_text(qty),
                            decimal_text(qty),
                            decimal_text(amount),
                            decimal_text(amount),
                            sequence_start + offset,
                            now,
                            now,
                        ),
                    )
                    new_id = int(cursor.lastrowid)
                    self._app_event(new_id, "application.settled", actor_id, {
                        "basis": "loss",
                        "settled_mwh": decimal_text(qty),
                        "settled_cny": decimal_text(amount),
                    })
                    result["loss_applications"].append({
                        "application_id": new_id,
                        "source_id": line["source_id"],
                        "unit_id": line["unit_id"],
                        "settled_mwh": decimal_text(qty),
                        "settled_cny": decimal_text(amount),
                    })
                    total_loss += qty

            self.connection.execute(
                "UPDATE measurement_versions SET state='confirmed',confirmed_by=?,confirmed_at=? WHERE version_id=?",
                (actor_id, now, version_id),
            )
            self.connection.execute(
                "INSERT INTO settlement_confirmations(version_id,actual_entries_json,result_json,"
                "idempotency_key,confirmed_by,created_at) VALUES(?,?,?,?,?,?)",
                (version_id, canonical_json(actual_payload), canonical_json(result), key, actor_id, now),
            )
            self.connection.execute(
                "INSERT INTO rev_idempotency(scope,idempotency_key,request_sha256,response_json,created_at) "
                "VALUES('confirm',?,?,?,?)",
                (key, request_digest, canonical_json(result), now),
            )
            self._audit("measurement", version_id, "measurement.confirmed", actor_id, {
                "settled_energy_mwh": decimal_text(quantize_volume(total_energy)),
                "confirmed_loss_mwh": decimal_text(quantize_volume(total_loss)),
                "idempotency_key": key,
            })
        return result

    # ── 5. 撤销、执行失败（结转） ────────────────────────────────

    def _application(self, application_id: int) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM revenue_applications WHERE application_id=?", (application_id,)
        ).fetchone()
        if row is None:
            raise NotFound("分摊行不存在")
        return row

    def revoke_application(self, actor_id: str, application_id: int, reason: str) -> dict[str, Any]:
        self._require(actor_id, "application.revoke")
        if not reason or not reason.strip():
            raise ValidationFailed("撤销原因不能为空")
        with transaction(self.connection, immediate=True):
            row = self._application(application_id)
            if row["state"] != "reserved":
                raise InvalidState("只有预占中的分摊行可以撤销")
            self.connection.execute(
                "UPDATE revenue_applications SET state='released',released_mwh=reserved_mwh,"
                "revision=revision+1,updated_at=? WHERE application_id=?",
                (self._now(), application_id),
            )
            self._app_event(application_id, "application.revoked", actor_id, {"reason": reason.strip()})
            self._audit("application", str(application_id), "application.revoked", actor_id, {"reason": reason.strip()})
        return {"application_id": application_id, "state": "released"}

    def mark_execution_failed(self, actor_id: str, application_id: int, reason: str) -> dict[str, Any]:
        """执行失败不释放额度，而把未结部分结转到后续周期。"""

        self._require(actor_id, "application.fail")
        if not reason or not reason.strip():
            raise ValidationFailed("失败原因不能为空")
        with transaction(self.connection, immediate=True):
            row = self._application(application_id)
            if row["state"] not in ("reserved", "settled"):
                raise InvalidState("只有预占中或已核销的分摊行可以登记执行失败")
            if row["state"] == "reserved":
                carry_mwh = Decimal(row["reserved_mwh"])
                carry_cny = Decimal(row["reserved_cny"])
            else:
                carry_mwh = Decimal(row["settled_mwh"])
                carry_cny = Decimal(row["settled_cny"])
            self.connection.execute(
                "UPDATE revenue_applications SET state='carried_forward',carried_mwh=?,carried_cny=?,"
                "revision=revision+1,updated_at=? WHERE application_id=?",
                (decimal_text(carry_mwh), decimal_text(carry_cny), self._now(), application_id),
            )
            self._app_event(application_id, "execution.failed", actor_id, {
                "reason": reason.strip(),
                "carried_mwh": decimal_text(carry_mwh),
                "carried_cny": decimal_text(carry_cny),
            })
            self._audit("application", str(application_id), "application.carried_forward", actor_id, {
                "reason": reason.strip(),
                "carried_mwh": decimal_text(carry_mwh),
            })
        return {
            "application_id": application_id,
            "state": "carried_forward",
            "carried_mwh": decimal_text(carry_mwh),
            "carried_cny": decimal_text(carry_cny),
        }

    # ── 6. 人工改账：不同人员复核后生成新版本 ────────────────────

    def request_adjustment(
        self,
        actor_id: str,
        application_id: int,
        raw: Mapping[str, Any],
    ) -> dict[str, Any]:
        self._require(actor_id, "adjustment.request")
        key = str(raw.get("idempotency_key", "")).strip()
        if not key:
            raise ValidationFailed("idempotency_key 不能为空")
        reason = str(raw.get("reason", "")).strip()
        if not reason:
            raise ValidationFailed("改账原因不能为空")
        delta_mwh = Decimal(str(raw.get("delta_mwh", 0)))
        delta_cny = Decimal(str(raw.get("delta_cny", 0)))
        if not delta_mwh.is_finite() or not delta_cny.is_finite():
            raise ValidationFailed("改账差额必须是有限数")
        if delta_mwh == 0 and delta_cny == 0:
            raise ValidationFailed("改账差额不能全部为零")
        request_digest, stored = self._idempotency(
            "adjust", key,
            {"application_id": application_id, "delta_mwh": str(delta_mwh), "delta_cny": str(delta_cny), "reason": reason},
        )
        # 重复改账请求返回首次受理结果，不再受当前版本状态影响。
        if stored is not None:
            if stored["request_sha256"] != request_digest:
                raise Conflict("幂等键对应不同改账内容")
            return json.loads(stored["response_json"])
        row = self._application(application_id)
        if row["state"] not in ("reserved", "settled"):
            raise InvalidState("当前状态的分摊行不允许改账")
        base_mwh = Decimal(row["reserved_mwh" if row["state"] == "reserved" else "settled_mwh"])
        base_cny = Decimal(row["reserved_cny" if row["state"] == "reserved" else "settled_cny"])
        if base_mwh + delta_mwh < 0 or base_cny + delta_cny < 0:
            raise ValidationFailed("改账后数量或金额不能为负")
        now = self._now()
        with transaction(self.connection, immediate=True):
            try:
                cursor = self.connection.execute(
                    "INSERT INTO manual_adjustments(application_id,delta_mwh,delta_cny,reason,"
                    "idempotency_key,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                    (
                        application_id,
                        decimal_text(quantize_volume(delta_mwh)),
                        decimal_text(quantize_money(delta_cny)),
                        reason,
                        key,
                        actor_id,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict("改账幂等键冲突") from exc
            adjustment_id = int(cursor.lastrowid)
            self._app_event(application_id, "adjustment.requested", actor_id, {
                "adjustment_id": adjustment_id,
                "delta_mwh": decimal_text(quantize_volume(delta_mwh)),
                "delta_cny": decimal_text(quantize_money(delta_cny)),
            })
            self._audit("manual_adjustment", str(adjustment_id), "adjustment.requested", actor_id, {
                "application_id": application_id,
            })
            self.connection.execute(
                "INSERT INTO rev_idempotency(scope,idempotency_key,request_sha256,response_json,created_at) "
                "VALUES('adjust',?,?,?,?)",
                (key, request_digest, canonical_json({"adjustment_id": adjustment_id, "state": "pending"}), now),
            )
        return {"adjustment_id": adjustment_id, "state": "pending"}

    def review_adjustment(
        self,
        actor_id: str,
        adjustment_id: int,
        approve: bool,
        note: str,
    ) -> dict[str, Any]:
        self._require(actor_id, "adjustment.review")
        with transaction(self.connection, immediate=True):
            row = self.connection.execute(
                "SELECT * FROM manual_adjustments WHERE adjustment_id=?", (adjustment_id,)
            ).fetchone()
            if row is None:
                raise NotFound("改账申请不存在")
            if row["state"] != "pending":
                raise InvalidState("改账申请已复核")
            if row["created_by"] == actor_id:
                raise Forbidden("人工改账必须由提交人之外的人员复核")
            now = self._now()
            new_revision: int | None = None
            if approve:
                app = self._application(row["application_id"])
                if app["state"] not in ("reserved", "settled"):
                    raise InvalidState("分摊行当前状态不允许批准改账")
                delta_mwh = Decimal(row["delta_mwh"])
                delta_cny = Decimal(row["delta_cny"])
                if app["state"] == "reserved":
                    mwh_field, cny_field = "reserved_mwh", "reserved_cny"
                else:
                    mwh_field, cny_field = "settled_mwh", "settled_cny"
                new_mwh = Decimal(app[mwh_field]) + delta_mwh
                new_cny = Decimal(app[cny_field]) + delta_cny
                if new_mwh < 0 or new_cny < 0:
                    raise ValidationFailed("改账后数量或金额不能为负")
                self.connection.execute(
                    f"UPDATE revenue_applications SET {mwh_field}=?,{cny_field}=?,"
                    "quantity_mwh=?,amount_cny=?,revision=revision+1,updated_at=? WHERE application_id=?",
                    (
                        decimal_text(quantize_volume(new_mwh)),
                        decimal_text(quantize_money(new_cny)),
                        decimal_text(quantize_volume(new_mwh)),
                        decimal_text(quantize_money(new_cny)),
                        now,
                        app["application_id"],
                    ),
                )
                new_revision = app["revision"] + 1
                self._app_event(app["application_id"], "adjustment.approved", actor_id, {
                    "adjustment_id": adjustment_id,
                    "delta_mwh": row["delta_mwh"],
                    "delta_cny": row["delta_cny"],
                    "new_revision": new_revision,
                })
            else:
                self._app_event(row["application_id"], "adjustment.rejected", actor_id, {
                    "adjustment_id": adjustment_id,
                })
            self.connection.execute(
                "UPDATE manual_adjustments SET state=?,reviewed_by=?,reviewed_at=?,review_note=? "
                "WHERE adjustment_id=?",
                ("approved" if approve else "rejected", actor_id, now, note.strip(), adjustment_id),
            )
            self._audit("manual_adjustment", str(adjustment_id),
                        "adjustment.approved" if approve else "adjustment.rejected", actor_id, {
                            "application_id": row["application_id"],
                            "note": note.strip(),
                        })
        return {
            "adjustment_id": adjustment_id,
            "state": "approved" if approve else "rejected",
            "new_revision": new_revision,
        }

    # ── 7. 三视图与可解释性 ──────────────────────────────────────

    @staticmethod
    def _station_scope(user: sqlite3.Row) -> str | None:
        return None if user["role"] != "station" else user["station_id"]

    def _application_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "application_id": row["application_id"],
            "run_id": row["run_id"],
            "version_id": row["version_id"],
            "cycle_id": row["cycle_id"],
            "source_id": row["source_id"],
            "unit_id": row["unit_id"],
            "slot_id": row["slot_id"],
            "basis": row["basis"],
            "quantity_mwh": row["quantity_mwh"],
            "unit_price_cny": row["unit_price_cny"],
            "amount_cny": row["amount_cny"],
            "state": row["state"],
            "reserved_mwh": row["reserved_mwh"],
            "settled_mwh": row["settled_mwh"],
            "released_mwh": row["released_mwh"],
            "carried_mwh": row["carried_mwh"],
            "reserved_cny": row["reserved_cny"],
            "settled_cny": row["settled_cny"],
            "carried_cny": row["carried_cny"],
            "revision": row["revision"],
        }

    def _station_application_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        # 场站视图隐藏单价与金额，只保留电量与状态。
        full = self._application_dict(row)
        for key in ("unit_price_cny", "amount_cny", "reserved_cny", "settled_cny", "carried_cny"):
            full.pop(key)
        return full

    def _cycle_overview(self, cycle_id: str, station_id: str | None) -> dict[str, Any]:
        source_sql = (
            "SELECT s.* FROM revenue_sources s WHERE s.cycle_id=? "
            "AND (s.station_id IS NULL OR s.station_id=?) ORDER BY s.use_order,s.source_id"
            if station_id is not None
            else "SELECT * FROM revenue_sources WHERE cycle_id=? ORDER BY use_order,source_id"
        )
        params: tuple[Any, ...] = (cycle_id, station_id) if station_id is not None else (cycle_id,)
        source_rows = self.connection.execute(source_sql, params).fetchall()
        app_sql = "SELECT * FROM revenue_applications WHERE cycle_id=?"
        app_params: list[Any] = [cycle_id]
        if station_id is not None:
            app_sql += " AND version_id IN (SELECT version_id FROM measurement_versions WHERE station_id=?)"
            app_params.append(station_id)
        app_sql += " ORDER BY application_id"
        applications = self.connection.execute(app_sql, app_params).fetchall()
        return {"source_rows": source_rows, "applications": applications}

    def station_view(self, actor_id: str, cycle_id: str) -> dict[str, Any]:
        user = self._require(actor_id, "view.station")
        station_id = user["station_id"]
        data = self._cycle_overview(cycle_id, station_id)
        sources = []
        for row in data["source_rows"]:
            # 场站可见来源名称、类型、适用机组与有效期；看不到额度上限与价格。
            sources.append({
                "source_id": row["source_id"],
                "name": row["name"],
                "kind": row["kind"],
                "eligible_unit_ids": json.loads(row["eligible_units_json"]),
                "valid_from": row["valid_from"],
                "valid_to": row["valid_to"],
                "state": row["state"],
            })
        measurements = self.connection.execute(
            "SELECT version_id,cycle_id,period_start,period_end,state,created_at "
            "FROM measurement_versions WHERE cycle_id=? AND station_id=? ORDER BY created_at",
            (cycle_id, station_id),
        ).fetchall()
        return {
            "view": "station",
            "station_id": station_id,
            "cycle_id": cycle_id,
            "sources": sources,
            "measurements": [dict(row) for row in measurements],
            "applications": [self._station_application_dict(row) for row in data["applications"]],
        }

    def operations_view(self, actor_id: str, cycle_id: str) -> dict[str, Any]:
        self._require(actor_id, "view.operations")
        data = self._cycle_overview(cycle_id, None)
        sources = []
        for row in data["source_rows"]:
            slots = self.connection.execute(
                "SELECT * FROM revenue_source_slots WHERE source_id=? ORDER BY starts_at,slot_id",
                (row["source_id"],),
            ).fetchall()
            sources.append(self._source_dict(row, slots))
        usage = self._live_usage(cycle_id)
        for source in sources:
            used = usage.get(source["source_id"], {"mwh": Decimal(0), "cny": Decimal(0)})
            source["consumed_mwh"] = decimal_text(quantize_volume(used["mwh"]))
            source["consumed_cny"] = decimal_text(quantize_money(used["cny"]))
            if source["limit_type"] == "MWH" and source["cap_amount"] is not None:
                source["remaining_cap_mwh"] = decimal_text(
                    quantize_volume(Decimal(source["cap_amount"]) - used["mwh"])
                )
            elif source["limit_type"] == "CNY" and source["cap_amount"] is not None:
                source["remaining_cap_cny"] = decimal_text(
                    quantize_money(Decimal(source["cap_amount"]) - used["cny"])
                )
        totals = {
            "reserved_mwh": Decimal(0),
            "settled_mwh": Decimal(0),
            "carried_mwh": Decimal(0),
            "settled_cny": Decimal(0),
            "carried_cny": Decimal(0),
        }
        for row in data["applications"]:
            totals["reserved_mwh"] += Decimal(row["reserved_mwh"]) if row["state"] == "reserved" else Decimal(0)
            totals["settled_mwh"] += Decimal(row["settled_mwh"]) if row["state"] == "settled" else Decimal(0)
            totals["carried_mwh"] += Decimal(row["carried_mwh"]) if row["state"] == "carried_forward" else Decimal(0)
            totals["settled_cny"] += Decimal(row["settled_cny"]) if row["state"] == "settled" else Decimal(0)
            totals["carried_cny"] += Decimal(row["carried_cny"]) if row["state"] == "carried_forward" else Decimal(0)
        return {
            "view": "operations",
            "cycle_id": cycle_id,
            "sources": sources,
            "applications": [self._application_dict(row) for row in data["applications"]],
            "totals": {key: decimal_text(quantize_money(value) if key.endswith("cny") else quantize_volume(value))
                       for key, value in totals.items()},
        }

    def audit_view(self, actor_id: str, cycle_id: str) -> dict[str, Any]:
        self._require(actor_id, "view.audit")
        data = self._cycle_overview(cycle_id, None)
        measurements = self.connection.execute(
            "SELECT version_id,station_id,period_start,period_end,state,created_by,created_at,"
            "confirmed_by,confirmed_at FROM measurement_versions WHERE cycle_id=? ORDER BY created_at",
            (cycle_id,),
        ).fetchall()
        adjustments = self.connection.execute(
            "SELECT a.* FROM manual_adjustments a JOIN revenue_applications r ON r.application_id=a.application_id "
            "WHERE r.cycle_id=? ORDER BY a.adjustment_id",
            (cycle_id,),
        ).fetchall()
        return {
            "view": "audit",
            "cycle_id": cycle_id,
            "sources": [
                {"source_id": row["source_id"], "kind": row["kind"], "state": row["state"], "revision": row["revision"]}
                for row in data["source_rows"]
            ],
            "measurements": [dict(row) for row in measurements],
            "applications": [self._application_dict(row) for row in data["applications"]],
            "manual_adjustments": [dict(row) for row in adjustments],
        }

    def explain_application(self, actor_id: str, application_id: int) -> dict[str, Any]:
        """解释一笔收益为什么落到某个来源。"""

        user = self._user(actor_id)
        row = self._application(application_id)
        if user["role"] == "station":
            version = self._measurement(row["version_id"])
            if version["station_id"] != user["station_id"]:
                raise Forbidden("场站只能查看本站收益的解释")
        source_row = self.connection.execute(
            "SELECT * FROM revenue_sources WHERE source_id=?", (row["source_id"],)
        ).fetchone()
        version = self._measurement(row["version_id"])
        slots = self.connection.execute(
            "SELECT * FROM revenue_source_slots WHERE source_id=? ORDER BY starts_at,slot_id",
            (row["source_id"],),
        ).fetchall()
        events = self.connection.execute(
            "SELECT event_type,actor_id,payload_json,created_at FROM application_events "
            "WHERE application_id=? ORDER BY event_id",
            (application_id,),
        ).fetchall()
        day_from, day_to = self._period_days(version)
        eligible = json.loads(source_row["eligible_units_json"])
        reasons: list[str] = [
            f"来源 {source_row['source_id']}（{source_row['name']}）类型为 {source_row['kind']}，使用顺序 {source_row['use_order']}",
            f"计量周期 {day_from}~{day_to} 落在有效期 {source_row['valid_from']}~{source_row['valid_to']} 内",
            (
                f"机组 {row['unit_id']} 在适用机组列表内"
                if not eligible or row["unit_id"] in eligible
                else f"机组 {row['unit_id']} 不适用（数据异常）"
            ),
        ]
        if row["slot_id"] is not None:
            slot = next((item for item in slots if item["slot_id"] == row["slot_id"]), None)
            if slot is not None:
                reasons.append(
                    f"按市场时段 {slot['slot_id']}（{slot['label']}，{slot['starts_at']}~{slot['ends_at']}）"
                    f"单价 {slot['price_cny_per_mwh']} 元/MWh 计价"
                )
        elif source_row["unit_price_cny_per_mwh"] is not None:
            reasons.append(f"按固定单价 {source_row['unit_price_cny_per_mwh']} 元/MWh 计列")
        elif source_row["curtailment_rate_cny_per_mwh"] is not None:
            reasons.append(
                f"基于计量版本 {version['version_id']} 确认的损失电量，按 "
                f"{source_row['curtailment_rate_cny_per_mwh']} 元/MWh 补偿"
            )
        if source_row["limit_type"] != "NONE":
            unit = "MWh" if source_row["limit_type"] == "MWH" else "元"
            reasons.append(f"来源设总额上限 {source_row['cap_amount']} {unit}，分摊时已预占并受余额约束")
        result = {
            "application_id": application_id,
            "source_id": row["source_id"],
            "version_id": row["version_id"],
            "cycle_id": row["cycle_id"],
            "basis": row["basis"],
            "state": row["state"],
            "revision": row["revision"],
            "reasons": reasons,
            "lifecycle": [
                {"event_type": item["event_type"], "actor_id": item["actor_id"],
                 "payload": json.loads(item["payload_json"]), "created_at": item["created_at"]}
                for item in events
            ],
        }
        if user["role"] == "station":
            # 场站解释中同样隐藏金额，只说明计价规则存在。
            result["quantity_mwh"] = row["quantity_mwh"]
        else:
            result["quantity_mwh"] = row["quantity_mwh"]
            result["unit_price_cny"] = row["unit_price_cny"]
            result["amount_cny"] = row["amount_cny"]
        return result

    def audit_chain(self, actor_id: str) -> dict[str, Any]:
        self._require(actor_id, "audit.read")
        rows = self.connection.execute("SELECT * FROM rev_audit_events ORDER BY event_id").fetchall()
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
