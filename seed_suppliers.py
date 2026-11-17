"""Run once:  python seed_suppliers.py
Safe to run again: it only adds what is missing and never changes existing prices."""
from datetime import date

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import config
import docs_models  # noqa: F401  (makes sure the new tables exist)
from models import Base, Material, Supplier, SupplierPrice

SUPPLIERS = [
    "Sodat General Hardware",
    "Richmond Supplies / Patmos Kenya Ltd",
    "Oceanply Hardware Ltd (Ocean)",
    "Genesis Chuma Ltd",
    "Ivory General Merchants Ltd",
    "Garden Vineyard Hardware & Electricals",
    "Hekiwa Steel & General Hardware Ltd",
    "Kiomba Limited",
]

# supplier -> (material, unit, price in KES) from the real documents of 19-20 Oct 2026
PRICES = {
    "Garden Vineyard Hardware & Electricals": [
        ("Nyumba cement", "bag", "785"),
    ],
    "Hekiwa Steel & General Hardware Ltd": [
        ("Cutting Orient Craft", "piece", "130"),
        ("Binding wire 15 kg small coil", "coil", "2300"),
        ("Blade", "piece", "110"),
        ("Deformed bar D12", "piece", "1080"),
        ("Deformed bar D16", "piece", "2090"),
        ("Deformed bar D8", "piece", "505"),
        ("Hoop iron", "roll", "1400"),
        ('Nails 4" x 50 kg', "packet", "7000"),
    ],
}
EFFECTIVE = date(2026, 10, 1)


def main():
    engine = create_engine(config.DATABASE_URL)
    Base.metadata.create_all(engine)
    db = sessionmaker(engine, expire_on_commit=False)()
    added = 0
    sup = {}
    for name in SUPPLIERS:
        s = db.scalar(select(Supplier).where(Supplier.name == name))
        if s is None:
            s = Supplier(name=name)
            db.add(s)
            db.flush()
            added += 1
        sup[name] = s
    for sname, items in PRICES.items():
        s = sup[sname]
        for mname, unit, price in items:
            m = db.scalar(select(Material).where(Material.name == mname))
            if m is None:
                m = Material(name=mname, unit=unit)
                db.add(m)
                db.flush()
            has_price = db.scalar(select(SupplierPrice).where(
                SupplierPrice.supplier_id == s.id, SupplierPrice.material_id == m.id,
                SupplierPrice.unit_price.is_not(None)))
            if has_price is None:
                db.add(SupplierPrice(supplier_id=s.id, material_id=m.id,
                                     unit_price=price, effective_from=EFFECTIVE))
    db.commit()
    print(f"Done. {added} new supplier(s) added; {len(SUPPLIERS)} suppliers in total.")


if __name__ == "__main__":
    main()