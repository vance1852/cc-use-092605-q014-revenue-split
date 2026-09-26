"""确定性的收益分摊、核销与结转计算。

所有函数只依赖入参，不访问时钟与存储，保证同一冻结计量版本
在任何时刻重算都得到完全一致的结果。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP
from typing import Iterable, Mapping, Sequence


ZERO = Decimal("0")
VOLUME_QUANTUM = Decimal("0.001")
MONEY_QUANTUM = Decimal("0.01")


def quantize_volume(value: Decimal) -> Decimal:
    return value.quantize(VOLUME_QUANTUM, rounding=ROUND_HALF_UP)


def quantize_money(value: Decimal) -> Decimal:
    return value.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)


def decimal_text(value: Decimal) -> str:
    return format(value, "f")


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class DemandLine:
    """冻结计量版本中的一条待分摊电量。"""

    line_id: str
    unit_id: str
    energy_kind: str  # generated 上网电量 / curtailed 限电损失电量
    time_bucket: str
    mwh: Decimal
    loss_confirmed: bool


@dataclass(frozen=True, slots=True)
class SourceCandidate:
    """参与某次分摊的来源批次及其规则快照。"""

    source_id: str
    source_type: str  # market / guaranteed / green_certificate / peak_reward / curtailment_comp
    usage_order: int
    scope_unit_ids: tuple[str, ...]  # 空元组表示全场适用
    valid_from: str
    valid_to: str
    cap_mwh: Decimal | None
    available_mwh: Decimal | None  # 上限扣除已预占与已核销后的剩余额度
    unit_price_cny: Decimal | None
    time_prices: Mapping[str, Decimal] = field(default_factory=dict)

    def covers(self, unit_id: str) -> bool:
        return not self.scope_unit_ids or unit_id in self.scope_unit_ids


@dataclass(frozen=True, slots=True)
class Allocation:
    line_id: str
    source_id: str
    mwh: Decimal
    unit_price_cny: Decimal
    amount_cny: Decimal
    reason: str


def _overlaps(valid_from: str, valid_to: str, period_start: str, period_end: str) -> bool:
    return valid_from <= period_end and valid_to >= period_start


def _rejections(source: SourceCandidate, line: DemandLine, period_start: str, period_end: str) -> list[str]:
    reasons: list[str] = []
    if source.source_type == "curtailment_comp" and line.energy_kind != "curtailed":
        reasons.append("限电补偿只承接限电损失电量")
    if source.source_type != "curtailment_comp" and line.energy_kind == "curtailed":
        reasons.append("该来源不承接限电损失电量")
    if not source.covers(line.unit_id):
        reasons.append("机组不在适用范围")
    if not _overlaps(source.valid_from, source.valid_to, period_start, period_end):
        reasons.append("发电周期超出来源有效期")
    if source.available_mwh is not None and source.available_mwh <= ZERO:
        reasons.append("额度已用尽")
    return reasons


def _price_for(source: SourceCandidate, line: DemandLine) -> Decimal | None:
    if source.source_type == "market":
        return source.time_prices.get(line.time_bucket)
    return source.unit_price_cny


def apportion(
    lines: Sequence[DemandLine],
    sources: Sequence[SourceCandidate],
    *,
    period_start: str,
    period_end: str,
) -> dict[str, object]:
    """按使用顺序把每条电量完整分摊给首个可承接的来源。

    限电损失电量在损失确认前不参与分摊，进入 pending_loss 列表。
    任一待分摊电量找不到可承接来源时抛出 ValueError，由调用方整体回滚。
    """

    ordered_sources = sorted(sources, key=lambda item: (item.usage_order, item.source_id))
    remaining: dict[str, Decimal | None] = {
        source.source_id: source.available_mwh for source in ordered_sources
    }
    allocations: list[Allocation] = []
    pending_loss: list[dict[str, str]] = []
    unallocated: list[dict[str, str]] = []
    explanations: dict[str, dict[str, object]] = {}
    ordered_lines = sorted(
        lines, key=lambda item: (item.unit_id, item.energy_kind, item.time_bucket, item.line_id)
    )
    for line in ordered_lines:
        if line.energy_kind == "curtailed" and not line.loss_confirmed:
            pending_loss.append({"line_id": line.line_id, "mwh": decimal_text(quantize_volume(line.mwh))})
            continue
        skipped: list[dict[str, object]] = []
        selected: str | None = None
        for source in ordered_sources:
            rejections = _rejections(source, line, period_start, period_end)
            available = remaining[source.source_id]
            if available is not None and not rejections and available < line.mwh:
                rejections.append(f"剩余额度 {decimal_text(available)} MWh 不足")
            price = _price_for(source, line)
            if not rejections and price is None:
                rejections.append(f"缺少时段 {line.time_bucket} 价格")
            if rejections:
                skipped.append({"source_id": source.source_id, "reasons": rejections})
                continue
            if available is not None:
                remaining[source.source_id] = quantize_volume(available - line.mwh)
            if source.source_type == "market":
                reason = f"按使用顺序命中市场电费来源，时段 {line.time_bucket} 计价"
            elif source.source_type == "curtailment_comp":
                reason = "损失电量已确认，命中限电补偿来源"
            else:
                reason = f"按使用顺序命中{source.source_id}，机组在适用范围内且额度充足"
            allocations.append(
                Allocation(
                    line_id=line.line_id,
                    source_id=source.source_id,
                    mwh=quantize_volume(line.mwh),
                    unit_price_cny=price,
                    amount_cny=quantize_money(line.mwh * price),
                    reason=reason,
                )
            )
            selected = source.source_id
            break
        explanations[line.line_id] = {"selected": selected, "skipped": skipped}
        if selected is None:
            unallocated.append({"line_id": line.line_id, "mwh": decimal_text(quantize_volume(line.mwh))})
    if unallocated:
        detail = ",".join(item["line_id"] for item in unallocated)
        raise ValueError(f"存在无法分摊的计量行: {detail}")
    return {
        "allocations": allocations,
        "pending_loss": pending_loss,
        "explanations": explanations,
        "remaining_quota": {key: (None if value is None else decimal_text(value)) for key, value in remaining.items()},
    }


@dataclass(frozen=True, slots=True)
class ReservationSlice:
    reservation_id: str
    source_id: str
    mwh: Decimal


def settle(
    actual_mwh: Decimal,
    reservations: Sequence[ReservationSlice],
) -> dict[str, object]:
    """按原分摊顺序用实际电量核销预占，返回每条核销量与未核销尾差。"""

    if actual_mwh < ZERO:
        raise ValueError("实际电量不能为负数")
    remaining = quantize_volume(actual_mwh)
    settled: list[dict[str, object]] = []
    for reservation in reservations:
        take = min(reservation.mwh, remaining)
        take = quantize_volume(max(ZERO, take))
        remaining = quantize_volume(remaining - take)
        settled.append(
            {
                "reservation_id": reservation.reservation_id,
                "source_id": reservation.source_id,
                "settled_mwh": take,
                "released_mwh": quantize_volume(reservation.mwh - take),
            }
        )
    return {"settled": settled, "unallocated_mwh": remaining}


def carry_forward(
    reservations: Sequence[ReservationSlice],
    sources: Sequence[SourceCandidate],
    *,
    period_start: str,
    period_end: str,
    unit_id: str,
    energy_kind: str,
) -> dict[str, object]:
    """把未核销预占按目标周期来源规则重新归集，额度不足部分返回释放。"""

    ordered_sources = sorted(sources, key=lambda item: (item.usage_order, item.source_id))
    remaining: dict[str, Decimal | None] = {
        source.source_id: source.available_mwh for source in ordered_sources
    }
    carried: list[dict[str, object]] = []
    released = ZERO
    pseudo_line = DemandLine(unit_id, unit_id, energy_kind, "FLAT", ZERO, True)
    for reservation in sorted(reservations, key=lambda item: item.reservation_id):
        left = quantize_volume(reservation.mwh)
        for source in ordered_sources:
            if left <= ZERO:
                break
            rejections = _rejections(source, pseudo_line, period_start, period_end)
            if rejections:
                continue
            available = remaining[source.source_id]
            if available is not None and available <= ZERO:
                continue
            take = left if available is None else min(left, available)
            take = quantize_volume(take)
            if available is not None:
                remaining[source.source_id] = quantize_volume(available - take)
            left = quantize_volume(left - take)
            carried.append(
                {
                    "from_reservation_id": reservation.reservation_id,
                    "source_id": source.source_id,
                    "mwh": take,
                }
            )
        released += left
    return {
        "carried": carried,
        "released_mwh": quantize_volume(released),
        "remaining_quota": {key: (None if value is None else decimal_text(value)) for key, value in remaining.items()},
    }
