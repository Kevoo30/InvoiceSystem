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
from models import Material, Supplier, SupplierPrice  # noqa: E402

HEKIWA = "Hekiwa Steel & General Hardware Ltd"
GARDEN = "Garden Vineyard Hardware & Electricals"

# (supplier, material, price in KES, date of the invoice it came from)
PRICES = [
    (HEKIWA, 'Orient Craft cutting disk 9"', "130", date(2026, 10, 19)),
    (HEKIWA, "Binding wire 15 kg small coil", "2300", date(2026, 10, 19)),
    (HEKIWA, "Blade", "110", date(2026, 10, 19)),
    (HEKIWA, "Deformed bar D12 mm", "1080", date(2026, 10, 19)),
    (HEKIWA, "Deformed bar D16 mm", "2090", date(2026, 10, 19)),
    (HEKIWA, "Deformed bar D8 mm", "505", date(2026, 10, 19)),
    (HEKIWA, "Hoop iron 17 kg", "1400", date(2026, 10, 19)),
    (HEKIWA, 'Nails 4" x 50 kg', "7000", date(2026, 10, 19)),
    (GARDEN, "Nyumba cement", "785", date(2026, 10, 20)),
]


def main():
    engine = create_engine(config.DATABASE_URL)
    with Session(engine) as s:
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