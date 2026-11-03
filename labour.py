"""Labour and service invoices (floor finishing, plastering, transport and similar).

These bill work done, not goods delivered, so there is no delivery note to match.
Instead the work is certified on site before it can be paid:

  finance      enters the invoice, approves it after certification, marks it paid
  storekeeper  certifies that the work was really done (the person on site)
  procurement  can view only
  admin        everything

The person who enters an invoice cannot approve their own: the site certifies first.
"""
from datetime import date, datetime
from decimal import Decimal

from flask import abort, flash, g, redirect, render_template, request, url_for
from sqlalchemy import Column, Date, DateTime, ForeignKey, Integer, Numeric, String, Text, select

from models import Base, User


class LabourInvoice(Base):
    __tablename__ = "labour_invoices"
    id = Column(Integer, primary_key=True)
    contractor = Column(String(120), nullable=False)
    invoice_number = Column(String(50), nullable=True)
    invoice_date = Column(Date, default=date.today)
    description = Column(Text, nullable=False)
    amount = Column(Numeric(12, 2), nullable=False)
    status = Column(String(20), default="pending")   # pending, certified, approved, paid
    photo_path = Column(String(255), nullable=True)
    entered_by = Column(ForeignKey("users.id"), nullable=True)
    certified_by = Column(ForeignKey("users.id"), nullable=True)
    certified_at = Column(DateTime, nullable=True)
    approved_by = Column(ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


STATE = {"pending": ("Waiting for site certification", "warn"),
         "certified": ("Work certified", "ok"),
         "approved": ("Approved for payment", "ok"),
         "paid": ("Paid", "ok")}


def register_labour(app, access, log, parse_money, parse_past_date, check_upload, store_upload):
    app.jinja_env.filters["labour_state"] = lambda s: STATE.get(s, (s, "warn"))[0]
    app.jinja_env.filters["labour_class"] = lambda s: STATE.get(s, (s, "warn"))[1]

    def names(*user_ids):
        ids = [i for i in user_ids if i]
        found = {u.id: u.name for u in g.db.scalars(select(User).where(User.id.in_(ids or [0])))}
        return found

    @app.route("/labour")
    @access("storekeeper", "procurement", "finance")
    def labour():
        show = request.args.get("show", "")
        q = select(LabourInvoice).order_by(LabourInvoice.invoice_date.desc(),
                                           LabourInvoice.id.desc()).limit(200)
        if show in STATE:
            q = q.where(LabourInvoice.status == show)
        return render_template("labour.html", rows=g.db.scalars(q).all(), show=show)

    @app.route("/labour/new", methods=["GET", "POST"])
    @access("finance")
    def labour_new():
        if request.method == "GET":
            return render_template("labour_new.html", values={}, today=date.today().isoformat())
        errors = []
        contractor = " ".join(request.form.get("contractor", "").split())[:120]
        number = " ".join(request.form.get("invoice_number", "").split())[:50]
        description = " ".join(request.form.get("description", "").split())[:1000]
        when = parse_past_date(request.form.get("invoice_date", ""))
        try:
            amount = parse_money(request.form.get("amount"))
        except ValueError:
            amount = None
        if not contractor:
            errors.append("Enter who did the work.")
        if not description:
            errors.append("Describe the work that was done.")
        if amount is None:
            errors.append("Enter the amount in KES, above zero.")
        if when is None:
            errors.append("Enter a valid date that is not in the future.")
        photo = request.files.get("photo")
        problem = check_upload(photo)
        if problem:
            errors.append(problem)
        if not errors:
            same = select(LabourInvoice).where(LabourInvoice.contractor == contractor)
            if number and g.db.scalar(same.where(LabourInvoice.invoice_number == number)):
                errors.append(f"Invoice {number} from {contractor} has already been entered.")
            elif g.db.scalar(same.where(LabourInvoice.amount == amount,
                                        LabourInvoice.invoice_date == when)):
                errors.append("An invoice from this person for the same amount and date "
                              "already exists. It looks like a duplicate.")
        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("labour_new.html", values=request.form,
                                   today=request.form.get("invoice_date") or date.today().isoformat())
        inv = LabourInvoice(contractor=contractor, invoice_number=number or None, invoice_date=when,
                            description=description, amount=amount, entered_by=g.user.id,
                            photo_path=store_upload(photo, app.config["UPLOAD_FOLDER"]))
        g.db.add(inv)
        g.db.flush()
        log("labour_entered", f"{contractor} {amount}")
        g.db.commit()
        flash("Labour invoice saved. It now waits for the site to certify the work.", "ok")
        return redirect(url_for("labour_detail", labour_id=inv.id))

    @app.route("/labour/<int:labour_id>")
    @access("storekeeper", "procurement", "finance")
    def labour_detail(labour_id):
        inv = g.db.get(LabourInvoice, labour_id) or abort(404)
        return render_template("labour_detail.html", inv=inv,
                               who=names(inv.entered_by, inv.certified_by, inv.approved_by))

    def step(labour_id, from_status, to_status, action, message, need_other_than_enterer=False):
        inv = g.db.get(LabourInvoice, labour_id) or abort(404)
        if inv.status != from_status:
            flash("That step is not available for this invoice now.", "error")
        else:
            inv.status = to_status
            if to_status == "certified":
                inv.certified_by, inv.certified_at = g.user.id, datetime.utcnow()
            if to_status == "approved":
                inv.approved_by = g.user.id
            log(action, f"{inv.contractor} {inv.amount}")
            g.db.commit()
            flash(message, "ok")
        return redirect(url_for("labour_detail", labour_id=inv.id))

    @app.route("/labour/<int:labour_id>/certify", methods=["POST"])
    @access("storekeeper")
    def labour_certify(labour_id):
        return step(labour_id, "pending", "certified", "labour_certified",
                    "Work certified as done.")

    @app.route("/labour/<int:labour_id>/approve", methods=["POST"])
    @access("finance")
    def labour_approve(labour_id):
        return step(labour_id, "certified", "approved", "labour_approved",
                    "Approved for payment.")

    @app.route("/labour/<int:labour_id>/paid", methods=["POST"])
    @access("finance")
    def labour_paid(labour_id):
        return step(labour_id, "approved", "paid", "labour_paid", "Marked as paid.")