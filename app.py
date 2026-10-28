"""Flask website for the Invoice & Delivery Note Matching System.

Part 1: login + roles, admin price changes (history kept), add supplier/materials,
        admin user management.
"""
import os
import secrets
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from functools import wraps

from dotenv import load_dotenv

load_dotenv()

from flask import (  # noqa: E402
    Flask, abort, flash, g, redirect, render_template, request, session, url_for,
)
from flask_wtf.csrf import CSRFProtect  # noqa: E402
from sqlalchemy import create_engine, func, select  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from werkzeug.security import check_password_hash, generate_password_hash  # noqa: E402

import config  # noqa: E402
from models import (  # noqa: E402
    AuditLog, Base, Material, Supplier, SupplierPrice, User,
)

ROLES = ("admin", "procurement", "storekeeper", "finance")
UNITS = ("piece", "bag", "packet", "roll", "coil", "truckload", "load",
         "metre", "litre", "kg", "tonne", "set")


def create_app(database_url: str | None = None, testing: bool = False) -> Flask:
    app = Flask(__name__)

    secret = os.environ.get("SECRET_KEY")
    if not secret:
        # Random key per start: works for testing, but everyone is logged out
        # on restart. Set SECRET_KEY in .env for real use.
        secret = secrets.token_hex(32)
        print("WARNING: SECRET_KEY not set in .env - using a temporary key.")
    app.config.update(
        SECRET_KEY=secret,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        TESTING=testing,
        WTF_CSRF_ENABLED=not testing,
    )
    CSRFProtect(app)

    engine = create_engine(database_url or config.DATABASE_URL, pool_pre_ping=True)
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(engine, expire_on_commit=False)

    # ------------------------------------------------------------ helpers
    @app.before_request
    def load_user():
        g.db = SessionLocal()
        g.user = None
        uid = session.get("user_id")
        if uid:
            user = g.db.get(User, uid)
            if user and user.active:
                g.user = user
            else:
                session.clear()

    @app.teardown_request
    def close_db(exc):
        db = g.pop("db", None)
        if db is not None:
            if exc:
                db.rollback()
            db.close()

    def access(*roles):
        """Require login. Admin can open everything; others need a listed role."""
        def deco(f):
            @wraps(f)
            def wrapper(*a, **k):
                if g.user is None:
                    return redirect(url_for("login", next=request.path))
                if roles and g.user.role != "admin" and g.user.role not in roles:
                    abort(403)
                return f(*a, **k)
            return wrapper
        return deco

    def log(action: str, details: str = ""):
        g.db.add(AuditLog(user_id=g.user.id if g.user else None,
                          action=action, details=details))

    def current_price_row(supplier_id: int, material_id: int):
        """Latest price that is already in effect (not future-dated).

        Empty placeholder rows (unit_price NULL) never override a real price.
        If no real price is in effect yet, return the placeholder so the
        supplier/material link is still found and shows as 'Not set'.
        """
        base = select(SupplierPrice).where(
            SupplierPrice.supplier_id == supplier_id,
            SupplierPrice.material_id == material_id)
        priced = g.db.scalar(
            base.where(SupplierPrice.unit_price.is_not(None),
                       SupplierPrice.effective_from <= date.today())
            .order_by(SupplierPrice.effective_from.desc(), SupplierPrice.id.desc())
            .limit(1))
        if priced is not None:
            return priced
        return g.db.scalar(base.order_by(SupplierPrice.id.asc()).limit(1))

    def parse_price(text: str):
        try:
            value = Decimal(text.strip().replace(",", ""))
        except (InvalidOperation, AttributeError):
            return None
        if value <= 0 or value > Decimal("99999999"):
            return None
        return value.quantize(Decimal("0.01"))

    def parse_date(text: str):
        if not text:
            return date.today()
        try:
            return datetime.strptime(text, "%Y-%m-%d").date()
        except ValueError:
            return None

    @app.context_processor
    def inject():
        return {"user": g.get("user"), "UNITS": UNITS, "ROLES": ROLES}

    @app.errorhandler(403)
    def forbidden(_):
        return render_template("error.html", code=403,
                               message="You do not have permission to open this page."), 403

    @app.errorhandler(404)
    def not_found(_):
        return render_template("error.html", code=404, message="Page not found."), 404

    # --------------------------------------------------------- login/logout
    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            email = request.form.get("email", "").strip().lower()
            password = request.form.get("password", "")
            user = g.db.scalar(select(User).where(User.email == email))
            if user and user.active and check_password_hash(user.password_hash, password):
                session.clear()
                session["user_id"] = user.id
                g.user = user
                log("login", email)
                g.db.commit()
                nxt = request.args.get("next", "")
                if nxt.startswith("/") and not nxt.startswith("//"):
                    return redirect(nxt)
                return redirect(url_for("home"))
            flash("Wrong email or password.", "error")
        return render_template("login.html")

    @app.route("/logout", methods=["POST"])
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.route("/")
    @access()
    def home():
        stats = None
        if g.user.role == "admin":
            links = g.db.execute(
                select(SupplierPrice.supplier_id, SupplierPrice.material_id).distinct()
            ).all()
            unset = sum(1 for sid, mid in links
                        if current_price_row(sid, mid).unit_price is None)
            stats = {
                "suppliers": g.db.scalar(select(func.count()).select_from(Supplier)
                                         .where(Supplier.active.is_(True))),
                "materials": g.db.scalar(select(func.count()).select_from(Material)
                                         .where(Material.active.is_(True))),
                "users": g.db.scalar(select(func.count()).select_from(User)
                                     .where(User.active.is_(True))),
                "unset": unset,
            }
        return render_template("home.html", stats=stats)

    # --------------------------------------------------------------- prices
    @app.route("/admin/prices")
    @access("admin")
    def prices():
        supplier_id = request.args.get("supplier", type=int)
        suppliers = g.db.scalars(select(Supplier).order_by(Supplier.name)).all()
        links = g.db.execute(
            select(SupplierPrice.supplier_id, SupplierPrice.material_id).distinct()
        ).all()
        rows = []
        for sid, mid in links:
            if supplier_id and sid != supplier_id:
                continue
            supplier = g.db.get(Supplier, sid)
            material = g.db.get(Material, mid)
            cur = current_price_row(sid, mid)
            rows.append({
                "supplier": supplier, "material": material,
                "price": cur.unit_price if cur else None,
                "since": cur.effective_from if cur else None,
            })
        rows.sort(key=lambda r: (r["supplier"].name, r["material"].name))
        return render_template("prices.html", rows=rows, suppliers=suppliers,
                               selected=supplier_id, today=date.today().isoformat())

    @app.route("/admin/prices/<int:supplier_id>/<int:material_id>", methods=["POST"])
    @access("admin")
    def change_price(supplier_id, material_id):
        supplier = g.db.get(Supplier, supplier_id) or abort(404)
        material = g.db.get(Material, material_id) or abort(404)
        if current_price_row(supplier_id, material_id) is None:
            abort(404)
        price = parse_price(request.form.get("price", ""))
        effective = parse_date(request.form.get("effective_from", ""))
        back = url_for("prices", supplier=request.form.get("back_supplier") or None)
        if price is None:
            flash("Enter a valid price greater than zero.", "error")
            return redirect(back)
        if effective is None:
            flash("Enter a valid date.", "error")
            return redirect(back)
        old = current_price_row(supplier_id, material_id)
        # Never overwrite: add a new history row.
        g.db.add(SupplierPrice(supplier_id=supplier_id, material_id=material_id,
                               unit_price=price, effective_from=effective,
                               changed_by=g.user.id))
        log("price_change",
            f"{supplier.name} / {material.name}: {old.unit_price if old else None} -> {price} "
            f"from {effective}")
        g.db.commit()
        if effective > date.today():
            flash(f"New price {price:,.2f} for {material.name} at {supplier.name} "
                  f"will apply from {effective}.", "ok")
        else:
            flash(f"{material.name} at {supplier.name} is now {price:,.2f}.", "ok")
        return redirect(back)

    @app.route("/admin/prices/<int:supplier_id>/<int:material_id>/history")
    @access("admin")
    def price_history(supplier_id, material_id):
        supplier = g.db.get(Supplier, supplier_id) or abort(404)
        material = g.db.get(Material, material_id) or abort(404)
        history = g.db.execute(
            select(SupplierPrice, User.name)
            .outerjoin(User, User.id == SupplierPrice.changed_by)
            .where(SupplierPrice.supplier_id == supplier_id,
                   SupplierPrice.material_id == material_id)
            .order_by(SupplierPrice.effective_from.desc(), SupplierPrice.id.desc())
        ).all()
        if not history:
            abort(404)
        return render_template("price_history.html", supplier=supplier,
                               material=material, history=history)

    # ------------------------------------------------------------ suppliers
    @app.route("/admin/suppliers")
    @access("admin")
    def suppliers():
        rows = g.db.scalars(select(Supplier).order_by(Supplier.name)).all()
        counts = {s.id: len({p.material_id for p in s.prices}) for s in rows}
        return render_template("suppliers.html", suppliers=rows, counts=counts)

    @app.route("/admin/suppliers/new", methods=["GET", "POST"])
    @access("admin")
    def supplier_new():
        if request.method == "POST":
            name = " ".join(request.form.get("name", "").split())
            if not name:
                flash("Enter the supplier name.", "error")
            elif g.db.scalar(select(Supplier).where(Supplier.name == name)):
                flash("A supplier with that name already exists.", "error")
            else:
                s = Supplier(name=name)
                g.db.add(s)
                g.db.flush()
                log("supplier_added", name)
                g.db.commit()
                flash("Supplier added. Now add the materials they supply.", "ok")
                return redirect(url_for("supplier_detail", supplier_id=s.id))
        return render_template("supplier_new.html")

    @app.route("/admin/suppliers/<int:supplier_id>")
    @access("admin")
    def supplier_detail(supplier_id):
        supplier = g.db.get(Supplier, supplier_id) or abort(404)
        material_ids = sorted({p.material_id for p in supplier.prices})
        items = []
        for mid in material_ids:
            material = g.db.get(Material, mid)
            cur = current_price_row(supplier_id, mid)
            items.append({"material": material,
                          "price": cur.unit_price if cur else None})
        items.sort(key=lambda i: i["material"].name)
        others = g.db.scalars(
            select(Material).where(Material.id.notin_(material_ids)
                                   if material_ids else True)
            .order_by(Material.name)
        ).all()
        return render_template("supplier_detail.html", supplier=supplier,
                               items=items, others=others)

    @app.route("/admin/suppliers/<int:supplier_id>/materials", methods=["POST"])
    @access("admin")
    def supplier_add_material(supplier_id):
        supplier = g.db.get(Supplier, supplier_id) or abort(404)
        back = redirect(url_for("supplier_detail", supplier_id=supplier_id))
        price_text = request.form.get("price", "").strip()
        price = None
        if price_text:
            price = parse_price(price_text)
            if price is None:
                flash("Enter a valid price, or leave it empty to set it later.", "error")
                return back

        existing_id = request.form.get("material_id", type=int)
        new_name = " ".join(request.form.get("new_name", "").split())
        if existing_id:
            material = g.db.get(Material, existing_id) or abort(404)
        elif new_name:
            unit = request.form.get("unit", "")
            if unit not in UNITS:
                flash("Choose a unit for the new material.", "error")
                return back
            material = g.db.scalar(select(Material).where(Material.name == new_name))
            if material is None:
                material = Material(name=new_name, unit=unit)
                g.db.add(material)
                g.db.flush()
                log("material_added", f"{new_name} ({unit})")
        else:
            flash("Pick an existing material or type a new material name.", "error")
            return back

        if g.db.scalar(select(SupplierPrice).where(
                SupplierPrice.supplier_id == supplier_id,
                SupplierPrice.material_id == material.id)):
            flash(f"{supplier.name} already supplies {material.name}.", "error")
            return back

        g.db.add(SupplierPrice(supplier_id=supplier_id, material_id=material.id,
                               unit_price=price, changed_by=g.user.id))
        log("supplier_material_added", f"{supplier.name} / {material.name} / {price}")
        g.db.commit()
        flash(f"{material.name} added to {supplier.name}.", "ok")
        return back

    # ---------------------------------------------------------------- users
    @app.route("/admin/users", methods=["GET", "POST"])
    @access("admin")
    def users():
        if request.method == "POST":
            name = " ".join(request.form.get("name", "").split())
            email = request.form.get("email", "").strip().lower()
            role = request.form.get("role", "")
            password = request.form.get("password", "")
            if not name or "@" not in email or role not in ROLES:
                flash("Enter a name, a valid email and a role.", "error")
            elif len(password) < 8:
                flash("Password must be at least 8 characters.", "error")
            elif g.db.scalar(select(User).where(User.email == email)):
                flash("A user with that email already exists.", "error")
            else:
                g.db.add(User(name=name, email=email, role=role,
                              password_hash=generate_password_hash(password)))
                log("user_added", f"{email} ({role})")
                g.db.commit()
                flash(f"User {name} created.", "ok")
            return redirect(url_for("users"))
        rows = g.db.scalars(select(User).order_by(User.name)).all()
        return render_template("users.html", rows=rows)

    @app.route("/admin/users/<int:user_id>/toggle", methods=["POST"])
    @access("admin")
    def user_toggle(user_id):
        target = g.db.get(User, user_id) or abort(404)
        if target.id == g.user.id:
            flash("You cannot deactivate your own account.", "error")
        else:
            target.active = not target.active
            log("user_active_changed", f"{target.email} -> {target.active}")
            g.db.commit()
        return redirect(url_for("users"))

    return app


if __name__ == "__main__":
    create_app().run(debug=True)