"""Upgrade an EXISTING MySQL database to the latest models.

Run once after receiving this update:   python upgrade_db.py
Safe to run again. It only adds what is missing and never deletes data.
(A brand-new database does not need this: seed.py creates everything.)
"""
from dotenv import load_dotenv

load_dotenv()

from sqlalchemy import create_engine, inspect, text  # noqa: E402

import config  # noqa: E402
from models import Base  # noqa: E402


def columns(engine, table):
    return {c["name"]: c for c in inspect(engine).get_columns(table)}


def main():
    engine = create_engine(config.DATABASE_URL)

    # New tables (delivery_allocations, supplier_aliases) are created here.
    Base.metadata.create_all(engine)

    if engine.dialect.name != "mysql":
        print("Not a MySQL database: nothing more to upgrade. "
              "(For a local SQLite test file, delete invoice_matcher.db and run seed.py.)")
        return

    steps = []

    dn = columns(engine, "delivery_notes")
    if "supplier_id" not in dn:
        steps += [
            "ALTER TABLE delivery_notes ADD COLUMN supplier_id INT NULL",
            "UPDATE delivery_notes dn JOIN purchase_orders po ON po.id = dn.po_id "
            "SET dn.supplier_id = po.supplier_id",
            "ALTER TABLE delivery_notes MODIFY supplier_id INT NOT NULL",
            "ALTER TABLE delivery_notes ADD CONSTRAINT fk_delivery_notes_supplier "
            "FOREIGN KEY (supplier_id) REFERENCES suppliers(id)",
        ]
    if not dn["po_id"]["nullable"]:
        steps.append("ALTER TABLE delivery_notes MODIFY po_id INT NULL")

    dl = columns(engine, "delivery_lines")
    if "returned_qty" not in dl:
        steps.append("ALTER TABLE delivery_lines ADD COLUMN returned_qty "
                     "DECIMAL(12,2) NOT NULL DEFAULT 0")

    mr = columns(engine, "match_results")
    if "returned_qty" not in mr:
        steps.append("ALTER TABLE match_results ADD COLUMN returned_qty DECIMAL(12,2) NULL")
    if "unit_price" not in mr:
        steps.append("ALTER TABLE match_results ADD COLUMN unit_price DECIMAL(12,2) NULL")
    if "amount_blocked" not in mr:
        steps.append("ALTER TABLE match_results ADD COLUMN amount_blocked "
                     "DECIMAL(14,2) NULL DEFAULT 0")

    if not steps:
        print("Database is already up to date.")
        return

    with engine.connect() as conn:
        for sql in steps:
            print("Running:", sql)
            conn.execute(text(sql))
        conn.commit()
    print("Upgrade complete.")


if __name__ == "__main__":
    main()