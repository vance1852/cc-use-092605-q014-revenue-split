"""确定性的收益归集分摊计算。

规则要点：
- MARKET_ENERGY 与 GUARANTEED_PURCHASE 共享同一个上网电量池，按来源使用顺序依次占用，
  保障额度受 cap 限制；同一兆瓦时不会在两个来源重复计列，池中未被覆盖的电量单独报告。
- GREEN_CERTIFICATE 与 PEAK_SHAVING 是环境/服务属性收益，按适用机组对全部计量电量计列，
  各自受额度上限约束，不消耗电量池。
- CURTAILMENT_COMPENSATION 只对“已确认”的损失电量计列，冻结阶段不产生分摊。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Mapping, Sequence


ZERO = Decimal("0")
HUNDRED = Decimal("100")

ENERGY_KINDS = ("MARKET_ENERGY", "GUARANTEED_PURCHASE")
ADDON_KINDS = ("GREEN_CERTIFICATE", "PEAK_SHAVING")
LOSS_KIND = "CURTAILMENT_COMPENSATION"


def quantize_volume(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)


def quantize_money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def decimal_text(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class EnergyEntry:
    unit_id: str
    slot_id: str | None
    energy_mwh: Decimal


@dataclass(frozen=True, slots=True)
class LossEntry:
    unit_id: str
    curtailment_mwh: Decimal


def _eligible_units(source: Mapping[str, object]) -> frozenset[str]:
    return frozenset(source.get("eligible_unit_ids") or ())  # type: ignore[arg-type]


def _unit_eligible(source: Mapping[str, object], unit_id: str) -> bool:
    units = _eligible_units(source)
    return not units or unit_id in units


def resolve_slot_price(
    source: Mapping[str, object],
    unit_id: str,
    slot_id: str | None,
) -> tuple[str, Decimal] | None:
    """返回 (时段编号, 单价)；找不到适用时段返回 None。"""

    slots = sorted(source.get("price_slots") or (), key=lambda item: (str(item["starts_at"]), str(item["slot_id"])))  # type: ignore[arg-type]
    for slot in slots:
        allowed = frozenset(slot.get("eligible_unit_ids") or ())
        if allowed and unit_id not in allowed:
            continue
        if slot_id is not None and slot["slot_id"] != slot_id:
            continue
        return str(slot["slot_id"]), Decimal(str(slot["price_cny_per_mwh"]))
    return None


def _source_price(source: Mapping[str, object]) -> Decimal:
    return Decimal(str(source.get("unit_price_cny_per_mwh") or 0))


def _rate(source: Mapping[str, object]) -> Decimal:
    return Decimal(str(source.get("curtailment_rate_cny_per_mwh") or 0))


class UsageLedger:
    """按来源累计已占用/已核销额度，供多来源分摊时扣减上限。"""

    def __init__(self, usage: Mapping[str, Mapping[str, Decimal]] | None = None) -> None:
        self._mwh: dict[str, Decimal] = {}
        self._cny: dict[str, Decimal] = {}
        for source_id, amounts in (usage or {}).items():
            self._mwh[source_id] = Decimal(str(amounts.get("mwh", 0)))
            self._cny[source_id] = Decimal(str(amounts.get("cny", 0)))

    def remaining(self, source: Mapping[str, object]) -> tuple[Decimal | None, Decimal | None]:
        source_id = str(source["source_id"])
        limit_type = source.get("limit_type", "NONE")
        cap = source.get("cap_amount")
        if cap is None or limit_type == "NONE":
            return None, None
        cap_value = Decimal(str(cap))
        if limit_type == "MWH":
            return max(ZERO, quantize_volume(cap_value - self._mwh.get(source_id, ZERO))), None
        return None, max(ZERO, quantize_money(cap_value - self._cny.get(source_id, ZERO)))

    def consume(self, source_id: str, quantity: Decimal, amount: Decimal) -> None:
        self._mwh[source_id] = self._mwh.get(source_id, ZERO) + quantity
        self._cny[source_id] = self._cny.get(source_id, ZERO) + amount

    def as_dict(self) -> dict[str, dict[str, str]]:
        return {
            source_id: {
                "consumed_mwh": decimal_text(quantize_volume(self._mwh.get(source_id, ZERO))),
                "consumed_cny": decimal_text(quantize_money(self._cny.get(source_id, ZERO))),
            }
            for source_id in sorted(set(self._mwh) | set(self._cny))
        }


def _ordered(sources: Sequence[Mapping[str, object]], kinds: tuple[str, ...]) -> list[Mapping[str, object]]:
    return sorted(
        (item for item in sources if item["kind"] in kinds),
        key=lambda item: (int(item["use_order"]), str(item["source_id"])),  # type: ignore[arg-type]
    )


def _priced_line(
    source: Mapping[str, object],
    unit_id: str,
    slot_id: str | None,
    basis: str,
    quantity: Decimal,
    price: Decimal,
) -> dict[str, object]:
    amount = quantize_money(quantity * price)
    return {
        "source_id": str(source["source_id"]),
        "kind": str(source["kind"]),
        "unit_id": unit_id,
        "slot_id": slot_id,
        "basis": basis,
        "quantity_mwh": decimal_text(quantize_volume(quantity)),
        "unit_price_cny": decimal_text(price),
        "amount_cny": decimal_text(amount),
    }


def _cap_limited_take(
    source: Mapping[str, object],
    quantity: Decimal,
    price: Decimal,
    ledger: UsageLedger,
) -> Decimal:
    remaining_mwh, remaining_cny = ledger.remaining(source)
    take = quantity
    if remaining_mwh is not None:
        take = min(take, remaining_mwh)
    if remaining_cny is not None and price > 0:
        take = min(take, quantize_volume(remaining_cny / price))
    return quantize_volume(max(ZERO, take))


def allocate(
    sources: Sequence[Mapping[str, object]],
    energy_entries: Sequence[EnergyEntry],
    loss_entries: Sequence[LossEntry] = (),
    usage: Mapping[str, Mapping[str, Decimal]] | None = None,
) -> dict[str, object]:
    """对冻结计量或已确认损失执行确定性分摊。

    energy_entries 非空时按使用顺序在电量池来源间分配（同一 MWh 只计一次）；
    add-on 来源按适用机组的全部计量电量独立计列；
    loss_entries 非空（确认后）才产生限电补偿行。
    """

    ledger = UsageLedger(usage)
    applications: list[dict[str, object]] = []

    # 1) 电量池来源：市场电费（时段价）与保障性收购（固定价、有额度）。
    pool: dict[tuple[str, str], Decimal] = {}
    for entry in energy_entries:
        key = (entry.unit_id, entry.slot_id or "")
        pool[key] = quantize_volume(pool.get(key, ZERO) + entry.energy_mwh)
    for source in _ordered(sources, ENERGY_KINDS):
        for unit_id, slot_key in sorted(pool):
            remaining_qty = pool[(unit_id, slot_key)]
            if remaining_qty <= 0 or not _unit_eligible(source, unit_id):
                continue
            slot_id: str | None = slot_key or None
            if source["kind"] == "MARKET_ENERGY":
                priced = resolve_slot_price(source, unit_id, slot_id)
                if priced is None:
                    continue
                slot_id, price = priced
            else:
                price = _source_price(source)
            take = _cap_limited_take(source, remaining_qty, price, ledger)
            if take <= 0:
                continue
            line = _priced_line(source, unit_id, slot_id, "energy", take, price)
            applications.append(line)
            ledger.consume(str(source["source_id"]), take, Decimal(str(line["amount_cny"])))
            pool[(unit_id, slot_key)] = quantize_volume(remaining_qty - take)

    unallocated = [
        {"unit_id": unit_id, "slot_id": slot_key or None, "quantity_mwh": decimal_text(quantity)}
        for (unit_id, slot_key), quantity in sorted(pool.items())
        if quantity > 0
    ]

    # 2) 绿证、调峰奖励：按适用机组对全部计量电量独立计列。
    addon_basis = sorted(
        ((entry.unit_id, entry.slot_id or "", entry.energy_mwh) for entry in energy_entries),
        key=lambda item: (item[0], item[1]),
    )
    for source in _ordered(sources, ADDON_KINDS):
        price = _source_price(source)
        for unit_id, slot_key, quantity in addon_basis:
            if quantity <= 0 or not _unit_eligible(source, unit_id):
                continue
            take = _cap_limited_take(source, quantity, price, ledger)
            if take <= 0:
                continue
            line = _priced_line(source, unit_id, slot_key or None, "energy", take, price)
            applications.append(line)
            ledger.consume(str(source["source_id"]), take, Decimal(str(line["amount_cny"])))

    # 3) 限电补偿：仅对已确认损失电量计列。
    for source in _ordered(sources, (LOSS_KIND,)):
        price = _rate(source)
        for entry in sorted(loss_entries, key=lambda item: item.unit_id):
            if entry.curtailment_mwh <= 0 or not _unit_eligible(source, entry.unit_id):
                continue
            take = _cap_limited_take(source, entry.curtailment_mwh, price, ledger)
            if take <= 0:
                continue
            line = _priced_line(source, entry.unit_id, None, "loss", take, price)
            applications.append(line)
            ledger.consume(str(source["source_id"]), take, Decimal(str(line["amount_cny"])))

    return {
        "applications": applications,
        "unallocated_energy": unallocated,
        "source_usage": ledger.as_dict(),
    }


def distribute_confirmation(
    reserved: Sequence[Mapping[str, object]],
    confirmed: Mapping[tuple[str, str, str | None], Decimal],
) -> list[dict[str, object]]:
    """把实际确认电量按预占顺序核销到保留行，返回每行的核销/释放量与金额。

    confirmed 的键为 (basis, unit_id, slot_id)，basis 取 energy 或 loss；
    reserved 必须已按创建顺序排列；金额按实际/预占比例摊销。
    """

    remaining = {key: quantize_volume(value) for key, value in confirmed.items()}
    results: list[dict[str, object]] = []
    for app in reserved:
        unit_id = str(app["unit_id"])
        slot_id = app["slot_id"]
        slot_value = None if slot_id is None else str(slot_id)
        key = (str(app["basis"]), unit_id, slot_value)
        reserved_qty = Decimal(str(app["quantity_mwh"]))
        reserved_amount = Decimal(str(app["reserved_cny"]))
        settled_qty = min(reserved_qty, remaining.get(key, ZERO))
        settled_qty = quantize_volume(max(ZERO, settled_qty))
        if reserved_qty > 0:
            settled_amount = quantize_money(reserved_amount * settled_qty / reserved_qty)
        else:
            settled_amount = ZERO
        if settled_qty > 0:
            remaining[key] = quantize_volume(remaining[key] - settled_qty)
        results.append({
            "application_id": app["application_id"],
            "settled_mwh": decimal_text(settled_qty),
            "released_mwh": decimal_text(quantize_volume(reserved_qty - settled_qty)),
            "settled_cny": decimal_text(settled_amount),
        })
    return results
