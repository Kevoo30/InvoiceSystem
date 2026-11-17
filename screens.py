"""Part 2 of the website: record deliveries, enter invoices, see the match result.

Who can do what
  storekeeper : record deliveries; view deliveries
  procurement : raise orders; view orders, deliveries and invoices
  finance     : enter and re-check invoices, approve, mark paid; view orders and deliveries
  admin       : everything

Nobody except admin can order, receive and bill the same goods.
Labour and service invoices are in labour.py.
"""
import os
import secrets
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from flask import (
    abort, flash, g, jsonify, redirect, render_template, request, send_from_directory, url_for,
)
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from werkzeug.utils import secure_filename

import ai_reader
import matching
from labour import LabourInvoice, register_labour
from models import (
    DeliveryLine, DeliveryNote, Invoice, InvoiceLine, Material, MatchResult,
    POLine, PurchaseOrder, Supplier, SupplierPrice, User,
)

ZERO = Decimal(0)

# JPG, PNG or PDF only, and the file must really start like one.
ALLOWED_UPLOADS = {".jpg": b"\xff\xd8\xff", ".jpeg": b"\xff\xd8\xff",
                   ".png": b"\x89PNG", ".pdf": b"%PDF"}

STATUS_LABEL = {
    matching.MATCHED: "Matched",
    matching.NOT_ON_DELIVERY_NOTE: "Not on delivery note",
    matching.RETURNED_BILLED: "Returned goods billed",
    matching.ALREADY_BILLED: "Already billed",
    matching.SHORT_DELIVERED: "Short delivered",
    matching.UNDER_INVOICED: "Under-invoiced",
    matching.OVER_INVOICED: "Over-invoiced",
    matching.OVER_DELIVERED: "Over-delivered",
    matching.NOT_ORDERED: "Not ordered",
    matching.PRICE_MISMATCH: "Price mismatch",
    matching.PRICE_NOT_SET: "Price not set",
    matching.DUPLICATE: "Duplicate invoice",
}
STATUS_CLASS = {matching.MATCHED: "ok", matching.UNDER_INVOICED: "warn",
                matching.OVER_DELIVERED: "warn", matching.PRICE_NOT_SET: "warn"}


# ------------------------------------------------------------------ parsing
def parse_qty(text):
    """Blank -> None. Otherwise a number >= 0 with 2 decimals, or ValueError."""
    text = (text or "").strip().replace(",", "")
    if not text:
        return None
    try:
        value = Decimal(text)
    except InvalidOperation:
        raise ValueError
    if not value.is_finite() or value < 0 or value > Decimal("1000000"):
        raise ValueError
    return value.quantize(Decimal("0.01"))


def parse_money(text):
    value = parse_qty(text)
    if value is not None and value <= 0:
        raise ValueError
    return value


def parse_past_date(text):
    """Empty -> today. Returns None if invalid or in the future."""
    if not text:
        return date.today()
    try:
        value = datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None
    return value if value <= date.today() else None


def check_upload(file_storage):
    """Error text, or None when there is no file or the file looks fine."""
    if not file_storage or not file_storage.filename:
        return None
    ext = os.path.splitext(file_storage.filename)[1].lower()
    if ext not in ALLOWED_UPLOADS:
        return "The photo must be a JPG, PNG or PDF file."
    head = file_storage.stream.read(8)
    file_storage.stream.seek(0)
    if not head.startswith(ALLOWED_UPLOADS[ext]):
        return "That file does not look like a real JPG, PNG or PDF."
    return None


def store_upload(file_storage, folder):
    if not file_storage or not file_storage.filename:
        return None
    ext = os.path.splitext(file_storage.filename)[1].lower()
    name = f"{secrets.token_hex(12)}{ext}"
    os.makedirs(folder, exist_ok=True)
    file_storage.save(os.path.join(folder, name))
    return name


# ------------------------------------------------------------------- screens
def register_screens(app, access, log, current_price_row):
    app.config.setdefault("UPLOAD_FOLDER", os.path.join(app.root_path, "uploads"))
    app.config.setdefault("MAX_CONTENT_LENGTH", 10 * 1024 * 1024)

    # ----- template helpers
    app.jinja_env.filters["status_label"] = lambda s: STATUS_LABEL.get(s, s)
    app.jinja_env.filters["status_class"] = lambda s: STATUS_CLASS.get(s, "err")
    app.jinja_env.filters["money"] = lambda v: "-" if v is None else f"{Decimal(str(v)):,.2f}"
    app.jinja_env.filters["qty"] = lambda v: "" if v is None else matching._n(Decimal(str(v)))

    def held_invoices() -> int:
        """Unpaid invoices that have at least one problem line."""
        if g.get("user") is None:
            return 0
        if not hasattr(g, "_held"):
            g._held = g.db.scalar(
                select(func.count(func.distinct(MatchResult.invoice_id)))
                .join(Invoice, Invoice.id == MatchResult.invoice_id)
                .where(Invoice.payment_status == "unpaid",
                       MatchResult.status != matching.MATCHED)) or 0
        return g._held

    app.jinja_env.globals["held_invoices"] = held_invoices

    @app.errorhandler(413)
    def too_big(_):
        return render_template("error.html", code=413,
                               message="That file is too large. The limit is 10 MB."), 413

    # ----- shared queries
    def supplier_materials(supplier_id):
        ids = {p.material_id for p in g.db.scalars(
            select(SupplierPrice).where(SupplierPrice.supplier_id == supplier_id))}
        if not ids:
            return []
        return g.db.scalars(select(Material).where(Material.id.in_(ids))
                            .order_by(Material.name)).all()

    def active_suppliers():
        return g.db.scalars(select(Supplier).where(Supplier.active.is_(True))
                            .order_by(Supplier.name)).all()

    def open_orders(supplier_id):
        return g.db.scalars(select(PurchaseOrder).where(
            PurchaseOrder.supplier_id == supplier_id, PurchaseOrder.status == "open")
            .order_by(PurchaseOrder.id.desc())).all()

    def chosen_order(supplier):
        """Optional order picked on a form. Must belong to the same supplier."""
        po_id = request.form.get("po_id", type=int)
        if not po_id:
            return None
        po = g.db.get(PurchaseOrder, po_id)
        return po if po is not None and po.supplier_id == supplier.id else None

    def invoice_results(invoice_id):
        return g.db.scalars(select(MatchResult).where(MatchResult.invoice_id == invoice_id)
                            .order_by(MatchResult.id)).all()

    def invoice_state(invoice, results):
        if invoice.payment_status == "paid":
            return "Paid", "ok"
        if invoice.payment_status == "approved":
            return "Approved for payment", "ok"
        if not results:
            return "Not checked", "warn"
        if matching.invoice_is_clear_for_payment(results):
            return "Clear for payment", "ok"
        return "On hold", "err"

    # ------------------------------------------------------------ uploaded files
    @app.route("/files/<path:name>")
    @access()
    def uploaded_file(name):
        return send_from_directory(app.config["UPLOAD_FOLDER"], secure_filename(name))

    # ---------------------------------------------------------------- deliveries
    @app.route("/deliveries")
    @access("storekeeper", "procurement", "finance")
    def deliveries():
        supplier_id = request.args.get("supplier", type=int)
        q = (select(DeliveryNote, Supplier.name, User.name)
             .join(Supplier, Supplier.id == DeliveryNote.supplier_id)
             .outerjoin(User, User.id == DeliveryNote.received_by)
             .order_by(DeliveryNote.delivery_date.desc(), DeliveryNote.id.desc())
             .limit(200))
        if supplier_id:
            q = q.where(DeliveryNote.supplier_id == supplier_id)
        rows = []
        for note, supplier_name, user_name in g.db.execute(q).all():
            returned = any(Decimal(str(l.returned_qty or 0)) > 0 for l in note.lines)
            rows.append({"note": note, "supplier": supplier_name, "by": user_name,
                         "items": len(note.lines), "returned": returned})
        return render_template("deliveries.html", rows=rows, suppliers=active_suppliers(),
                               selected=supplier_id)

    @app.route("/deliveries/new", methods=["GET", "POST"])
    @access("storekeeper")
    def delivery_new():
        suppliers = active_suppliers()
        today = date.today().isoformat()

        if request.method == "GET":
            supplier = g.db.get(Supplier, request.args.get("supplier", type=int) or 0)
            materials = supplier_materials(supplier.id) if supplier else []
            return render_template("delivery_new.html", suppliers=suppliers, supplier=supplier,
                                   materials=materials, values={}, today=today,
                                   orders=open_orders(supplier.id) if supplier else [])

        supplier = g.db.get(Supplier, request.form.get("supplier_id", type=int) or 0)
        if supplier is None:
            flash("Choose the supplier first.", "error")
            return redirect(url_for("delivery_new"))
        materials = supplier_materials(supplier.id)

        errors, lines = [], []
        when = parse_past_date(request.form.get("delivery_date", ""))
        if when is None:
            errors.append("Enter a valid delivery date that is not in the future.")
        note_number = " ".join(request.form.get("note_number", "").split())[:50]

        for m in materials:
            try:
                qty = parse_qty(request.form.get(f"qty_{m.id}"))
                ret = parse_qty(request.form.get(f"ret_{m.id}")) or ZERO
            except ValueError:
                errors.append(f"{m.name}: enter numbers only.")
                continue
            if not qty:
                if ret:
                    errors.append(f"{m.name}: a returned quantity was entered but nothing arrived.")
                continue
            if ret > qty:
                errors.append(f"{m.name}: returned ({matching._n(ret)}) is more than "
                              f"arrived ({matching._n(qty)}).")
                continue
            lines.append((m, qty, ret))
        if not lines and not errors:
            photo_chosen = bool(request.files.get("photo") and request.files["photo"].filename)
            errors.append("The photo is attached but no items are entered. Press Read photo, "
                          "or type the quantities in." if photo_chosen else
                          "Enter the quantity that arrived for at least one item.")

        photo = request.files.get("photo")
        upload_error = check_upload(photo)
        if upload_error:
            errors.append(upload_error)

        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("delivery_new.html", suppliers=suppliers, supplier=supplier,
                                   materials=materials, values=request.form,
                                   today=request.form.get("delivery_date") or today,
                                   orders=open_orders(supplier.id))

        po = chosen_order(supplier)
        note = DeliveryNote(supplier_id=supplier.id, po_id=po.id if po else None,
                            note_number=note_number or None,
                            delivery_date=when, received_by=g.user.id,
                            photo_path=store_upload(photo, app.config["UPLOAD_FOLDER"]))
        g.db.add(note)
        g.db.flush()
        for m, qty, ret in lines:
            g.db.add(DeliveryLine(note_id=note.id, material_id=m.id, quantity=qty,
                                  returned_qty=ret, condition="good"))
        log("delivery_recorded", f"{supplier.name}: {len(lines)} item(s) on {when}")
        g.db.commit()
        flash("Delivery saved.", "ok")
        return redirect(url_for("delivery_detail", note_id=note.id))

    @app.route("/deliveries/<int:note_id>")
    @access("storekeeper", "procurement", "finance")
    def delivery_detail(note_id):
        note = g.db.get(DeliveryNote, note_id) or abort(404)
        supplier = g.db.get(Supplier, note.supplier_id)
        by = g.db.get(User, note.received_by) if note.received_by else None
        lines = sorted(note.lines, key=lambda l: l.material.name)
        return render_template("delivery_detail.html", note=note, supplier=supplier,
                               by=by, lines=lines)

    # ------------------------------------------------------------------ invoices
    @app.route("/invoices")
    @access("procurement", "finance")
    def invoices():
        wanted = request.args.get("show", "")
        rows = []
        q = (select(Invoice, Supplier.name).join(Supplier, Supplier.id == Invoice.supplier_id)
             .order_by(Invoice.invoice_date.desc(), Invoice.id.desc()).limit(200))
        for inv, supplier_name in g.db.execute(q).all():
            results = invoice_results(inv.id)
            state, cls = invoice_state(inv, results)
            if wanted == "hold" and state not in ("On hold", "Not checked"):
                continue
            if wanted == "clear" and state != "Clear for payment":
                continue
            if wanted == "done" and state not in ("Approved for payment", "Paid"):
                continue
            rows.append({"inv": inv, "supplier": supplier_name, "state": state, "cls": cls,
                         "summary": matching.invoice_summary(inv, results)})
        return render_template("invoices.html", rows=rows, wanted=wanted)

    @app.route("/invoices/new", methods=["GET", "POST"])
    @access("finance")
    def invoice_new():
        suppliers = active_suppliers()
        today = date.today().isoformat()

        def hints_for(supplier_id, materials):
            out = {}
            for m in materials:
                row = current_price_row(supplier_id, m.id)
                out[m.id] = row.unit_price if row is not None else None
            return out

        if request.method == "GET":
            supplier = g.db.get(Supplier, request.args.get("supplier", type=int) or 0)
            materials = supplier_materials(supplier.id) if supplier else []
            hints = hints_for(supplier.id, materials) if supplier else {}
            return render_template("invoice_new.html", suppliers=suppliers, supplier=supplier,
                                   materials=materials, hints=hints, values={}, today=today,
                                   orders=open_orders(supplier.id) if supplier else [])

        supplier = g.db.get(Supplier, request.form.get("supplier_id", type=int) or 0)
        if supplier is None:
            flash("Choose the supplier first.", "error")
            return redirect(url_for("invoice_new"))
        materials = supplier_materials(supplier.id)

        errors, lines = [], []
        number = " ".join(request.form.get("invoice_number", "").split())
        if not number or len(number) > 50:
            errors.append("Enter the invoice number (up to 50 characters).")
        when = parse_past_date(request.form.get("invoice_date", ""))
        if when is None:
            errors.append("Enter a valid invoice date that is not in the future.")
        if number and g.db.scalar(select(Invoice).where(Invoice.supplier_id == supplier.id,
                                                        Invoice.invoice_number == number)):
            errors.append(f"Invoice {number} from {supplier.name} has already been entered.")

        for m in materials:
            try:
                qty = parse_qty(request.form.get(f"qty_{m.id}"))
                price = parse_money(request.form.get(f"price_{m.id}"))
            except ValueError:
                errors.append(f"{m.name}: enter a valid quantity and a price above zero.")
                continue
            if not qty:
                if price is not None:
                    errors.append(f"{m.name}: a price was entered but no quantity.")
                continue
            if price is None:
                errors.append(f"{m.name}: enter the unit price shown on the invoice.")
                continue
            lines.append((m, qty, price))
        if not lines and not errors:
            photo_chosen = bool(request.files.get("photo") and request.files["photo"].filename)
            errors.append("The photo is attached but no items are entered. Press Read photo, "
                          "or type the quantities and prices in." if photo_chosen else
                          "Enter the quantity and price for at least one item.")

        photo = request.files.get("photo")
        upload_error = check_upload(photo)
        if upload_error:
            errors.append(upload_error)

        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("invoice_new.html", suppliers=suppliers, supplier=supplier,
                                   materials=materials, hints=hints_for(supplier.id, materials),
                                   values=request.form,
                                   today=request.form.get("invoice_date") or today,
                                   orders=open_orders(supplier.id))

        po = chosen_order(supplier)
        inv = Invoice(supplier_id=supplier.id, po_id=po.id if po else None,
                      invoice_number=number, invoice_date=when, payment_status="unpaid",
                      photo_path=store_upload(photo, app.config["UPLOAD_FOLDER"]))
        g.db.add(inv)
        try:
            g.db.flush()
        except IntegrityError:
            g.db.rollback()
            flash(f"Invoice {number} from {supplier.name} has already been entered.", "error")
            return redirect(url_for("invoice_new", supplier=supplier.id))
        for m, qty, price in lines:
            g.db.add(InvoiceLine(invoice_id=inv.id, material_id=m.id, quantity=qty,
                                 unit_price=price))
        log("invoice_entered", f"{supplier.name} #{number}: {len(lines)} item(s)")
        g.db.commit()
        matching.match_invoice(g.db, inv)
        return redirect(url_for("invoice_detail", invoice_id=inv.id))

    @app.route("/invoices/<int:invoice_id>")
    @access("procurement", "finance")
    def invoice_detail(invoice_id):
        inv = g.db.get(Invoice, invoice_id) or abort(404)
        supplier = g.db.get(Supplier, inv.supplier_id)
        results = invoice_results(inv.id)
        names = {m.id: m for m in g.db.scalars(select(Material).where(
            Material.id.in_([r.material_id for r in results] or [0])))}
        state, cls = invoice_state(inv, results)
        return render_template("invoice_detail.html", inv=inv, supplier=supplier,
                               results=results, names=names, state=state, cls=cls,
                               summary=matching.invoice_summary(inv, results),
                               clear=matching.invoice_is_clear_for_payment(results))

    @app.route("/invoices/<int:invoice_id>/recheck", methods=["POST"])
    @access("finance")
    def invoice_recheck(invoice_id):
        inv = g.db.get(Invoice, invoice_id) or abort(404)
        if inv.payment_status != "unpaid":
            flash("This invoice has already been approved, so it cannot be checked again.", "error")
        else:
            matching.match_invoice(g.db, inv)
            log("invoice_rechecked", f"#{inv.invoice_number}")
            g.db.commit()
            flash("Checked again against the latest deliveries and prices.", "ok")
        return redirect(url_for("invoice_detail", invoice_id=inv.id))

    @app.route("/invoices/<int:invoice_id>/approve", methods=["POST"])
    @access("finance")
    def invoice_approve(invoice_id):
        inv = g.db.get(Invoice, invoice_id) or abort(404)
        if inv.payment_status != "unpaid":
            flash("This invoice has already been approved.", "error")
        else:
            results = matching.match_invoice(g.db, inv)   # always re-check first
            if not matching.invoice_is_clear_for_payment(results):
                flash("This invoice still has differences, so it cannot be approved.", "error")
            else:
                inv.payment_status = "approved"
                log("invoice_approved", f"#{inv.invoice_number}")
                g.db.commit()
                flash("Invoice approved for payment.", "ok")
        return redirect(url_for("invoice_detail", invoice_id=inv.id))

    @app.route("/invoices/<int:invoice_id>/paid", methods=["POST"])
    @access("finance")
    def invoice_paid(invoice_id):
        inv = g.db.get(Invoice, invoice_id) or abort(404)
        if inv.payment_status != "approved":
            flash("Only an approved invoice can be marked as paid.", "error")
        else:
            inv.payment_status = "paid"
            log("invoice_paid", f"#{inv.invoice_number}")
            g.db.commit()
            flash("Invoice marked as paid.", "ok")
        return redirect(url_for("invoice_detail", invoice_id=inv.id))

    # ------------------------------------------------------------ photo reader
    MEDIA = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
             ".pdf": "application/pdf"}

    @app.route("/api/read-photo", methods=["POST"])
    @access("storekeeper", "finance")
    def read_photo():
        kind = request.form.get("kind", "")
        allowed = {"delivery": ("admin", "storekeeper"),
                   "invoice": ("admin", "finance")}
        if kind not in allowed or g.user.role not in allowed[kind]:
            abort(403)
        supplier = g.db.get(Supplier, request.form.get("supplier_id", type=int) or 0)
        if supplier is None:
            return jsonify(error="Choose the supplier first."), 400
        photo = request.files.get("photo")
        if not photo or not photo.filename:
            return jsonify(error="Choose a photo first."), 400
        problem = check_upload(photo)
        if problem:
            return jsonify(error=problem), 400
        ext = os.path.splitext(photo.filename)[1].lower()
        try:
            out = ai_reader.read_document(photo.read(), MEDIA[ext], kind,
                                          client=app.config.get("AI_CLIENT"))
        except ai_reader.ReaderError as e:
            return jsonify(error=str(e)), 422
        names = {m.id: m.name for m in supplier_materials(supplier.id)}
        mapped, unmatched = ai_reader.map_lines(g.db, supplier.id, out["lines"], names)
        log("photo_read", f"{kind} {supplier.name}: {len(mapped)} read, {len(unmatched)} unmatched")
        g.db.commit()
        return jsonify(number=out["number"], date=out["date"], unmatched=unmatched,
                       lines=[{"material_id": mid, **v} for mid, v in mapped.items()])

    # ---------------------------------------------------------------- dashboard
    def dash(q="", supplier_id=None, show=""):
        """Rows for the home page tables. Filters: search text, supplier, invoice status."""
        role = g.user.role
        q = (q or "").strip()[:60]
        out = {"suppliers": active_suppliers(), "invoices": [], "notes": [], "orders": []}

        def search(*columns):
            return or_(*[func.lower(c).contains(q.lower(), autoescape=True) for c in columns])

        # latest delivery notes (every role)
        nq = (select(DeliveryNote, Supplier.name)
              .join(Supplier, Supplier.id == DeliveryNote.supplier_id)
              .order_by(DeliveryNote.delivery_date.desc(), DeliveryNote.id.desc()).limit(10))
        if supplier_id:
            nq = nq.where(DeliveryNote.supplier_id == supplier_id)
        if q:
            nq = nq.where(search(Supplier.name, DeliveryNote.note_number))
        for note, supplier_name in g.db.execute(nq).all():
            out["notes"].append({
                "id": note.id, "date": note.delivery_date, "number": note.note_number,
                "supplier": supplier_name, "lines": len(note.lines),
                "returned": any(Decimal(str(l.returned_qty or 0)) > 0 for l in note.lines)})

        if role not in ("admin", "procurement", "finance"):
            return out

        # latest invoices with their check result
        iq = (select(Invoice, Supplier.name)
              .join(Supplier, Supplier.id == Invoice.supplier_id)
              .order_by(Invoice.invoice_date.desc(), Invoice.id.desc()).limit(200))
        if supplier_id:
            iq = iq.where(Invoice.supplier_id == supplier_id)
        if q:
            iq = iq.where(search(Supplier.name, Invoice.invoice_number))
        for inv, supplier_name in g.db.execute(iq).all():
            results = invoice_results(inv.id)
            state, cls = invoice_state(inv, results)
            if show == "hold" and state not in ("On hold", "Not checked"):
                continue
            if show == "clear" and state != "Clear for payment":
                continue
            if show == "done" and state not in ("Approved for payment", "Paid"):
                continue
            out["invoices"].append({"inv": inv, "supplier": supplier_name, "state": state,
                                    "cls": cls, "summary": matching.invoice_summary(inv, results)})
            if len(out["invoices"]) == 10:
                break

        # open orders with how much of each has arrived
        oq = (select(PurchaseOrder, Supplier.name)
              .join(Supplier, Supplier.id == PurchaseOrder.supplier_id)
              .where(PurchaseOrder.status == "open")
              .order_by(PurchaseOrder.id.desc()).limit(8))
        if supplier_id:
            oq = oq.where(PurchaseOrder.supplier_id == supplier_id)
        if q:
            oq = oq.where(search(Supplier.name, PurchaseOrder.po_number))
        for po, supplier_name in g.db.execute(oq).all():
            rows = order_rows(po)
            ordered = sum(r["ordered"] for r in rows)
            arrived = sum(min(r["delivered"], r["ordered"]) for r in rows)
            out["orders"].append({"id": po.id, "number": po.po_number, "supplier": supplier_name,
                                  "date": po.order_date,
                                  "percent": int(arrived * 100 / ordered) if ordered else 0})
        return out

    app.jinja_env.globals["dash"] = dash

    def todo(limit=8):
        """Plain-language list of what needs this person to act now."""
        role = g.user.role
        items = []

        def add(tone, text, url, label):
            items.append({"tone": tone, "text": text, "url": url, "label": label})

        if role in ("admin", "finance"):
            q = (select(Invoice, Supplier.name).join(Supplier, Supplier.id == Invoice.supplier_id)
                 .where(Invoice.payment_status != "paid")
                 .order_by(Invoice.invoice_date.desc(), Invoice.id.desc()).limit(60))
            for inv, supplier_name in g.db.execute(q).all():
                results = invoice_results(inv.id)
                state, _ = invoice_state(inv, results)
                where = url_for("invoice_detail", invoice_id=inv.id)
                who = f"Invoice {inv.invoice_number} from {supplier_name}"
                if state == "On hold":
                    held = matching.invoice_summary(inv, results)["blocked"]
                    add("err", f"{who} is on hold. KES {held:,.2f} must not be paid yet.",
                        where, "Review")
                elif state == "Not checked":
                    add("warn", f"{who} has not been checked yet.", where, "Check")
                elif state == "Clear for payment":
                    add("ok", f"{who} matches. It can be approved for payment.", where, "Approve")
                elif state == "Approved for payment":
                    add("ok", f"{who} is approved. Mark it paid once the money is sent.",
                        where, "Open")
        if role in ("admin", "storekeeper"):
            for r in g.db.scalars(select(LabourInvoice).where(LabourInvoice.status == "pending")
                                  .order_by(LabourInvoice.id)).all():
                add("warn", f"{r.contractor} billed KES {Decimal(str(r.amount)):,.2f} for "
                    f"\"{r.description[:50]}\". Confirm the work is done.",
                    url_for("labour_detail", labour_id=r.id), "Certify")
        if role in ("admin", "finance"):
            for r in g.db.scalars(select(LabourInvoice).where(
                    LabourInvoice.status.in_(("certified", "approved")))
                    .order_by(LabourInvoice.id)).all():
                step = "Approve it" if r.status == "certified" else "Mark it paid"
                add("ok", f"{r.contractor}'s labour invoice (KES {Decimal(str(r.amount)):,.2f}) "
                    f"is {'certified' if r.status == 'certified' else 'approved'}. {step}.",
                    url_for("labour_detail", labour_id=r.id), "Open")
        if role in ("admin", "procurement"):
            oq = (select(PurchaseOrder, Supplier.name)
                  .join(Supplier, Supplier.id == PurchaseOrder.supplier_id)
                  .where(PurchaseOrder.status == "open").order_by(PurchaseOrder.id.desc()).limit(30))
            for po, supplier_name in g.db.execute(oq).all():
                rows = order_rows(po)
                ordered = sum(r["ordered"] for r in rows)
                arrived = sum(min(r["delivered"], r["ordered"]) for r in rows)
                pct = int(arrived * 100 / ordered) if ordered else 0
                where = url_for("order_detail", po_id=po.id)
                if pct >= 100:
                    add("ok", f"Order {po.po_number} from {supplier_name} has fully arrived. "
                        "Close it if nothing else is expected.", where, "Open")
                else:
                    add("warn", f"Order {po.po_number} from {supplier_name}: "
                        f"{pct}% has arrived.", where, "Open")
        return {"items": items[:limit], "more": max(len(items) - limit, 0)}

    app.jinja_env.globals["todo"] = todo

    # ------------------------------------------------------------------- orders
    def order_rows(po):
        """Ordered vs delivered vs invoiced for each line of an order."""
        rows = []
        for ln in sorted(po.lines, key=lambda l: l.material.name):
            ordered = Decimal(str(ln.quantity))
            delivered = ZERO
            for dl in g.db.scalars(
                    select(DeliveryLine).join(DeliveryNote, DeliveryNote.id == DeliveryLine.note_id)
                    .where(DeliveryNote.po_id == po.id, DeliveryLine.material_id == ln.material_id)):
                delivered += matching._usable(dl)
            invoiced = Decimal(str(g.db.scalar(
                select(func.coalesce(func.sum(InvoiceLine.quantity), 0))
                .join(Invoice, Invoice.id == InvoiceLine.invoice_id)
                .where(Invoice.po_id == po.id, InvoiceLine.material_id == ln.material_id)) or 0))
            if delivered == 0:
                label, cls = "Nothing delivered", "warn"
            elif delivered < ordered:
                label, cls = "Short", "warn"
            elif delivered > ordered:
                label, cls = "Over delivered", "err"
            else:
                label, cls = "Complete", "ok"
            rows.append({"line": ln, "ordered": ordered, "delivered": delivered,
                         "invoiced": invoiced, "outstanding": max(ordered - delivered, ZERO),
                         "label": label, "cls": cls})
        return rows

    def next_po_number():
        year = date.today().year
        n = g.db.scalar(select(func.count()).select_from(PurchaseOrder)) or 0
        while True:
            n += 1
            number = f"PO-{year}-{n:04d}"
            if not g.db.scalar(select(PurchaseOrder).where(PurchaseOrder.po_number == number)):
                return number

    @app.route("/orders")
    @access("procurement", "finance")
    def orders():
        show = request.args.get("show", "")
        q = (select(PurchaseOrder, Supplier.name)
             .join(Supplier, Supplier.id == PurchaseOrder.supplier_id)
             .order_by(PurchaseOrder.id.desc()).limit(200))
        if show in ("open", "closed"):
            q = q.where(PurchaseOrder.status == show)
        rows = []
        for po, supplier_name in g.db.execute(q).all():
            progress = order_rows(po)
            rows.append({"po": po, "supplier": supplier_name, "items": len(po.lines),
                         "complete": bool(progress) and all(r["label"] == "Complete" for r in progress)})
        return render_template("orders.html", rows=rows, show=show)

    @app.route("/orders/new", methods=["GET", "POST"])
    @access("procurement")
    def order_new():
        suppliers = active_suppliers()
        today = date.today().isoformat()

        def hints_for(supplier_id, materials):
            return {m.id: (current_price_row(supplier_id, m.id).unit_price
                           if current_price_row(supplier_id, m.id) else None) for m in materials}

        if request.method == "GET":
            supplier = g.db.get(Supplier, request.args.get("supplier", type=int) or 0)
            materials = supplier_materials(supplier.id) if supplier else []
            return render_template("order_new.html", suppliers=suppliers, supplier=supplier,
                                   materials=materials, hints=hints_for(supplier.id, materials)
                                   if supplier else {}, values={}, today=today)

        supplier = g.db.get(Supplier, request.form.get("supplier_id", type=int) or 0)
        if supplier is None:
            flash("Choose the supplier first.", "error")
            return redirect(url_for("order_new"))
        materials = supplier_materials(supplier.id)
        errors, lines = [], []
        when = parse_past_date(request.form.get("order_date", ""))
        if when is None:
            errors.append("Enter a valid order date that is not in the future.")
        for m in materials:
            try:
                qty = parse_qty(request.form.get(f"qty_{m.id}"))
                price = parse_money(request.form.get(f"price_{m.id}"))
            except ValueError:
                errors.append(f"{m.name}: enter a valid quantity and a price above zero.")
                continue
            if qty:
                lines.append((m, qty, price))
            elif price is not None:
                errors.append(f"{m.name}: a price was entered but no quantity.")
        if not lines and not errors:
            errors.append("Enter the quantity for at least one item.")
        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("order_new.html", suppliers=suppliers, supplier=supplier,
                                   materials=materials, hints=hints_for(supplier.id, materials),
                                   values=request.form,
                                   today=request.form.get("order_date") or today)
        po = PurchaseOrder(po_number=next_po_number(), supplier_id=supplier.id,
                           order_date=when, status="open", created_by=g.user.id)
        g.db.add(po)
        g.db.flush()
        for m, qty, price in lines:
            # With no price typed, the current list price becomes the agreed price.
            if price is None:
                row = current_price_row(supplier.id, m.id)
                price = row.unit_price if row is not None else None
            g.db.add(POLine(po_id=po.id, material_id=m.id, quantity=qty, agreed_price=price))
        log("order_created", f"{po.po_number} {supplier.name}: {len(lines)} item(s)")
        g.db.commit()
        flash(f"Order {po.po_number} saved.", "ok")
        return redirect(url_for("order_detail", po_id=po.id))

    @app.route("/orders/<int:po_id>")
    @access("procurement", "finance")
    def order_detail(po_id):
        po = g.db.get(PurchaseOrder, po_id) or abort(404)
        supplier = g.db.get(Supplier, po.supplier_id)
        notes = g.db.scalars(select(DeliveryNote).where(DeliveryNote.po_id == po.id)
                             .order_by(DeliveryNote.delivery_date)).all()
        invs = g.db.scalars(select(Invoice).where(Invoice.po_id == po.id)
                            .order_by(Invoice.invoice_date)).all()
        return render_template("order_detail.html", po=po, supplier=supplier,
                               rows=order_rows(po), notes=notes, invoices=invs)

    @app.route("/orders/<int:po_id>/close", methods=["POST"])
    @access("procurement")
    def order_close(po_id):
        po = g.db.get(PurchaseOrder, po_id) or abort(404)
        po.status = "closed" if po.status == "open" else "open"
        log("order_status", f"{po.po_number} -> {po.status}")
        g.db.commit()
        return redirect(url_for("order_detail", po_id=po.id))

    # ------------------------------------------------- labour and service invoices
    register_labour(app, access, log, parse_money, parse_past_date, check_upload, store_upload)