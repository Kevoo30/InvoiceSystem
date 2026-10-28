from datetime import date

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from models import (
    Base, Supplier, Material, PurchaseOrder, POLine, DeliveryNote,
    DeliveryLine, Invoice, InvoiceLine,
)
from seed import seed
import matching


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        seed(s)
        yield s


def mat(s, name):
    return s.scalar(select(Material).where(Material.name == name))


def genesis_po(s):
    sup = s.scalar(select(Supplier).where(Supplier.name == "Genesis Chuma Ltd"))
    po = PurchaseOrder(po_number="PO-001", supplier_id=sup.id)
    s.add(po)
    s.flush()
    for name, qty in [("Coast calcium white", 15), ("Bucket", 10), ("Plastic drum", 2)]:
        s.add(POLine(po_id=po.id, material_id=mat(s, name).id, quantity=qty))
    s.flush()
    return sup, po


def deliver(s, po, items):
    note = DeliveryNote(po_id=po.id, delivery_date=date.today())
    s.add(note)
    s.flush()
    for name, qty in items:
        s.add(DeliveryLine(note_id=note.id, material_id=mat(s, name).id, quantity=qty))
    s.flush()


def invoice(s, sup, po, number, items):
    inv = Invoice(supplier_id=sup.id, po_id=po.id, invoice_number=number)
    s.add(inv)
    s.flush()
    for name, qty in items:
        s.add(InvoiceLine(invoice_id=inv.id, material_id=mat(s, name).id, quantity=qty))
    s.commit()
    return inv


def by_name(s, results):
    return {s.get(Material, r.material_id).name: r for r in results}


def test_genesis_chuma_plastic_drum_not_on_delivery_note(session):
    """Real case: 2 plastic drums invoiced but not on the delivery note."""
    sup, po = genesis_po(session)
    deliver(session, po, [("Coast calcium white", 15), ("Bucket", 10)])
    inv = invoice(session, sup, po, "INV-100",
                  [("Coast calcium white", 15), ("Bucket", 10), ("Plastic drum", 2)])

    results = matching.match_invoice(session, inv)
    r = by_name(session, results)

    assert r["Coast calcium white"].status == matching.MATCHED
    assert r["Bucket"].status == matching.MATCHED
    assert r["Plastic drum"].status == matching.NOT_ON_DELIVERY_NOTE
    assert r["Plastic drum"].invoiced_qty == 2 and r["Plastic drum"].delivered_qty == 0
    assert not matching.invoice_is_clear_for_payment(results)


def test_fully_matched_invoice(session):
    sup, po = genesis_po(session)
    deliver(session, po, [("Coast calcium white", 15), ("Bucket", 10), ("Plastic drum", 2)])
    inv = invoice(session, sup, po, "INV-101",
                  [("Coast calcium white", 15), ("Bucket", 10), ("Plastic drum", 2)])
    assert matching.invoice_is_clear_for_payment(matching.match_invoice(session, inv))


def test_partial_deliveries_add_up(session):
    sup, po = genesis_po(session)
    deliver(session, po, [("Coast calcium white", 10)])
    deliver(session, po, [("Coast calcium white", 5)])
    inv = invoice(session, sup, po, "INV-102", [("Coast calcium white", 15)])
    r = by_name(session, matching.match_invoice(session, inv))
    assert r["Coast calcium white"].status == matching.MATCHED


def test_short_delivery(session):
    sup, po = genesis_po(session)
    deliver(session, po, [("Bucket", 8)])
    inv = invoice(session, sup, po, "INV-103", [("Bucket", 10)])
    r = by_name(session, matching.match_invoice(session, inv))
    assert r["Bucket"].status == matching.SHORT_DELIVERED


def test_over_invoiced(session):
    sup, po = genesis_po(session)
    deliver(session, po, [("Bucket", 10)])
    inv = invoice(session, sup, po, "INV-104", [("Bucket", 12)])
    r = by_name(session, matching.match_invoice(session, inv))
    assert r["Bucket"].status == matching.OVER_INVOICED


def test_item_not_ordered(session):
    sup, po = genesis_po(session)
    inv = invoice(session, sup, po, "INV-105", [("Coast calcium", 5)])
    r = by_name(session, matching.match_invoice(session, inv))
    assert r["Coast calcium"].status == matching.NOT_ORDERED


def test_duplicate_invoice_number(session):
    sup, po = genesis_po(session)
    deliver(session, po, [("Bucket", 10)])
    invoice(session, sup, po, "INV-106", [("Bucket", 10)])
    dup = invoice_dup = Invoice(supplier_id=sup.id, po_id=po.id, invoice_number="INV-106")
    # unique constraint blocks it at DB level; the matcher also guards the logic
    session.add(dup)
    with pytest.raises(Exception):
        session.commit()