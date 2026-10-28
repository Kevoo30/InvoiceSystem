"""Loads the 8 suppliers and their materials as starting data.

Prices are left empty (None): the admin sets them via the price-change screen.
Quantities already bought are NOT stored here; they belong in purchase orders.
"""
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

import config
from models import Base, Supplier, Material, SupplierPrice

# supplier -> list of (material name, unit)
DATA = {
    "Sodat General Hardware": [
        ('PPR elbow 3/4"', "piece"),
        ("Tangit 1/2", "piece"),
        ('PVC pipe 3"', "piece"),
    ],
    "Richmond Supplies / Patmos Kenya Ltd": [
        ("Rocksand", "truckload"),
        ("Ballast", "truckload"),
    ],
    "Oceanply Hardware Ltd (Ocean)": [
        ('Chicken wire 3/4"', "roll"),
        ("Metallic spade", "piece"),
        ('Steel nails 2" packet', "packet"),
        ('Ordinary nails 2", 1/2 kg', "packet"),
        ("Hoop iron 17 kg", "roll"),
    ],
    "Genesis Chuma Ltd": [
        ("Coast calcium white", "bag"),
        ("Bucket", "piece"),
        ("Plastic drum", "piece"),
    ],
    "Ivory General Merchants Ltd": [
        ('PVC pipe 3"', "piece"),
        ("R-Tee 4x2", "piece"),
        ('PVC Tee 3"', "piece"),
        ("PVC reducer 3x2", "piece"),
        ("4-way trap", "piece"),
        ("Tangit 500 ml", "piece"),
        ('PVC bend 1 1/2" 45 deg', "piece"),
        ('PVC bend 1 1/2"', "piece"),
        ('PVC pipe 3" HG', "piece"),
        ('PVC pipe 1 1/2"', "piece"),
        ('PVC pipe 4"', "piece"),
        ("Hacksaw blades", "piece"),
        ("Metro glue", "piece"),
        ("Insulating tape", "roll"),
        ("Packing tape", "roll"),
        ("Petrol engine pump", "piece"),
        ('Suction pipe 2"', "metre"),
        ('Delivery pipe 2"', "roll"),
        ("Thread seal tape, big", "roll"),
        ('Hose clips 2"', "piece"),
        ("Petrol", "litre"),
        ("Oil", "piece"),
        ("Kitchen sink, single bowl", "piece"),
    ],
    "Garden Vineyard Hardware & Electricals": [
        ("Tembo cement", "bag"),
        ("Nyumba cement", "bag"),
        ("Coast calcium", "bag"),
    ],
    "Hekiwa Steel & General Hardware Ltd": [
        ("Deformed bar D8 mm", "piece"),
        ("Deformed bar D10 mm", "piece"),
        ("Deformed bar D12 mm", "piece"),
        ("Deformed bar D16 mm", "piece"),
        ("Binding wire 15 kg small coil", "coil"),
        ("Blade", "piece"),
        ('Oasis cutting disk 9"', "piece"),
        ('Orient Craft cutting disk 9"', "piece"),
        ('Nails 4" x 50 kg', "bag"),
        ("Hoop iron 17 kg", "roll"),  # quantity still to be confirmed
    ],
    "Kiomba Limited": [
        ("River sand", "load"),
    ],
}


def seed(session: Session) -> None:
    for supplier_name, items in DATA.items():
        supplier = session.scalar(select(Supplier).where(Supplier.name == supplier_name))
        if supplier is None:
            supplier = Supplier(name=supplier_name)
            session.add(supplier)
            session.flush()
        for material_name, unit in items:
            material = session.scalar(select(Material).where(Material.name == material_name))
            if material is None:
                material = Material(name=material_name, unit=unit)
                session.add(material)
                session.flush()
            exists = session.scalar(
                select(SupplierPrice).where(
                    SupplierPrice.supplier_id == supplier.id,
                    SupplierPrice.material_id == material.id,
                )
            )
            if exists is None:
                session.add(SupplierPrice(supplier_id=supplier.id, material_id=material.id, unit_price=None))
    session.commit()


if __name__ == "__main__":
    engine = create_engine(config.DATABASE_URL)
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        seed(s)
    print("Seed complete.")