"""Invoice vs delivery matching. Pure functions: no database, no Flask.

Rule: an invoice line is payable only up to what was actually delivered and kept
(delivered minus returned), less what earlier invoices already billed, and at the
agreed price. Anything else is held back and flagged.
"""
from decimal import Decimal, ROUND_HALF_UP

TOL = Decimal("0.01")
ZERO = Decimal(0)

FLAG_TEXT = {
    "not_delivered": "Nothing delivered (or all returned)",
    "over_billed": "Billed more than delivered",
    "price_differs": "Price differs from agreed price",
    "price_unset": "No agreed price set",
}
BLOCKING = {"not_delivered", "over_billed", "price_differs"}


def D(value):
    if value is None or value == "":
        return ZERO
    return Decimal(str(value))


def money(value):
    return D(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def net_delivered(delivered, returned):
    """Quantity kept from one delivery line."""
    return max(D(delivered) - D(returned), ZERO)


def check_invoice(lines, available, expected, stated_total=None):
    """lines: [{"material_id", "name", "qty", "price"}]
    available: {material_id: quantity delivered and not yet billed}
    expected: {material_id: agreed price or None}
    """
    remaining = {k: D(v) for k, v in available.items()}
    results = []
    invoiced = payable = ZERO
    for ln in lines:
        mid = ln["material_id"]
        qty, price = D(ln["qty"]), D(ln["price"])
        avail = max(remaining.get(mid, ZERO), ZERO)
        pay_qty = min(qty, avail)
        remaining[mid] = avail - pay_qty

        flags = []
        if avail <= 0:
            flags.append("not_delivered")
        elif qty > avail:
            flags.append("over_billed")
        agreed = expected.get(mid)
        if agreed is None:
            flags.append("price_unset")
        elif abs(price - D(agreed)) > TOL:
            flags.append("price_differs")

        pay_price = D(agreed) if agreed is not None else price
        line_total = money(qty * price)
        pay_amount = money(pay_qty * pay_price)
        invoiced += line_total
        payable += pay_amount
        results.append({
            "material_id": mid, "name": ln.get("name", ""),
            "qty": qty, "price": price, "agreed_price": agreed,
            "line_total": line_total, "available": avail,
            "pay_qty": pay_qty, "pay_amount": pay_amount,
            "hold_amount": line_total - pay_amount,
            "flags": flags,
            "status": "ok" if not flags else flags[0],
            "notes": [FLAG_TEXT[f] for f in flags],
        })

    total_ok = True
    if stated_total not in (None, ""):
        total_ok = abs(money(stated_total) - invoiced) <= TOL

    any_flags = {f for r in results for f in r["flags"]}
    if any_flags & BLOCKING or not total_ok:
        verdict = "mismatch"
    elif "price_unset" in any_flags:
        verdict = "review"
    else:
        verdict = "match"
    return {
        "lines": results, "invoiced_total": money(invoiced),
        "payable_total": money(payable), "hold_total": money(invoiced - payable),
        "total_ok": total_ok, "stated_total": stated_total, "verdict": verdict,
    }