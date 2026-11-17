"""Delivery notes and invoices: manual entry, photo entry (AI reads, a person confirms),
and matching. Hooked into app.py with register(app, access, log)."""
import base64
import difflib
import json
import os
import re
import secrets
from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from flask import (abort, flash, g, redirect, render_template, request,
                   send_from_directory, url_for)
from sqlalchemy import select

import docmatch
from docs_models import (DocAlias, DocDelivery, DocDeliveryLine, DocInvoice,
                         DocInvoiceLine)
from models import Material, Supplier, SupplierPrice

UPLOAD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "uploads")
MEDIA = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
         ".webp": "image/webp"}
IMAGE_NAME = re.compile(r"^[0-9a-f]{32}\.(png|jpg|jpeg|webp)$")
DELIVERY_ROLES = ("storekeeper", "procurement")
INVOICE_ROLES = ("finance", "procurement")


# ----------------------------------------------------------------- helpers
def dec(text):
    try:
        return Decimal(str(text).strip().replace(",", ""))
    except (InvalidOperation, AttributeError):
        return None


def to_date(text):
    try:
        return datetime.strptime((text or "").strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


def norm(text):
    return " ".join((text or "").lower().split())


def at(items, i):
    return items[i] if i < len(items) else ""


def supplier_materials():
    rows = g.db.execute(
        select(SupplierPrice.supplier_id, Material.id, Material.name, Material.unit)
        .join(Material, Material.id == SupplierPrice.material_id).distinct()).all()
    out = defaultdict(list)
    for sid, mid, name, unit in rows:
        out[str(sid)].append({"id": mid, "name": name, "unit": unit})
    for v in out.values():
        v.sort(key=lambda m: m["name"])
    return out


def agreed_price(sid, mid, on):
    base = select(SupplierPrice).where(
        SupplierPrice.supplier_id == sid, SupplierPrice.material_id == mid,
        SupplierPrice.unit_price.is_not(None))
    row = g.db.scalar(base.where(SupplierPrice.effective_from <= on)
                      .order_by(SupplierPrice.effective_from.desc(),
                                SupplierPrice.id.desc()).limit(1))
    if row is None:  # price entered after the invoice date: use the earliest known
        row = g.db.scalar(base.order_by(SupplierPrice.effective_from.asc(),
                                        SupplierPrice.id.asc()).limit(1))
    return docmatch.D(row.unit_price) if row else None


def compute_result(inv):
    sid = inv.supplier_id
    delivered = defaultdict(lambda: docmatch.ZERO)
    for dl in g.db.scalars(select(DocDeliveryLine).join(DocDelivery)
                           .where(DocDelivery.supplier_id == sid)):
        delivered[dl.material_id] += docmatch.net_delivered(dl.qty_delivered, dl.qty_returned)
    billed = defaultdict(lambda: docmatch.ZERO)
    for il in g.db.scalars(select(DocInvoiceLine).join(DocInvoice)
                           .where(DocInvoice.supplier_id == sid, DocInvoice.id < inv.id)):
        billed[il.material_id] += docmatch.D(il.qty)
    available = {m: q - billed[m] for m, q in delivered.items()}
    lines = [{"material_id": l.material_id, "name": l.material.name,
              "qty": l.qty, "price": l.unit_price} for l in inv.lines]
    expected = {l.material_id: agreed_price(sid, l.material_id, inv.invoice_date)
                for l in inv.lines}
    return docmatch.check_invoice(lines, available, expected, inv.stated_total)


PROMPT = (
    "This is a photo of a supplier {kind} from a Kenyan hardware supplier. "
    "Read it and reply with ONLY a JSON object, no other text, shaped like: "
    '{{"number": "document number or empty", "date": "YYYY-MM-DD or empty", '
    '"total": number or null, "lines": [{{"description": "item exactly as written", '
    '"qty": number, "unit_price": number or null, "returned_qty": number}}]}}. '
    "Set returned_qty to the full quantity if the line is marked returned/rejected "
    "by hand, otherwise 0. Use null when a figure is not visible. Never guess a "
    "figure you cannot read."
)


def extract_from_image(path, kind):
    import anthropic
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY is missing in the .env file.")
    media = MEDIA[os.path.splitext(path)[1].lower()]
    with open(path, "rb") as fh:
        data = base64.standard_b64encode(fh.read()).decode()
    resp = anthropic.Anthropic(api_key=key).messages.create(
        model=os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6"), max_tokens=3000,
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": media, "data": data}},
            {"type": "text", "text": PROMPT.format(kind=kind)}]}])
    text = "".join(b.text for b in resp.content if b.type == "text")
    return json.loads(text[text.find("{"): text.rfind("}") + 1])


def map_material(sid, written, materials):
    """Alias saved earlier -> exact name -> closest name -> nothing."""
    key = norm(written)
    if not key:
        return None
    alias = g.db.scalar(select(DocAlias).where(DocAlias.supplier_id == sid,
                                               DocAlias.alias == key))
    if alias:
        return alias.material_id
    names = {norm(m["name"]): m["id"] for m in materials}
    if key in names:
        return names[key]
    close = difflib.get_close_matches(key, list(names), n=1, cutoff=0.6)
    return names[close[0]] if close else None


# ------------------------------------------------------------------ routes
def register(app, access, log):
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024

    def render_form(kind, form, error=None):
        suppliers = g.db.scalars(select(Supplier).where(Supplier.active.is_(True))
                                 .order_by(Supplier.name)).all()
        return render_template("doc_form.html", kind=kind, form=form, error=error,
                               suppliers=suppliers, sm=supplier_materials())

    def blank():
        return {"supplier_id": "", "number": "", "date": date.today().isoformat(),
                "total": "", "image": "", "scanned": False, "lines": [
                    {"material_id": "", "written": "", "qty": "", "returned": "", "price": ""}]}

    def read_form():
        f = request.form
        ids, wr, qty = f.getlist("material_id"), f.getlist("written"), f.getlist("qty")
        ret, pr = f.getlist("returned"), f.getlist("price")
        return {"supplier_id": f.get("supplier_id", ""), "number": f.get("number", "").strip(),
                "date": f.get("date", ""), "total": f.get("total", ""),
                "image": f.get("image", ""), "scanned": f.get("scanned") == "1",
                "lines": [{"material_id": ids[i], "written": at(wr, i), "qty": at(qty, i),
                           "returned": at(ret, i), "price": at(pr, i)} for i in range(len(ids))]}

    def scan(kind):
        form = blank()
        form["supplier_id"] = request.form.get("supplier_id", "")
        sid = request.form.get("supplier_id", type=int)
        photo = request.files.get("photo")
        if not sid or not g.db.get(Supplier, sid):
            return render_form(kind, form, "Choose the supplier before uploading the photo.")
        ext = os.path.splitext(photo.filename or "")[1].lower() if photo else ""
        if ext not in MEDIA:
            return render_form(kind, form, "Upload a photo (.jpg, .png or .webp).")
        name = secrets.token_hex(16) + ext
        path = os.path.join(UPLOAD_DIR, name)
        photo.save(path)
        try:
            data = extract_from_image(path, "delivery note" if kind == "delivery" else "invoice")
        except Exception as exc:  # shown to the user; they can still type it in
            return render_form(kind, form, f"Could not read the photo ({exc}). "
                                           "You can enter the document by hand.")
        mats = supplier_materials().get(str(sid), [])
        lines = []
        for ln in data.get("lines") or []:
            written = str(ln.get("description") or "")
            mid = map_material(sid, written, mats)
            lines.append({"material_id": mid or "", "written": written,
                          "qty": ln.get("qty") if ln.get("qty") is not None else "",
                          "returned": ln.get("returned_qty") or 0,
                          "price": ln.get("unit_price") if ln.get("unit_price") is not None else ""})
        form.update({"number": str(data.get("number") or ""), "date": data.get("date") or form["date"],
                     "total": data.get("total") if data.get("total") is not None else "",
                     "image": name, "scanned": True, "lines": lines or form["lines"]})
        return render_form(kind, form)

    def save(kind, form):
        sid = int(form["supplier_id"]) if form["supplier_id"].isdigit() else None
        when = to_date(form["date"])
        if not sid or not g.db.get(Supplier, sid):
            return render_form(kind, form, "Choose the supplier.")
        if when is None:
            return render_form(kind, form, "Enter a valid date.")
        if not form["number"]:
            return render_form(kind, form, "Enter the document number printed on the paper.")
        valid_ids = {m["id"] for m in supplier_materials().get(str(sid), [])}
        clean = []
        for n, ln in enumerate(form["lines"], 1):
            if not (ln["material_id"] or ln["qty"] or ln["written"]):
                continue  # empty row
            mid = int(ln["material_id"]) if str(ln["material_id"]).isdigit() else None
            qty = dec(ln["qty"])
            if mid not in valid_ids:
                return render_form(kind, form, f"Line {n}: choose the material this supplier supplies.")
            if qty is None or qty <= 0:
                return render_form(kind, form, f"Line {n}: enter a quantity above zero.")
            row = {"mid": mid, "written": ln["written"].strip()[:200], "qty": qty}
            if kind == "delivery":
                ret = dec(ln["returned"]) if str(ln["returned"]).strip() else docmatch.ZERO
                if ret is None or ret < 0 or ret > qty:
                    return render_form(kind, form, f"Line {n}: returned must be between 0 and the quantity delivered.")
                row["returned"] = ret
            else:
                price = dec(ln["price"])
                if price is None or price <= 0:
                    return render_form(kind, form, f"Line {n}: enter the unit price on the invoice.")
                row["price"] = price
            clean.append(row)
        if not clean:
            return render_form(kind, form, "Add at least one line.")
        image = form["image"] if IMAGE_NAME.match(form["image"] or "") else ""

        if kind == "delivery":
            doc = DocDelivery(supplier_id=sid, note_no=form["number"], delivery_date=when,
                              created_by=g.user.id, image_path=image,
                              lines=[DocDeliveryLine(material_id=r["mid"], written_name=r["written"],
                                                     qty_delivered=r["qty"], qty_returned=r["returned"])
                                     for r in clean])
        else:
            if g.db.scalar(select(DocInvoice).where(DocInvoice.supplier_id == sid,
                                                    DocInvoice.invoice_no == form["number"])):
                return render_form(kind, form, "That invoice number is already recorded for this supplier.")
            total = dec(form["total"]) if str(form["total"]).strip() else None
            doc = DocInvoice(supplier_id=sid, invoice_no=form["number"], invoice_date=when,
                             stated_total=total, created_by=g.user.id, image_path=image,
                             lines=[DocInvoiceLine(material_id=r["mid"], written_name=r["written"],
                                                   qty=r["qty"], unit_price=r["price"])
                                    for r in clean])
        g.db.add(doc)
        # Remember how this supplier writes each item, so the next photo maps itself.
        for r in clean:
            key = norm(r["written"])
            mat = g.db.get(Material, r["mid"])
            if key and key != norm(mat.name) and not g.db.scalar(
                    select(DocAlias).where(DocAlias.supplier_id == sid, DocAlias.alias == key)):
                g.db.add(DocAlias(supplier_id=sid, material_id=r["mid"], alias=key))
        g.db.flush()
        log(f"{kind}_added", f"{form['number']} / supplier {sid}")
        g.db.commit()
        if kind == "invoice":
            flash("Invoice saved and matched.", "ok")
            return redirect(url_for("invoice_detail", invoice_id=doc.id))
        flash("Delivery recorded.", "ok")
        return redirect(url_for("delivery_list"))

    def make_new(kind):
        def view():
            if request.method == "GET":
                return render_form(kind, blank())
            if request.form.get("action") == "scan":
                return scan(kind)
            return save(kind, read_form())
        return view

    for kind, roles in (("delivery", DELIVERY_ROLES), ("invoice", INVOICE_ROLES)):
        app.add_url_rule(f"/{kind}s/new", f"{kind}_new", access(*roles)(make_new(kind)),
                         methods=["GET", "POST"])

    @app.route("/deliveries")
    @access(*DELIVERY_ROLES, "finance")
    def delivery_list():
        rows = g.db.scalars(select(DocDelivery).order_by(DocDelivery.delivery_date.desc(),
                                                         DocDelivery.id.desc())).all()
        return render_template("doc_list.html", kind="delivery", rows=rows,
                               can_add=g.user.role in ("admin", *DELIVERY_ROLES))

    @app.route("/invoices")
    @access(*INVOICE_ROLES)
    def invoice_list():
        rows = g.db.scalars(select(DocInvoice).order_by(DocInvoice.invoice_date.desc(),
                                                        DocInvoice.id.desc())).all()
        verdicts = {r.id: compute_result(r)["verdict"] for r in rows}
        return render_template("doc_list.html", kind="invoice", rows=rows,
                               verdicts=verdicts, can_add=True)

    @app.route("/invoices/<int:invoice_id>")
    @access(*INVOICE_ROLES)
    def invoice_detail(invoice_id):
        inv = g.db.get(DocInvoice, invoice_id) or abort(404)
        return render_template("invoice_result.html", inv=inv, res=compute_result(inv))

    @app.route("/documents/photo/<name>")
    @access()
    def document_photo(name):
        if not IMAGE_NAME.match(name):
            abort(404)
        return send_from_directory(UPLOAD_DIR, name)