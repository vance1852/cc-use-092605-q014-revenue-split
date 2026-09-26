from __future__ import annotations

import unittest
from decimal import Decimal

from revenue_settlement.allocation import (
    EnergyEntry,
    LossEntry,
    UsageLedger,
    allocate,
    distribute_confirmation,
    resolve_slot_price,
)


def source(source_id, kind, **over):
    base = {
        "source_id": source_id,
        "kind": kind,
        "use_order": over.pop("use_order", 10),
        "limit_type": over.pop("limit_type", "NONE"),
        "eligible_unit_ids": (),
        "unit_price_cny_per_mwh": None,
        "curtailment_rate_cny_per_mwh": None,
        "price_slots": (),
    }
    base.update(over)
    return base


MARKET = source("mkt", "MARKET_ENERGY", price_slots=(
    {"slot_id": "peak", "starts_at": "2026-09-01T08:00:00Z", "price_cny_per_mwh": "450", "eligible_unit_ids": ()},
    {"slot_id": "valley", "starts_at": "2026-09-01T20:00:00Z", "price_cny_per_mwh": "280", "eligible_unit_ids": ()},
))
GUARANTEE = source("gp", "GUARANTEED_PURCHASE", use_order=20, limit_type="MWH",
                   cap_amount="100", unit_price_cny_per_mwh="430")
GREEN = source("gc", "GREEN_CERTIFICATE", use_order=30, eligible_unit_ids=("U1",),
               unit_price_cny_per_mwh="30")
PEAK = source("ps", "PEAK_SHAVING", use_order=40, eligible_unit_ids=("U2",),
              unit_price_cny_per_mwh="15")
LOSS = source("cc", "CURTAILMENT_COMPENSATION", use_order=50, limit_type="MWH",
              cap_amount="8", curtailment_rate_cny_per_mwh="120")


class AllocationTests(unittest.TestCase):
    def test_same_mwh_not_double_counted_between_market_and_guarantee(self) -> None:
        result = allocate(
            [MARKET, GUARANTEE],
            [EnergyEntry("U1", "peak", Decimal("120")), EnergyEntry("U1", "valley", Decimal("80"))],
        )
        energy = [line for line in result["applications"] if line["basis"] == "energy"]
        # 市场无上限吃满全部电量，保障性收购一行都不产生。
        self.assertEqual({line["source_id"] for line in energy}, {"mkt"})
        self.assertEqual(result["unallocated_energy"], [])

    def test_guarantee_cap_only_covers_uncovered_pool_energy(self) -> None:
        capped_market = source("mkt", "MARKET_ENERGY", limit_type="MWH", cap_amount="100", price_slots=(
            {"slot_id": "peak", "starts_at": "s", "price_cny_per_mwh": "450", "eligible_unit_ids": ()},
        ))
        result = allocate(
            [capped_market, GUARANTEE],
            [EnergyEntry("U1", "peak", Decimal("150"))],
        )
        by_source = {}
        for line in result["applications"]:
            by_source.setdefault(line["source_id"], Decimal(0))
            by_source[line["source_id"]] += Decimal(line["quantity_mwh"])
        self.assertEqual(by_source["mkt"], Decimal("100.000"))
        # 保障额度 100 MWh 只覆盖市场未覆盖的 50 MWh，而不是重复计列 150。
        self.assertEqual(by_source["gp"], Decimal("50.000"))
        self.assertEqual(result["unallocated_energy"], [])

    def test_addons_apply_to_full_metered_energy_of_eligible_units(self) -> None:
        result = allocate(
            [MARKET, GREEN, PEAK],
            [EnergyEntry("U1", "peak", Decimal("100")), EnergyEntry("U2", "peak", Decimal("40"))],
        )
        gc = [line for line in result["applications"] if line["source_id"] == "gc"]
        ps = [line for line in result["applications"] if line["source_id"] == "ps"]
        self.assertEqual(sum((Decimal(line["quantity_mwh"]) for line in gc), Decimal(0)), Decimal("100.000"))
        self.assertEqual(sum((Decimal(line["quantity_mwh"]) for line in ps), Decimal(0)), Decimal("40.000"))

    def test_loss_compensation_requires_confirmed_loss(self) -> None:
        # 冻结阶段（无 loss_entries）不产生任何补偿行。
        frozen = allocate([LOSS], [EnergyEntry("U1", "peak", Decimal("100"))])
        self.assertEqual(frozen["applications"], [])
        confirmed = allocate(
            [LOSS], [EnergyEntry("U1", "peak", Decimal("100"))],
            [LossEntry("U1", Decimal("20"))],
        )
        line = confirmed["applications"][0]
        self.assertEqual(line["basis"], "loss")
        self.assertEqual(line["quantity_mwh"], "8.000")  # 受 8 MWh 额度上限约束
        self.assertEqual(line["amount_cny"], "960.00")

    def test_existing_usage_reduces_capacity(self) -> None:
        ledger = UsageLedger({"cc": {"mwh": Decimal("5"), "cny": Decimal("600")}})
        result = allocate(
            [LOSS], [], [LossEntry("U1", Decimal("20"))],
            {"cc": {"mwh": Decimal("5"), "cny": Decimal("600")}},
        )
        self.assertEqual(result["applications"][0]["quantity_mwh"], "3.000")
        self.assertIsNone(ledger.remaining(MARKET)[0])

    def test_slot_price_unit_restriction(self) -> None:
        restricted = dict(MARKET)
        restricted["price_slots"] = (
            {"slot_id": "peak", "starts_at": "s", "price_cny_per_mwh": "450",
             "eligible_unit_ids": ("OTHER",)},
        )
        self.assertIsNone(resolve_slot_price(restricted, "U1", "peak"))
        slot_id, price = resolve_slot_price(MARKET, "U1", "valley")
        self.assertEqual((slot_id, price), ("valley", Decimal("280")))


class ConfirmationTests(unittest.TestCase):
    def test_partial_confirmation_settles_in_sequence_and_releases_rest(self) -> None:
        reserved = [
            {"application_id": 1, "basis": "energy", "unit_id": "U1", "slot_id": "peak",
             "quantity_mwh": "100.000", "reserved_cny": "45000.00"},
            {"application_id": 2, "basis": "energy", "unit_id": "U1", "slot_id": "peak",
             "quantity_mwh": "50.000", "reserved_cny": "21500.00"},
        ]
        result = distribute_confirmation(
            reserved, {("energy", "U1", "peak"): Decimal("120")}
        )
        self.assertEqual(result[0]["settled_mwh"], "100.000")
        self.assertEqual(result[0]["released_mwh"], "0.000")
        self.assertEqual(result[1]["settled_mwh"], "20.000")
        self.assertEqual(result[1]["released_mwh"], "30.000")
        self.assertEqual(result[1]["settled_cny"], "8600.00")

    def test_energy_and_loss_keys_are_distinct(self) -> None:
        reserved = [
            {"application_id": 1, "basis": "loss", "unit_id": "U1", "slot_id": None,
             "quantity_mwh": "10.000", "reserved_cny": "1200.00"},
        ]
        result = distribute_confirmation(
            reserved, {("energy", "U1", None): Decimal("999"), ("loss", "U1", None): Decimal("4")}
        )
        self.assertEqual(result[0]["settled_mwh"], "4.000")
        self.assertEqual(result[0]["released_mwh"], "6.000")


if __name__ == "__main__":
    unittest.main()
