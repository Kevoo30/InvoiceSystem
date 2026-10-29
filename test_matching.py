from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

import load_prices
import matching
from models import (
    Base, DeliveryAllocation, DeliveryLine, DeliveryNote, Invoice, InvoiceLine,
    Material, POLine, PurchaseOrder, Supplier, SupplierPrice,
)
from seed import seed

D = Decimal
HEKIWA = "Hekiwa Steel & General Hardware Ltd"
GARDEN = "Garden Vineyard Hardware & Electricals"
GENESIS = "Genesis Chuma Ltd"


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        seed(s)
        yield s


# ------------------------------------------------------------------ helpers
def sup(s, name):
    return s.scalar(select(Supplier).where(Supplier.name == name))


def mat(s, name):
    return s.scalar(select(Material).where(Material.name == name))


def set_price(s, supplier, material, price, effective=date(2026, 1, 1)):
    s.add(SupplierPrice(supplier_id=sup(s, supplier).id, material_id=mat(s, material).id,
                        unit_price=D(str(price)), effective_from=effective))
    s.flush()


def make_po(s, supplier, items, number="PO-001"):
    po = PurchaseOrder(po_number=number, supplier_id=sup(s, supplier).id)
    s.add(po)
    s.flush()
    for name, qty in items:
        s.add(POLine(po_id=po.id, material_id=mat(s, name).id, quantity=qty))
    s.flush()
    return po


def deliver(s, supplier, items, when=date(2026, 10, 20), po=None):
    """items: (material, qty) or (material, qty, returned_qty)."""
    note = DeliveryNote(supplier_id=sup(s, supplier).id, po_id=po.id if po else None,
                        delivery_date=when)
    s.add(note)
    s.flush()
    for it in items:
        s.add(DeliveryLine(note_id=note.id, material_id=mat(s, it[0]).id, quantity=it[1],
                           returned_qty=it[2] if len(it) > 2 else 0))
    s.flush()
    return note


def invoice(s, supplier, number, items, po=None, when=date(2026, 10, 19)):
    """items: (material, qty) or (material, qty, unit_price)."""
    inv = Invoice(supplier_id=sup(s, supplier).id, po_id=po.id if po else None,
                  invoice_number=number, invoice_date=when)
    s.add(inv)
    s.flush()
    for it in items:
        s.add(InvoiceLine(invoice_id=inv.id, material_id=mat(s, it[0]).id, quantity=it[1],
                          unit_price=D(str(it[2])) if len(it) > 2 else None))
    s.commit()
    return inv


def by_name(s, results):
    return {s.get(Material, r.material_id).name: r for r in results}


# ---------------------------------------------- PO-based cases (Genesis Chuma)
def genesis_po(s):
    return make_po(s, GENESIS, [("Coast calcium white", 15), ("Bucket", 10), ("Plastic drum", 2)])


def test_genesis_chuma_plastic_drum_not_on_delivery_note(session):
    """Real case: 2 plastic drums invoiced but not on the delivery note."""
    po = genesis_po(session)
    deliver(session, GENESIS, [("Coast calcium white", 15), ("Bucket", 10)], po=po)
    inv = invoice(session, GENESIS, "INV-100",
                  [("Coast calcium white", 15), ("Bucket", 10), ("Plastic drum", 2)], po=po)
    results = matching.match_invoice(session, inv)
    r = by_name(session, results)
    assert r["Coast calcium white"].status == matching.MATCHED
    assert r["Bucket"].status == matching.MATCHED
    assert r["Plastic drum"].status == matching.NOT_ON_DELIVERY_NOTE
    assert r["Plastic drum"].invoiced_qty == 2 and r["Plastic drum"].delivered_qty == 0
    assert not matching.invoice_is_clear_for_payment(results)


def test_fully_matched_invoice(session):
    po = genesis_po(session)
    deliver(session, GENESIS, [("Coast calcium white", 15), ("Bucket", 10), ("Plastic drum", 2)], po=po)
    inv = invoice(session, GENESIS, "INV-101",
                  [("Coast calcium white", 15), ("Bucket", 10), ("Plastic drum", 2)], po=po)
    assert matching.invoice_is_clear_for_payment(matching.match_invoice(session, inv))


def test_partial_deliveries_add_up(session):
    po = genesis_po(session)
    deliver(session, GENESIS, [("Coast calcium white", 10)], po=po)
    deliver(session, GENESIS, [("Coast calcium white", 5)], po=po)
    inv = invoice(session, GENESIS, "INV-102", [("Coast calcium white", 15)], po=po)
    assert by_name(session, matching.match_invoice(session, inv))["Coast calcium white"].status == matching.MATCHED


def test_short_delivery(session):
    po = genesis_po(session)
    deliver(session, GENESIS, [("Bucket", 8)], po=po)
    inv = invoice(session, GENESIS, "INV-103", [("Bucket", 10)], po=po)
    assert by_name(session, matching.match_invoice(session, inv))["Bucket"].status == matching.SHORT_DELIVERED


def test_over_invoiced_against_po(session):
    po = genesis_po(session)
    deliver(session, GENESIS, [("Bucket", 10)], po=po)
    inv = invoice(session, GENESIS, "INV-104", [("Bucket", 12)], po=po)
    assert by_name(session, matching.match_invoice(session, inv))["Bucket"].status == matching.OVER_INVOICED


def test_item_not_on_po(session):
    po = genesis_po(session)
    inv = invoice(session, GENESIS, "INV-105", [("Coast calcium", 5)], po=po)
    assert by_name(session, matching.match_invoice(session, inv))["Coast calcium"].status == matching.NOT_ORDERED


def test_duplicate_invoice_number_blocked_by_database(session):
    deliver(session, GENESIS, [("Bucket", 10)])
    invoice(session, GENESIS, "INV-106", [("Bucket", 10)])
    session.add(Invoice(supplier_id=sup(session, GENESIS).id, invoice_number="INV-106"))
    with pytest.raises(Exception):
        session.commit()


# --------------------------------------- real documents, no PO number on them
def test_garden_vineyard_nyumba_cement_real_documents(session):
    """Handwritten delivery note and invoice, 220 bags at 785 = 172,700. No LPO."""
    set_price(session, GARDEN, "Nyumba cement", 785)
    deliver(session, GARDEN, [("Nyumba cement", 220)])
    inv = invoice(session, GARDEN, "151", [("Nyumba cement", 220, 785)], when=date(2026, 10, 20))
    results = matching.match_invoice(session, inv)
    assert results[0].status == matching.MATCHED
    summary = matching.invoice_summary(inv, results)
    assert summary == {"total": D("172700"), "blocked": D("0"), "payable": D("172700")}


def test_garden_vineyard_wrong_price_is_flagged(session):
    set_price(session, GARDEN, "Nyumba cement", 785)
    deliver(session, GARDEN, [("Nyumba cement", 220)])
    inv = invoice(session, GARDEN, "152", [("Nyumba cement", 220, 800)])
    r = matching.match_invoice(session, inv)[0]
    assert r.status == matching.PRICE_MISMATCH
    assert r.amount_blocked == D("3300")        # (800 - 785) x 220


def test_price_not_set_blocks_payment(session):
    deliver(session, GARDEN, [("Nyumba cement", 220)])
    inv = invoice(session, GARDEN, "153", [("Nyumba cement", 220, 785)])
    results = matching.match_invoice(session, inv)
    assert results[0].status == matching.PRICE_NOT_SET
    assert not matching.invoice_is_clear_for_payment(results)


def test_hekiwa_invoice_with_returned_hoop_iron_real_documents(session):
    """Real case: delivery note says 'X RETURNED' beside hoop iron (8), yet the
    supplier invoiced all 8 at 1,400. Total 271,790; payable should be 260,590."""
    load_prices.PRICES  # real prices come from the documents themselves
    for supplier, material, price, when in load_prices.PRICES:
        if supplier == HEKIWA:
            set_price(session, supplier, material, price, when)
    lines = [
        ('Orient Craft cutting disk 9"', 7, 130), ("Binding wire 15 kg small coil", 3, 2300),
        ("Blade", 5, 110), ("Deformed bar D12 mm", 32, 1080), ("Deformed bar D16 mm", 95, 2090),
        ("Deformed bar D8 mm", 24, 505), ("Hoop iron 17 kg", 8, 1400), ('Nails 4" x 50 kg', 1, 7000),
    ]
    # delivered 20 Oct, invoice dated 19 Oct; hoop iron came but was sent back
    deliver(session, HEKIWA, [(m, q, q if m == "Hoop iron 17 kg" else 0) for m, q, _ in lines])
    inv = invoice(session, HEKIWA, "81231", lines)

    results = matching.match_invoice(session, inv)
    r = by_name(session, results)
    for name in r:
        if name != "Hoop iron 17 kg":
            assert r[name].status == matching.MATCHED, name
    hoop = r["Hoop iron 17 kg"]
    assert hoop.status == matching.RETURNED_BILLED
    assert hoop.invoiced_qty == 8 and hoop.delivered_qty == 0 and hoop.returned_qty == 8
    assert hoop.amount_blocked == D("11200")
    assert not matching.invoice_is_clear_for_payment(results)

    summary = matching.invoice_summary(inv, results)
    assert summary == {"total": D("271790"), "blocked": D("11200"), "payable": D("260590")}


def test_partial_delivery_then_second_delivery(session):
    """Hekiwa brought only 32 of 57 D12 bars first; the rest comes later."""
    set_price(session, HEKIWA, "Deformed bar D12 mm", 1080)
    deliver(session, HEKIWA, [("Deformed bar D12 mm", 32)], when=date(2026, 10, 20))
    first = invoice(session, HEKIWA, "A1", [("Deformed bar D12 mm", 32, 1080)])
    assert matching.match_invoice(session, first)[0].status == matching.MATCHED
    deliver(session, HEKIWA, [("Deformed bar D12 mm", 25)], when=date(2026, 10, 27))
    second = invoice(session, HEKIWA, "A2", [("Deformed bar D12 mm", 25, 1080)])
    assert matching.match_invoice(session, second)[0].status == matching.MATCHED


def test_double_billing_same_delivery_is_caught(session):
    set_price(session, GARDEN, "Nyumba cement", 785)
    deliver(session, GARDEN, [("Nyumba cement", 220)])
    one = invoice(session, GARDEN, "151", [("Nyumba cement", 220, 785)])
    assert matching.match_invoice(session, one)[0].status == matching.MATCHED
    two = invoice(session, GARDEN, "152", [("Nyumba cement", 220, 785)])
    r = matching.match_invoice(session, two)[0]
    assert r.status == matching.ALREADY_BILLED
    assert r.amount_blocked == D("172700")


def test_matching_the_same_invoice_twice_does_not_double_count(session):
    set_price(session, GARDEN, "Nyumba cement", 785)
    deliver(session, GARDEN, [("Nyumba cement", 220)])
    inv = invoice(session, GARDEN, "151", [("Nyumba cement", 220, 785)])
    matching.match_invoice(session, inv)
    assert matching.match_invoice(session, inv)[0].status == matching.MATCHED
    total = session.scalar(select(func.sum(DeliveryAllocation.quantity)))
    assert D(str(total)) == D("220")


def test_under_invoiced_needs_review(session):
    set_price(session, GARDEN, "Nyumba cement", 785)
    deliver(session, GARDEN, [("Nyumba cement", 220)])
    inv = invoice(session, GARDEN, "154", [("Nyumba cement", 200, 785)])
    results = matching.match_invoice(session, inv)
    assert results[0].status == matching.UNDER_INVOICED
    assert not matching.invoice_is_clear_for_payment(results)


def test_wording_check_all_loaded_prices_exist_in_seed(session):
    """Every price in load_prices.py must point at a real supplier and material."""
    for supplier, material, _price, _when in load_prices.PRICES:
        assert sup(session, supplier) is not None, supplier
        assert mat(session, material) is not None, material