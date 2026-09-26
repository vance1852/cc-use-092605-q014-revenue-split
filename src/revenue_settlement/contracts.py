"""收益归集领域输入契约。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Mapping

from .engine import ZERO
from .errors import ValidationFailed


IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{1,63}$")
SOURCE_TYPES = {"market", "guaranteed", "green_certificate", "peak_reward", "curtailment_comp"}
ENERGY_KINDS = {"generated", "curtailed"}
TIME_BUCKETS = {"PEAK", "FLAT", "VALLEY"}
PERIOD_STATUSES = {"open", "closed"}


def required_text(value: object, field: str, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationFailed(f"{field} 不能为空")
    result = value.strip()
    if len(result) > maximum:
        raise ValidationFailed(f"{field} 不能超过 {maximum} 个字符")
    return result


def identifier(value: object, field: str) -> str:
    result = required_text(value, field, 64)
    if not IDENTIFIER.fullmatch(result):
        raise ValidationFailed(f"{field} 格式不正确")
    return result


def decimal_value(
    value: object,
    field: str,
    *,
    minimum: Decimal | None = None,
    maximum: Decimal | None = None,
) -> Decimal:
    if isinstance(value, bool):
        raise ValidationFailed(f"{field} 必须是数值")
    try:
        result = Decimal(str(value))
    except Exception as exc:
        raise ValidationFailed(f"{field} 必须是十进制数值") from exc
    if not result.is_finite():
        raise ValidationFailed(f"{field} 必须是有限数值")
    if minimum is not None and result < minimum:
        raise ValidationFailed(f"{field} 不能小于 {minimum}")
    if maximum is not None and result > maximum:
        raise ValidationFailed(f"{field} 不能大于 {maximum}")
    return result


def date_text(value: object, field: str) -> str:
    result = required_text(value, field, 10)
    try:
        return date.fromisoformat(result).isoformat()
    except ValueError as exc:
        raise ValidationFailed(f"{field} 必须是 YYYY-MM-DD 日期") from exc


def _scope_units(value: object) -> tuple[str, ...]:
    if value in (None, [], "*"):
        return ()
    if not isinstance(value, (list, tuple)) or not value:
        raise ValidationFailed("scope_unit_ids 必须是机组编号数组或 *")
    units = tuple(sorted({identifier(item, "scope_unit_ids 元素") for item in value}))
    return units


def _time_prices(value: object) -> dict[str, Decimal]:
    if not isinstance(value, Mapping):
        raise ValidationFailed("time_prices 必须是时段价格对象")
    prices: dict[str, Decimal] = {}
    for bucket, price in value.items():
        name = required_text(bucket, "time_prices 键", 16).upper()
        if name not in TIME_BUCKETS:
            raise ValidationFailed("time_prices 时段必须是 PEAK、FLAT 或 VALLEY")
        prices[name] = decimal_value(price, f"time_prices.{name}", minimum=ZERO)
    return prices


@dataclass(frozen=True, slots=True)
class UnitRegistration:
    unit_id: str
    station_id: str
    name: str
    capacity_mw: Decimal

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "UnitRegistration":
        return cls(
            unit_id=identifier(raw.get("unit_id"), "unit_id"),
            station_id=identifier(raw.get("station_id"), "station_id"),
            name=required_text(raw.get("name"), "name"),
            capacity_mw=decimal_value(raw.get("capacity_mw"), "capacity_mw", minimum=ZERO),
        )


@dataclass(frozen=True, slots=True)
class PeriodRegistration:
    period_id: str
    start_date: str
    end_date: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "PeriodRegistration":
        start = date_text(raw.get("start_date"), "start_date")
        end = date_text(raw.get("end_date"), "end_date")
        if end < start:
            raise ValidationFailed("end_date 不能早于 start_date")
        return cls(
            period_id=identifier(raw.get("period_id"), "period_id"),
            start_date=start,
            end_date=end,
        )


@dataclass(frozen=True, slots=True)
class SourceRegistration:
    source_id: str
    source_type: str
    usage_order: int
    scope_unit_ids: tuple[str, ...]
    valid_from: str
    valid_to: str
    cap_mwh: Decimal | None
    unit_price_cny: Decimal | None
    time_prices: Mapping[str, Decimal]

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "SourceRegistration":
        source_type = required_text(raw.get("source_type"), "source_type", 32)
        if source_type not in SOURCE_TYPES:
            raise ValidationFailed("source_type 不是受支持的来源类型")
        order = raw.get("usage_order")
        if isinstance(order, bool) or not isinstance(order, int) or not 1 <= order <= 9999:
            raise ValidationFailed("usage_order 必须是 1 到 9999 的整数")
        valid_from = date_text(raw.get("valid_from"), "valid_from")
        valid_to = date_text(raw.get("valid_to"), "valid_to")
        if valid_to < valid_from:
            raise ValidationFailed("valid_to 不能早于 valid_from")
        cap_raw = raw.get("cap_mwh")
        cap = None if cap_raw is None else decimal_value(cap_raw, "cap_mwh", minimum=Decimal("0.001"))
        unit_price_raw = raw.get("unit_price_cny")
        unit_price = None if unit_price_raw is None else decimal_value(
            unit_price_raw, "unit_price_cny", minimum=ZERO
        )
        time_prices = _time_prices(raw.get("time_prices", {}))
        if source_type == "market":
            missing = TIME_BUCKETS - set(time_prices)
            if missing:
                raise ValidationFailed("市场电费来源必须提供 PEAK、FLAT、VALLEY 全时段价格")
        elif unit_price is None:
            raise ValidationFailed("非市场来源必须提供 unit_price_cny")
        if source_type == "market" and cap is not None:
            raise ValidationFailed("市场电费来源不设额度上限")
        return cls(
            source_id=identifier(raw.get("source_id"), "source_id"),
            source_type=source_type,
            usage_order=order,
            scope_unit_ids=_scope_units(raw.get("scope_unit_ids")),
            valid_from=valid_from,
            valid_to=valid_to,
            cap_mwh=cap,
            unit_price_cny=unit_price,
            time_prices=time_prices,
        )


@dataclass(frozen=True, slots=True)
class MeteringLineInput:
    line_id: str
    unit_id: str
    energy_kind: str
    time_bucket: str
    mwh: Decimal

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "MeteringLineInput":
        energy_kind = required_text(raw.get("energy_kind"), "energy_kind", 16)
        if energy_kind not in ENERGY_KINDS:
            raise ValidationFailed("energy_kind 必须是 generated 或 curtailed")
        bucket = required_text(raw.get("time_bucket"), "time_bucket", 16).upper()
        if bucket not in TIME_BUCKETS:
            raise ValidationFailed("time_bucket 必须是 PEAK、FLAT 或 VALLEY")
        return cls(
            line_id=identifier(raw.get("line_id"), "line_id"),
            unit_id=identifier(raw.get("unit_id"), "unit_id"),
            energy_kind=energy_kind,
            time_bucket=bucket,
            mwh=decimal_value(raw.get("mwh"), "mwh", minimum=Decimal("0.001")),
        )


@dataclass(frozen=True, slots=True)
class MeteringVersionInput:
    metering_version_id: str
    period_id: str
    lines: tuple[MeteringLineInput, ...]

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "MeteringVersionInput":
        lines_raw = raw.get("lines")
        if not isinstance(lines_raw, (list, tuple)) or not lines_raw:
            raise ValidationFailed("lines 必须是非空计量行数组")
        lines = tuple(MeteringLineInput.from_dict(item) for item in lines_raw)
        line_ids = [line.line_id for line in lines]
        if len(set(line_ids)) != len(line_ids):
            raise ValidationFailed("lines 内 line_id 不能重复")
        return cls(
            metering_version_id=identifier(raw.get("metering_version_id"), "metering_version_id"),
            period_id=identifier(raw.get("period_id"), "period_id"),
            lines=lines,
        )


@dataclass(frozen=True, slots=True)
class AdjustmentInput:
    run_id: int
    reason: str
    line_deltas: Mapping[str, Decimal]

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "AdjustmentInput":
        run_raw = raw.get("run_id")
        if isinstance(run_raw, bool) or not isinstance(run_raw, int) or run_raw <= 0:
            raise ValidationFailed("run_id 必须是正整数")
        deltas_raw = raw.get("line_deltas")
        if not isinstance(deltas_raw, Mapping) or not deltas_raw:
            raise ValidationFailed("line_deltas 必须是非空对象")
        deltas = {
            identifier(line_id, "line_deltas 键"): decimal_value(delta, f"line_deltas.{line_id}")
            for line_id, delta in deltas_raw.items()
        }
        if any(delta == ZERO for delta in deltas.values()):
            raise ValidationFailed("line_deltas 调整量不能为零")
        return cls(
            run_id=run_raw,
            reason=required_text(raw.get("reason"), "reason", 512),
            line_deltas=deltas,
        )
