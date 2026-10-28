"""Matching engine: plain, explainable rules (no AI here).

For one invoice, compare per material:
    ordered (PO)  vs  delivered (delivery notes, good condition)  vs  invoiced.
Only fully MATCHED invoices may go to payment approval.
"""
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

import config
from models import (
    Invoice, PurchaseOrder, POLine, DeliveryNote, DeliveryLine,
    MatchResult, Material,
)

MATCHED = "MATCHED"
NOT_ON_DELIVERY_NOTE = "NOT_ON_DELIVERY_NOTE"   # invoiced, nothing delivered
SHORT_DELIVERED = "SHORT_DELIVERED"             # delivered < invoiced
OVER_INVOICED = "OVER_INVOICED"                 # invoiced > ordered
OVER_DELIVERED = "OVER_DELIVERED"               # delivered > ordered
NOT_ORDERED = "NOT_ORDERED"                     # invoiced, never on the PO
PRICE_MISMATCH = "PRICE_MISMATCH"
DUPLICATE = "DUPLICATE"

D = Decimal


def _equal(a: Decimal, b: Decimal, tol: float) -> bool:
    if tol == 0:
        return a == b
    return abs(a - b) <= max(a, b) * D(str(tol))


def match_invoice(session: Session, invoice: Invoice) -> list[MatchResult]:
    """Match every invoice line, save the results, and return them."""
    results: list[MatchResult] = []

    # Duplicate check: same supplier + invoice number already stored.
    dupes = session.scalars(
        select(Invoice).where(
            Invoice.supplier_id == invoice.supplier_id,
            Invoice.invoice_number == invoice.invoice_number,
            Invoice.id != invoice.id,
        )
    ).all()
    if dupes:
        r = MatchResult(
            invoice_id=invoice.id, material_id=invoice.lines[0].material_id,
            status=DUPLICATE,
            reason=f"Invoice number {invoice.invoice_number} already exists for this supplier.",
        )
        session.add(r)
        session.commit()
        return [r]

    po = session.get(PurchaseOrder, invoice.po_id) if invoice.po_id else None

    for line in invoice.lines:
        name = session.get(Material, line.material_id).name

        po_line = None
        ordered = D(0)
        if po:
            po_line = session.scalar(
                select(POLine).where(POLine.po_id == po.id, POLine.material_id == line.material_id)
            )
            if po_line:
                ordered = D(po_line.quantity)

        delivered = D(0)
        if po:
            rows = session.scalars(
                select(DeliveryLine)
                .join(DeliveryNote, DeliveryLine.note_id == DeliveryNote.id)
                .where(
                    DeliveryNote.po_id == po.id,
                    DeliveryLine.material_id == line.material_id,
                    DeliveryLine.condition == "good",
                )
            ).all()
            delivered = sum((D(r.quantity) for r in rows), D(0))

        invoiced = D(line.quantity)
        status, reason = MATCHED, "Ordered, delivered and invoiced quantities agree."

        if po_line is None:
            status = NOT_ORDERED
            reason = f"{name}: invoiced {invoiced} but it is not on the purchase order."
        elif delivered == 0:
            status = NOT_ON_DELIVERY_NOTE
            reason = (f"{name}: invoiced {invoiced}, delivered 0. "
                      "Not on any delivery note - block payment for this line.")
        elif invoiced > ordered and not _equal(invoiced, ordered, config.QUANTITY_TOLERANCE):
            status = OVER_INVOICED
            reason = f"{name}: invoiced {invoiced} but only {ordered} ordered."
        elif delivered > ordered and not _equal(delivered, ordered, config.QUANTITY_TOLERANCE):
            status = OVER_DELIVERED
            reason = f"{name}: delivered {delivered} but only {ordered} ordered. Needs approval."
        elif not _equal(invoiced, delivered, config.QUANTITY_TOLERANCE):
            status = SHORT_DELIVERED if delivered < invoiced else OVER_DELIVERED
            reason = f"{name}: invoiced {invoiced}, delivered {delivered}."
        elif (line.unit_price is not None and po_line.agreed_price is not None
              and not _equal(D(line.unit_price), D(po_line.agreed_price), config.PRICE_TOLERANCE)):
            status = PRICE_MISMATCH
            reason = (f"{name}: invoice price {line.unit_price} "
                      f"differs from agreed price {po_line.agreed_price}.")

        r = MatchResult(
            invoice_id=invoice.id, material_id=line.material_id,
            ordered_qty=ordered, delivered_qty=delivered, invoiced_qty=invoiced,
            status=status, reason=reason,
        )
        session.add(r)
        results.append(r)

    session.commit()
    return results


def invoice_is_clear_for_payment(results: list[MatchResult]) -> bool:
    return bool(results) and all(r.status == MATCHED for r in results)