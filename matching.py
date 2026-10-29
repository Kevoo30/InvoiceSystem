"""Matching engine: plain, explainable rules (no AI here).

An invoice is checked against what the SUPPLIER actually delivered and has not
already been billed. A purchase order is optional, because many delivery notes
and invoices carry no LPO number. When a PO is given it adds two more checks
(item was ordered, quantity not above what was ordered).

For every material on the invoice the engine compares:
    invoiced quantity  vs  delivered quantity still unbilled (returns removed)
and then the unit price against the agreed/list price.

Only invoices where every line is MATCHED may go to payment approval.
`amount_blocked` on each result is the money that must NOT be paid.
"""
from decimal import Decimal

from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session

import config
from models import (
    DeliveryAllocation, DeliveryLine, DeliveryNote, Invoice, Material,
    MatchResult, POLine, SupplierPrice,
)

MATCHED = "MATCHED"
NOT_ON_DELIVERY_NOTE = "NOT_ON_DELIVERY_NOTE"   # invoiced, nothing delivered
RETURNED_BILLED = "RETURNED_BILLED"             # invoiced goods that were sent back
ALREADY_BILLED = "ALREADY_BILLED"               # delivery already billed on another invoice
SHORT_DELIVERED = "SHORT_DELIVERED"             # delivered < invoiced
UNDER_INVOICED = "UNDER_INVOICED"               # delivered > invoiced (needs review)
OVER_INVOICED = "OVER_INVOICED"                 # more billed than ordered (PO)
OVER_DELIVERED = "OVER_DELIVERED"               # more delivered than ordered (PO)
NOT_ORDERED = "NOT_ORDERED"                     # invoiced, never on the PO
PRICE_MISMATCH = "PRICE_MISMATCH"
PRICE_NOT_SET = "PRICE_NOT_SET"                 # no agreed/list price to check against
DUPLICATE = "DUPLICATE"

D = Decimal
ZERO = D(0)


# ------------------------------------------------------------------ helpers
def _d(value) -> Decimal:
    return D(str(value)) if value is not None else ZERO


def _n(x: Decimal) -> str:
    """Quantity without trailing zeros: 220.00 -> 220."""
    return format(x.normalize(), "f")


def _equal(a: Decimal, b: Decimal, tol: float) -> bool:
    if tol == 0:
        return a == b
    return abs(a - b) <= max(a, b) * D(str(tol))


def _usable(line: DeliveryLine) -> Decimal:
    """Quantity on a delivery line that was accepted (not returned/rejected)."""
    if line.condition != "good":
        return ZERO
    return max(_d(line.quantity) - _d(line.returned_qty), ZERO)


def _returned(line: DeliveryLine) -> Decimal:
    """Quantity on a delivery line that was sent back or rejected."""
    if line.condition != "good":
        return _d(line.quantity)
    return _d(line.returned_qty)


def _allocated_elsewhere(session: Session, line_id: int, invoice_id: int) -> Decimal:
    total = session.scalar(
        select(func.coalesce(func.sum(DeliveryAllocation.quantity), 0))
        .where(DeliveryAllocation.delivery_line_id == line_id,
               DeliveryAllocation.invoice_id != invoice_id))
    return _d(total)


def expected_price(session: Session, invoice: Invoice, material_id: int, po_line):
    """(price, source) the invoice should be charging, or (None, None)."""
    if po_line is not None and po_line.agreed_price is not None:
        return _d(po_line.agreed_price), "the agreed price on the order"
    as_of = invoice.invoice_date
    q = (select(SupplierPrice)
         .where(SupplierPrice.supplier_id == invoice.supplier_id,
                SupplierPrice.material_id == material_id,
                SupplierPrice.unit_price.is_not(None))
         .order_by(SupplierPrice.effective_from.desc(), SupplierPrice.id.desc())
         .limit(1))
    if as_of is not None:
        q = q.where(SupplierPrice.effective_from <= as_of)
    row = session.scalar(q)
    if row is not None:
        return _d(row.unit_price), "the price list"
    return None, None


# ------------------------------------------------------------ one material
def _check_material(session: Session, invoice: Invoice, material_id: int,
                    invoiced: Decimal, price):
    name = session.get(Material, material_id).name
    qtol = config.QUANTITY_TOLERANCE

    po_line = None
    ordered = None
    if invoice.po_id:
        po_line = session.scalar(select(POLine).where(
            POLine.po_id == invoice.po_id, POLine.material_id == material_id))
        if po_line is not None:
            ordered = _d(po_line.quantity)

    q = (select(DeliveryLine)
         .join(DeliveryNote, DeliveryLine.note_id == DeliveryNote.id)
         .where(DeliveryNote.supplier_id == invoice.supplier_id,
                DeliveryLine.material_id == material_id)
         .order_by(DeliveryNote.delivery_date, DeliveryLine.id))
    if invoice.po_id:
        q = q.where(or_(DeliveryNote.po_id == invoice.po_id, DeliveryNote.po_id.is_(None)))
    lines = session.scalars(q).all()

    remaining = returned = usable_total = billed_elsewhere = ZERO
    free_lines = []
    for ln in lines:
        usable = _usable(ln)
        taken = _allocated_elsewhere(session, ln.id, invoice.id)
        free = max(usable - taken, ZERO)
        usable_total += usable
        billed_elsewhere += min(taken, usable)
        remaining += free
        if taken == 0:
            returned += _returned(ln)
        if free > 0:
            free_lines.append((ln, free))

    expected, source = expected_price(session, invoice, material_id, po_line)
    unit = price if price is not None else (expected or ZERO)
    eq = _equal(invoiced, remaining, qtol)

    status, reason, blocked, allocate = MATCHED, "", ZERO, False

    if invoice.po_id and po_line is None:
        status = NOT_ORDERED
        reason = f"{name}: invoiced {_n(invoiced)} but it is not on the purchase order."
        blocked = invoiced * unit
    elif not lines:
        status = NOT_ON_DELIVERY_NOTE
        reason = (f"{name}: invoiced {_n(invoiced)}, delivered 0. It is not on any "
                  f"delivery note - do not pay for this line.")
        blocked = invoiced * unit
    elif (ordered is not None and billed_elsewhere + invoiced > ordered
          and not _equal(billed_elsewhere + invoiced, ordered, qtol)):
        excess = billed_elsewhere + invoiced - ordered
        status = OVER_INVOICED
        reason = (f"{name}: billed {_n(billed_elsewhere + invoiced)} in total "
                  f"but only {_n(ordered)} ordered.")
        blocked = min(invoiced, excess) * unit
    elif returned > 0 and invoiced > remaining and not eq and invoiced <= remaining + returned:
        status = RETURNED_BILLED
        back = invoiced - remaining
        reason = (f"{name}: invoiced {_n(invoiced)}, but {_n(returned)} was returned or "
                  f"rejected, so only {_n(remaining)} was kept. Ask the supplier for a "
                  f"credit note for {_n(back)}.")
        blocked = back * unit
    elif remaining == 0 and billed_elsewhere > 0:
        status = ALREADY_BILLED
        reason = (f"{name}: all {_n(billed_elsewhere)} delivered were already billed on "
                  f"another invoice. Possible double billing.")
        blocked = invoiced * unit
    elif invoiced > remaining and not eq:
        status = SHORT_DELIVERED
        reason = (f"{name}: invoiced {_n(invoiced)} but only {_n(remaining)} "
                  f"delivered and not yet billed.")
        blocked = (invoiced - remaining) * unit
    elif invoiced < remaining and not eq:
        status = UNDER_INVOICED
        reason = (f"{name}: invoiced {_n(invoiced)} but {_n(remaining)} delivered and "
                  f"not yet billed. Check whether a later invoice is expected.")
    else:
        allocate = True
        if ordered is not None and usable_total > ordered and not _equal(usable_total, ordered, qtol):
            status = OVER_DELIVERED
            reason = (f"{name}: delivered {_n(usable_total)} but only {_n(ordered)} "
                      f"ordered. Needs approval.")
        elif price is not None and expected is None:
            status = PRICE_NOT_SET
            reason = (f"{name}: no price is set for this supplier, so the invoice price "
                      f"{price:,.2f} cannot be checked. Set the price first.")
        elif price is not None and not _equal(price, expected, config.PRICE_TOLERANCE):
            status = PRICE_MISMATCH
            reason = (f"{name}: invoice price {price:,.2f} differs from {source} "
                      f"({expected:,.2f}).")
            blocked = max(price - expected, ZERO) * invoiced
        else:
            reason = "Ordered, delivered and invoiced quantities agree."
            if price is not None:
                reason = "Quantity and price agree with the delivery and price list."

    if allocate:
        left = min(invoiced, remaining)
        for ln, free in free_lines:
            if left <= 0:
                break
            take = min(free, left)
            session.add(DeliveryAllocation(delivery_line_id=ln.id,
                                           invoice_id=invoice.id, quantity=take))
            left -= take

    return MatchResult(
        invoice_id=invoice.id, material_id=material_id, ordered_qty=ordered,
        delivered_qty=remaining, returned_qty=returned, invoiced_qty=invoiced,
        unit_price=price, amount_blocked=blocked, status=status, reason=reason)


# ------------------------------------------------------------- whole invoice
def match_invoice(session: Session, invoice: Invoice) -> list[MatchResult]:
    """Match every material on the invoice, save the results and return them.

    Safe to run again on the same invoice: its earlier results and billing
    allocations are replaced, never doubled.
    """
    session.execute(delete(DeliveryAllocation).where(DeliveryAllocation.invoice_id == invoice.id))
    session.execute(delete(MatchResult).where(MatchResult.invoice_id == invoice.id))
    session.flush()

    if not invoice.lines:
        session.commit()
        return []

    dupes = session.scalars(select(Invoice).where(
        Invoice.supplier_id == invoice.supplier_id,
        Invoice.invoice_number == invoice.invoice_number,
        Invoice.id != invoice.id)).all()
    if dupes:
        total = sum((_d(l.quantity) * _d(l.unit_price) for l in invoice.lines
                     if l.unit_price is not None), ZERO)
        r = MatchResult(
            invoice_id=invoice.id, material_id=invoice.lines[0].material_id,
            status=DUPLICATE, amount_blocked=total,
            reason=f"Invoice number {invoice.invoice_number} already exists for this supplier.")
        session.add(r)
        session.commit()
        return [r]

    grouped: dict[int, dict] = {}
    for line in invoice.lines:
        g = grouped.setdefault(line.material_id, {"qty": ZERO, "price": None})
        g["qty"] += _d(line.quantity)
        if g["price"] is None and line.unit_price is not None:
            g["price"] = _d(line.unit_price)

    results = []
    for material_id, g in grouped.items():
        result = _check_material(session, invoice, material_id, g["qty"], g["price"])
        session.add(result)
        results.append(result)
    session.commit()
    return results


def invoice_is_clear_for_payment(results: list[MatchResult]) -> bool:
    return bool(results) and all(r.status == MATCHED for r in results)


def invoice_summary(invoice: Invoice, results: list[MatchResult]) -> dict:
    """Invoice total, money that must not be paid, and what is safe to pay."""
    total = sum((_d(l.quantity) * _d(l.unit_price) for l in invoice.lines
                 if l.unit_price is not None), ZERO)
    blocked = sum((_d(r.amount_blocked) for r in results), ZERO)
    return {"total": total, "blocked": blocked, "payable": total - blocked}