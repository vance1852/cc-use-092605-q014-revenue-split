"""收益归集领域输入契约。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from .errors import ValidationFailed


IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{1,63}$")

# 收益来源类型：市场电费按时段计价；保障性收购有额度上限；绿证、调峰奖励按机组适用；限电补偿依赖已确认损失电量。
SOURCE_KINDS = {"MARKET_ENERGY", "GUARANTEED_PURCHASE", "GREEN_CERTIFICATE", "PEAK_SHAVING", "CURTAILMENT_COMPENSATION"}
LIMIT_TYPES = {"MWH", "CNY", "NONE"}


def required_text(value: object, name: str, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationFailed(f"{name} 不能为空")
    result = value.strip()
    if len(result) > maximum:
        raise ValidationFailed(f"{name} 不能超过 {maximum} 个字符")
    return result


def identifier(value: object, name: str) -> str:
    result = required_text(value, name, 64)
    if not IDENTIFIER.fullmatch(result):
        raise ValidationFailed(f"{name} 格式不正确")
    return result


def optional_identifier(value: object, name: str) -> str | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return identifier(value, name)


def decimal_value(value: object, name: str, *, minimum: Decimal | None = None) -> Decimal:
    if isinstance(value, bool):
        raise ValidationFailed(f"{name} 必须是数值")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValidationFailed(f"{name} 必须是十进制数值") from exc
    if not result.is_finite():
        raise ValidationFailed(f"{name} 必须是有限数值")
    if minimum is not None and result < minimum:
        raise ValidationFailed(f"{name} 不能小于 {minimum}")
    return result


def date_text(value: object, name: str) -> str:
    result = required_text(value, name, 10)
    try:
        return date.fromisoformat(result).isoformat()
    except ValueError as exc:
        raise ValidationFailed(f"{name} 必须是 YYYY-MM-DD 日期") from exc


def positive_integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValidationFailed(f"{name} 必须是正整数")
    return value


def _identifier_list(raw: object, name: str) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, (list, tuple)):
        raise ValidationFailed(f"{name} 必须是编号数组")
    result = [identifier(item, f"{name}[]") for item in raw]
    if len(set(result)) != len(result):
        raise ValidationFailed(f"{name} 不能包含重复编号")
    return tuple(result)


@dataclass(frozen=True, slots=True)
class PriceSlot:
    """一个计价时段：按 MWh 给价，可选按机组限定。"""

    slot_id: str
    label: str
    starts_at: str
    ends_at: str
    price_cny_per_mwh: Decimal
    eligible_unit_ids: tuple[str, ...]

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "PriceSlot":
        starts_at = required_text(raw.get("starts_at"), "price_slots.starts_at", 40)
        ends_at = required_text(raw.get("ends_at"), "price_slots.ends_at", 40)
        if ends_at <= starts_at:
            raise ValidationFailed("计价时段 ends_at 必须晚于 starts_at")
        return cls(
            slot_id=identifier(raw.get("slot_id"), "price_slots.slot_id"),
            label=required_text(raw.get("label"), "price_slots.label", 64),
            starts_at=starts_at,
            ends_at=ends_at,
            price_cny_per_mwh=decimal_value(raw.get("price_cny_per_mwh"), "price_slots.price_cny_per_mwh", minimum=Decimal("0")),
            eligible_unit_ids=_identifier_list(raw.get("eligible_unit_ids"), "price_slots.eligible_unit_ids"),
        )


@dataclass(frozen=True, slots=True)
class RevenueSource:
    """结算依据（来源批次）：适用范围、有效期、上限与使用顺序。"""

    source_id: str
    name: str
    kind: str
    cycle_id: str
    station_id: str | None
    eligible_unit_ids: tuple[str, ...]
    valid_from: str
    valid_to: str
    use_order: int
    limit_type: str
    cap_amount: Decimal | None
    unit_price_cny_per_mwh: Decimal | None
    price_slots: tuple[PriceSlot, ...]
    curtailment_rate_cny_per_mwh: Decimal | None

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "RevenueSource":
        kind = required_text(raw.get("kind"), "kind", 32).upper()
        if kind not in SOURCE_KINDS:
            raise ValidationFailed("kind 必须是 MARKET_ENERGY、GUARANTEED_PURCHASE、GREEN_CERTIFICATE、PEAK_SHAVING 或 CURTAILMENT_COMPENSATION")
        valid_from = date_text(raw.get("valid_from"), "valid_from")
        valid_to = date_text(raw.get("valid_to"), "valid_to")
        if valid_to < valid_from:
            raise ValidationFailed("valid_to 不能早于 valid_from")
        limit_type = required_text(raw.get("limit_type", "NONE"), "limit_type", 8).upper()
        if limit_type not in LIMIT_TYPES:
            raise ValidationFailed("limit_type 必须是 MWH、CNY 或 NONE")
        use_order = raw.get("use_order", 100)
        if isinstance(use_order, bool) or not isinstance(use_order, int) or not 1 <= use_order <= 999:
            raise ValidationFailed("use_order 必须是 1 到 999 的整数")
        cap_raw = raw.get("cap_amount")
        cap_amount = None if cap_raw is None else decimal_value(cap_raw, "cap_amount", minimum=Decimal("0"))
        if limit_type == "NONE" and cap_amount is not None:
            raise ValidationFailed("limit_type 为 NONE 时不能设置 cap_amount")
        if limit_type != "NONE" and cap_amount is None:
            raise ValidationFailed("限量来源必须设置 cap_amount")
        slots_raw = raw.get("price_slots", [])
        if not isinstance(slots_raw, list):
            raise ValidationFailed("price_slots 必须是数组")
        slots = tuple(PriceSlot.from_dict(item) for item in slots_raw)
        if kind == "MARKET_ENERGY":
            if not slots:
                raise ValidationFailed("市场电费必须登记至少一个计价时段")
            slot_ids = [slot.slot_id for slot in slots]
            if len(set(slot_ids)) != len(slot_ids):
                raise ValidationFailed("计价时段编号不能重复")
        elif slots:
            raise ValidationFailed("只有市场电费可以登记计价时段")
        rate_raw = raw.get("curtailment_rate_cny_per_mwh")
        rate = None if rate_raw is None else decimal_value(rate_raw, "curtailment_rate_cny_per_mwh", minimum=Decimal("0"))
        price_raw = raw.get("unit_price_cny_per_mwh")
        unit_price = None if price_raw is None else decimal_value(price_raw, "unit_price_cny_per_mwh", minimum=Decimal("0"))
        if kind == "CURTAILMENT_COMPENSATION":
            if rate is None:
                raise ValidationFailed("限电补偿必须给出 curtailment_rate_cny_per_mwh")
            if unit_price is not None:
                raise ValidationFailed("限电补偿单价使用 curtailment_rate_cny_per_mwh")
        elif kind == "MARKET_ENERGY":
            if unit_price is not None:
                raise ValidationFailed("市场电费单价通过 price_slots 登记")
        elif unit_price is None:
            raise ValidationFailed("固定单价来源必须给出 unit_price_cny_per_mwh")
        if kind != "CURTAILMENT_COMPENSATION" and rate is not None:
            raise ValidationFailed("只有限电补偿可以给出 curtailment_rate_cny_per_mwh")
        return cls(
            source_id=identifier(raw.get("source_id"), "source_id"),
            name=required_text(raw.get("name"), "name"),
            kind=kind,
            cycle_id=identifier(raw.get("cycle_id"), "cycle_id"),
            station_id=optional_identifier(raw.get("station_id"), "station_id"),
            eligible_unit_ids=_identifier_list(raw.get("eligible_unit_ids"), "eligible_unit_ids"),
            valid_from=valid_from,
            valid_to=valid_to,
            use_order=use_order,
            limit_type=limit_type,
            cap_amount=cap_amount,
            price_slots=slots,
            curtailment_rate_cny_per_mwh=rate,
            unit_price_cny_per_mwh=unit_price,
        )


@dataclass(frozen=True, slots=True)
class MeteredEnergy:
    """单机组、单时段、分性质的冻结电量（MWh）。"""

    unit_id: str
    slot_id: str | None
    energy_mwh: Decimal
    curtailment_mwh: Decimal


@dataclass(frozen=True, slots=True)
class MeasurementVersion:
    """冻结的计量版本：同一发电周期内每台机组的上网电量与已确认损失电量。"""

    version_id: str
    cycle_id: str
    station_id: str
    period_start: str
    period_end: str
    entries: tuple[MeteredEnergy, ...]

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "MeasurementVersion":
        period_start = required_text(raw.get("period_start"), "period_start", 40)
        period_end = required_text(raw.get("period_end"), "period_end", 40)
        if period_end <= period_start:
            raise ValidationFailed("period_end 必须晚于 period_start")
        entries_raw = raw.get("entries", [])
        if not isinstance(entries_raw, list) or not entries_raw:
            raise ValidationFailed("entries 必须是非空电量明细数组")
        entries: list[MeteredEnergy] = []
        seen: set[tuple[str, str]] = set()
        for item in entries_raw:
            if not isinstance(item, Mapping):
                raise ValidationFailed("电量明细必须是对象")
            unit_id = identifier(item.get("unit_id"), "entries.unit_id")
            slot_id = optional_identifier(item.get("slot_id"), "entries.slot_id")
            key = (unit_id, slot_id or "")
            if key in seen:
                raise ValidationFailed("同一机组同一时段电量不能重复登记")
            seen.add(key)
            entries.append(
                MeteredEnergy(
                    unit_id=unit_id,
                    slot_id=slot_id,
                    energy_mwh=decimal_value(item.get("energy_mwh"), "entries.energy_mwh", minimum=Decimal("0")),
                    curtailment_mwh=decimal_value(item.get("curtailment_mwh", 0), "entries.curtailment_mwh", minimum=Decimal("0")),
                )
            )
        return cls(
            version_id=identifier(raw.get("version_id"), "version_id"),
            cycle_id=identifier(raw.get("cycle_id"), "cycle_id"),
            station_id=identifier(raw.get("station_id"), "station_id"),
            period_start=period_start,
            period_end=period_end,
            entries=tuple(entries),
        )
