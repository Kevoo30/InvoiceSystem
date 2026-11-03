"""Load the first real prices, read from the supplier documents (October 2026).

Run once:  python load_prices.py
Safe to run again: a price is only added when it differs from the current one,
and earlier prices stay in the history. Admin can change any of them later on
the Prices page.
"""
from datetime import date
from decimal import Decimal

from dotenv import load_dotenv

load_dotenv()

from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

import config  # noqa: E402
from models import Base, Material, Supplier, SupplierPrice  # noqa: E402
import labour  # noqa: E402,F401
from seed import seed  # noqa: E402

HEKIWA = "Hekiwa Steel & General Hardware Ltd"
GARDEN = "Garden Vineyard Hardware & Electricals"
IVORY = "Ivory General Merchants Ltd"
OCEAN = "Oceanply Hardware Ltd (Ocean)"
GENESIS = "Genesis Chuma Ltd"
SODAT = "Sodat General Hardware"
PATMOS = "Richmond Supplies / Patmos Kenya Ltd"
KIOMBA = "Kiomba Limited"
IRRIG = "Irrigation & Plumbing Supplier (INV/2026/00180)"

# (supplier, material, price in KES, date of the invoice it came from)
PRICES = [
    (HEKIWA, 'Orient Craft cutting disk 9"', "130", date(2026, 10, 19)),
    (HEKIWA, 'Binding wire 15 kg small coil', "2300", date(2026, 10, 19)),
    (HEKIWA, 'Blade', "110", date(2026, 10, 19)),
    (HEKIWA, 'Deformed bar D12 mm', "1080", date(2026, 10, 19)),
    (HEKIWA, 'Deformed bar D16 mm', "2090", date(2026, 10, 19)),
    (HEKIWA, 'Deformed bar D8 mm', "505", date(2026, 10, 19)),
    (HEKIWA, 'Hoop iron 17 kg', "1400", date(2026, 10, 19)),
    (HEKIWA, 'Nails 4" x 50 kg', "7000", date(2026, 10, 19)),
    (GARDEN, 'Nyumba cement', "785", date(2026, 10, 20)),
    (GARDEN, 'Coast calcium', "735", date(2026, 10, 16)),
    (GARDEN, 'Tembo cement', "810", date(2026, 10, 4)),
    (IVORY, 'Metro glue', "290", date(2026, 10, 16)),
    (IVORY, 'Hacksaw blades', "120", date(2026, 10, 16)),
    (IVORY, 'Insulating tape', "120", date(2026, 10, 16)),
    (IVORY, 'Packing tape', "250", date(2026, 10, 16)),
    (IVORY, 'R-Tee 4x2', "350", date(2026, 10, 16)),
    (IVORY, 'PVC Tee 3"', "150", date(2026, 10, 16)),
    (IVORY, 'PVC reducer 3x2', "80", date(2026, 10, 16)),
    (IVORY, '4-way trap', "250", date(2026, 10, 16)),
    (IVORY, 'Tangit 500 ml', "750", date(2026, 10, 16)),
    (IVORY, 'PVC bend 1 1/2"', "35", date(2026, 10, 16)),
    (IVORY, 'PVC pipe 3" HG', "980", date(2026, 10, 16)),
    (IVORY, 'PVC pipe 1 1/2"', "450", date(2026, 10, 16)),
    (IVORY, 'PVC pipe 4"', "1700", date(2026, 10, 16)),
    (IVORY, 'Petrol engine pump', "15500", date(2026, 10, 22)),
    (IVORY, 'Suction pipe 2"', "400", date(2026, 10, 22)),
    (IVORY, 'Delivery pipe 2"', "4950", date(2026, 10, 22)),
    (IVORY, 'Thread seal tape, big', "100", date(2026, 10, 22)),
    (IVORY, 'Hose clips 2"', "190", date(2026, 10, 22)),
    (IVORY, 'Petrol', "100", date(2026, 10, 22)),
    (IVORY, 'Oil', "500", date(2026, 10, 22)),
    (IVORY, 'Kitchen sink, single bowl', "1100", date(2026, 10, 22)),
    (IVORY, 'Boss connector 4x1 1/2"', "80", date(2026, 10, 28)),
    (IVORY, 'R-bush 2x1 1/2"', "50", date(2026, 10, 28)),
    (IVORY, 'PVC pipe 4" HG', "1700", date(2026, 10, 28)),
    (OCEAN, 'Chicken wire 3/4"', "1500", date(2026, 10, 5)),
    (OCEAN, 'Metallic spade', "600", date(2026, 10, 5)),
    (OCEAN, 'Hoop iron 17 kg', "2700", date(2026, 10, 20)),
    (OCEAN, 'Ordinary nails 4" 50 kg bag', "6600", date(2026, 10, 27)),
    (OCEAN, 'Binding wire 17 kg', "2800", date(2026, 10, 27)),
    (OCEAN, 'Ordinary nails 2", 1 kg', "200", date(2026, 10, 27)),
    (OCEAN, 'Ordinary nails 3", 1 kg', "200", date(2026, 10, 27)),
    (OCEAN, 'Ordinary nails 4", 1 kg', "200", date(2026, 10, 27)),
    (OCEAN, 'Frame long 8.4x2 set, without vent', "1200", date(2026, 10, 27)),
    (GENESIS, 'Coast calcium white', "750", date(2026, 10, 5)),
    (GENESIS, 'Bucket', "250", date(2026, 10, 5)),
    (GENESIS, 'Plastic drum', "2500", date(2026, 10, 5)),
    (SODAT, 'PVC pipe 3"', "450", date(2026, 10, 3)),
    (SODAT, 'Tangit 1/2', "800", date(2026, 10, 3)),
    (PATMOS, 'Rocksand', "35000", date(2026, 10, 22)),
    (PATMOS, 'Ballast', "31000", date(2026, 10, 22)),
    (KIOMBA, 'River sand', "93000", date(2026, 10, 22)),
    (IRRIG, 'PPR pipe 3/4" Danco', "350", date(2026, 10, 23)),
    (IRRIG, 'PPR male elbow 25x20', "40", date(2026, 10, 23)),
    (IRRIG, 'PPR female elbow 25x20', "40", date(2026, 10, 23)),
    (IRRIG, 'GI socket 1/2"', "20", date(2026, 10, 23)),
    (IRRIG, 'GI male plug 1/2"', "20", date(2026, 10, 23)),
    (IRRIG, 'GI hex nipple 1/2"', "20", date(2026, 10, 23)),
    (IRRIG, 'PPR male socket 25x20', "40", date(2026, 10, 23)),
    (IRRIG, 'Cobra concealed stopcock', "650", date(2026, 10, 23)),
    (IRRIG, 'PVC pipe 4"', "1000", date(2026, 10, 23)),
    (IRRIG, 'PVC pipe 3"', "450", date(2026, 10, 23)),
    (IRRIG, 'PVC pipe 1 1/2"', "250", date(2026, 10, 23)),
    (IRRIG, 'PVC pipe 2"', "350", date(2026, 10, 23)),
    (IRRIG, 'PVC tee 4"', "200", date(2026, 10, 23)),
    (IRRIG, 'PVC bend 45 deg', "100", date(2026, 10, 23)),
    (IRRIG, 'PVC tee with plug', "120", date(2026, 10, 23)),
    (IRRIG, 'Boss connector', "100", date(2026, 10, 23)),
    (IRRIG, 'Tangit 1000 ml', "1400", date(2026, 10, 23)),
]


def main():
    engine = create_engine(config.DATABASE_URL)
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        seed(s)   # adds any supplier or material that is missing (safe to repeat)
        for supplier_name, material_name, price, when in PRICES:
            supplier = s.scalar(select(Supplier).where(Supplier.name == supplier_name))
            material = s.scalar(select(Material).where(Material.name == material_name))
            if supplier is None or material is None:
                print(f"SKIPPED (not found): {supplier_name} / {material_name}")
                continue
            current = s.scalar(
                select(SupplierPrice)
                .where(SupplierPrice.supplier_id == supplier.id,
                       SupplierPrice.material_id == material.id,
                       SupplierPrice.unit_price.is_not(None))
                .order_by(SupplierPrice.effective_from.desc(), SupplierPrice.id.desc())
                .limit(1))
            value = Decimal(price)
            if current is not None and current.unit_price == value:
                print(f"unchanged: {material_name} at {supplier_name} = {value}")
                continue
            s.add(SupplierPrice(supplier_id=supplier.id, material_id=material.id,
                                unit_price=value, effective_from=when))
            print(f"set: {material_name} at {supplier_name} = {value}")
        s.commit()
    print("Done.")


if __name__ == "__main__":
    main()