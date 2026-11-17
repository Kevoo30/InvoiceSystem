"""Orders, deliveries, invoices, photo reader and dashboards, end to end."""
import io
from datetime import date
from types import SimpleNamespace

import pytest
from PIL import Image
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from werkzeug.security import generate_password_hash

import load_prices
from app import create_app
from models import (Invoice, Material, MatchResult, PurchaseOrder, Supplier, SupplierPrice, User)
from seed import seed

HEKIWA = "Hekiwa Steel & General Hardware Ltd"
USERS = [("Ann Admin", "admin@x.com", "admin"), ("Sam Store", "store@x.com", "storekeeper"),
         ("Pat Proc", "proc@x.com", "procurement"), ("Fay Fin", "fin@x.com", "finance")]


class FakeClient:
    def __init__(self, text):
        self.text, self.messages = text, self

    def create(self, **kw):
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=self.text)])


@pytest.fixture
def c(tmp_path):
    url = f"sqlite:///{tmp_path/'t.db'}"
    app = create_app(url, testing=True)
    app.config["UPLOAD_FOLDER"] = str(tmp_path / "up")
    engine = create_engine(url)
    with Session(engine) as s:
        seed(s)
        for name, email, role in USERS:
            s.add(User(name=name, email=email, role=role,
                       password_hash=generate_password_hash("password123")))
        s.commit()
        for sup, mat, price, when in load_prices.PRICES:
            sid = s.scalar(select(Supplier.id).where(Supplier.name == sup))
            mid = s.scalar(select(Material.id).where(Material.name == mat))
            s.add(SupplierPrice(supplier_id=sid, material_id=mid, unit_price=price,
                                effective_from=when))
        s.commit()
    cl = app.test_client()
    cl.app, cl.engine = app, engine
    return cl


def login(c, who):
    c.post("/logout")
    email = {"admin": "admin@x.com", "store": "store@x.com", "proc": "proc@x.com",
             "fin": "fin@x.com"}[who]
    return c.post("/login", data={"email": email, "password": "password123"},
                  follow_redirects=True)


def ids(c):
    with Session(c.engine) as s:
        sup = s.scalar(select(Supplier).where(Supplier.name == HEKIWA)).id
        mats = {m.name: m.id for m in s.scalars(select(Material))}
    return sup, mats

def recent(response):
    """Only the 'Recent' part of the dashboard (the to-do list ignores search)."""
    return response.get_data(as_text=True).split("<h2>Recent</h2>")[1]

def png():
    b = io.BytesIO()
    Image.new("RGB", (50, 50), "white").save(b, "PNG")
    return b.getvalue()


def test_every_role_gets_a_dashboard_and_the_right_pages(c):
    for who in ("admin", "store", "proc", "fin"):
        assert login(c, who).status_code == 200
        assert c.get("/").status_code == 200
    expect = {"store": {"/deliveries": 200, "/deliveries/new": 200, "/orders": 403,
                        "/invoices": 403, "/invoices/new": 403, "/orders/new": 403},
              "proc": {"/orders": 200, "/orders/new": 200, "/deliveries": 200,
                       "/invoices": 200, "/deliveries/new": 403, "/invoices/new": 403,
                       "/admin/prices": 403},
              "fin": {"/orders": 200, "/deliveries": 200, "/invoices": 200,
                      "/invoices/new": 200, "/orders/new": 403, "/deliveries/new": 403},
              "admin": {"/orders/new": 200, "/deliveries/new": 200, "/invoices/new": 200,
                        "/admin/prices": 200}}
    for who, pages in expect.items():
        login(c, who)
        for path, code in pages.items():
            assert c.get(path).status_code == code, (who, path)


def test_order_delivery_invoice_chain(c):
    sup, m = ids(c)
    hoop, bar = m["Hoop iron 17 kg"], m["Deformed bar D12 mm"]
    login(c, "proc")
    r = c.post("/orders/new", data={"supplier_id": sup, "order_date": date.today().isoformat(),
                                    f"qty_{bar}": "57", f"qty_{hoop}": "8"})
    assert r.status_code == 302
    with Session(c.engine) as s:
        po = s.scalar(select(PurchaseOrder))
        assert {l.material_id: float(l.agreed_price) for l in po.lines} == {bar: 1080.0, hoop: 1400.0}
        po_id = po.id
    login(c, "store")
    c.post("/deliveries/new", data={"supplier_id": sup, "po_id": po_id, f"qty_{bar}": "32",
                                    f"qty_{hoop}": "8", f"ret_{hoop}": "8"})
    login(c, "fin")
    r = c.post("/invoices/new", data={"supplier_id": sup, "po_id": po_id, "invoice_number": "81231",
                                      f"qty_{bar}": "32", f"price_{bar}": "1080",
                                      f"qty_{hoop}": "8", f"price_{hoop}": "1400"},
               follow_redirects=True)
    assert b"Returned goods billed" in r.data
    page = c.get(f"/orders/{po_id}").data
    assert b"Short" in page and b"Over delivered" not in page
    with Session(c.engine) as s:
        inv = s.scalar(select(Invoice))
        assert inv.po_id == po_id
        rows = {r.material_id: r for r in s.scalars(select(MatchResult))}
        assert rows[hoop].status == "RETURNED_BILLED"
        assert float(rows[hoop].amount_blocked) == 11200.0
    # finance cannot approve it
    c.post(f"/invoices/{inv.id}/approve")
    with Session(c.engine) as s:
        assert s.scalar(select(Invoice)).payment_status == "unpaid"


def test_order_from_another_supplier_is_ignored(c):
    sup, m = ids(c)
    with Session(c.engine) as s:
        other = s.scalar(select(Supplier).where(Supplier.id != sup)).id
        s.add(PurchaseOrder(po_number="PO-X", supplier_id=other))
        s.commit()
        po_id = s.scalar(select(PurchaseOrder.id))
    login(c, "store")
    c.post("/deliveries/new", data={"supplier_id": sup, "po_id": po_id,
                                    f"qty_{m['Blade']}": "5"})
    with Session(c.engine) as s:
        from models import DeliveryNote
        assert s.scalar(select(DeliveryNote)).po_id is None


def test_photo_reader_maps_lines(c):
    sup, m = ids(c)
    c.app.config["AI_CLIENT"] = FakeClient(
        '{"number":"81231","date":"2026-10-19","lines":['
        '{"name":"DEFORMED BARS D12MM","qty":32,"returned":0,"price":1080},'
        '{"name":"HOOP IRON","qty":8,"returned":8,"price":1400},'
        '{"name":"MYSTERY ITEM","qty":1,"returned":0,"price":5}]}')
    login(c, "fin")
    r = c.post("/api/read-photo", data={"kind": "invoice", "supplier_id": sup,
                                        "photo": (io.BytesIO(png()), "a.png")})
    j = r.get_json()
    assert r.status_code == 200, j
    got = {l["material_id"]: l for l in j["lines"]}
    assert got[m["Deformed bar D12 mm"]]["qty"] == 32
    assert got[m["Hoop iron 17 kg"]]["returned"] == 8
    assert j["unmatched"] == ["MYSTERY ITEM"] and j["number"] == "81231"


def test_photo_reader_rules(c):
    sup, _ = ids(c)
    login(c, "store")
    f = lambda: {"kind": "invoice", "supplier_id": sup, "photo": (io.BytesIO(png()), "a.png")}
    assert c.post("/api/read-photo", data=f()).status_code == 403      # storekeeper, invoice
    login(c, "proc")
    assert c.post("/api/read-photo", data=f()).status_code == 403      # procurement reads nothing
    d = {"kind": "delivery", "supplier_id": sup, "photo": (io.BytesIO(png()), "a.png")}
    assert c.post("/api/read-photo", data=d).status_code == 403
    login(c, "fin")
    bad = {"kind": "invoice", "supplier_id": sup, "photo": (io.BytesIO(b"nope"), "a.png")}
    assert c.post("/api/read-photo", data=bad).status_code == 400
    c.app.config.pop("AI_CLIENT", None)
    r = c.post("/api/read-photo", data=f())                            # no API key
    assert r.status_code == 422 and "not set up" in r.get_json()["error"]


def test_photo_reader_never_leaks_exception_text(c, monkeypatch):
    sup, _ = ids(c)

    class Boom:
        messages = None

        def __init__(self):
            self.messages = self

        def create(self, **kw):
            raise RuntimeError("sk-secret-123")
    c.app.config["AI_CLIENT"] = Boom()
    login(c, "fin")
    r = c.post("/api/read-photo", data={"kind": "invoice", "supplier_id": sup,
                                        "photo": (io.BytesIO(png()), "a.png")})
    assert "sk-secret" not in r.get_data(as_text=True)


def test_entry_forms_open_with_a_supplier_chosen(c):
    sup, _ = ids(c)
    for who, path in (("store", "/deliveries/new"), ("fin", "/invoices/new"),
                      ("proc", "/orders/new")):
        login(c, who)
        r = c.get(f"{path}?supplier={sup}")
        assert r.status_code == 200, path
    assert b"Read photo" in c.get(f"/invoices/new?supplier={sup}").data if login(c, "fin") else 0


def test_dashboard_has_no_python_leaks_and_no_cards(c):
    for who in ("admin", "store", "proc", "fin"):
        login(c, who)
        page = c.get("/").get_data(as_text=True)
        assert "built-in method" not in page
        assert 'class="stat' not in page and 'class="tile' not in page


def test_dashboard_tables_show_real_rows(c):
    sup, m = ids(c)
    hoop = m["Hoop iron 17 kg"]
    login(c, "proc")
    c.post("/orders/new", data={"supplier_id": sup, f"qty_{hoop}": "8"})
    login(c, "store")
    c.post("/deliveries/new", data={"supplier_id": sup, "po_id": 1, "note_number": "DN-77",
                                    f"qty_{hoop}": "8", f"ret_{hoop}": "8"})
    login(c, "fin")
    c.post("/invoices/new", data={"supplier_id": sup, "po_id": 1, "invoice_number": "81231",
                                  f"qty_{hoop}": "8", f"price_{hoop}": "1400"})
    page = c.get("/").get_data(as_text=True)
    assert "81231" in page and "11,200.00" in page and "On hold" in page
    assert "must not be paid yet" in page                       # plain-language to-do
    assert "DN-77" in page and "Goods returned" in page
    # search and filters
    assert "81231" in recent(c.get("/?q=hekiwa"))
    assert "81231" not in recent(c.get("/?q=zzzz"))
    assert "81231" in recent(c.get("/?show=hold"))
    assert "81231" not in recent(c.get("/?show=clear"))
    assert "81231" in recent(c.get("/?q=81231"))
    c.get("/?q=%25%25")                                    # wildcard text must not break it
    # procurement follows the order until it arrives
    login(c, "proc")
    assert "PO-" in c.get("/").get_data(as_text=True)
    # storekeeper: deliveries only, and a plain empty message
    login(c, "store")
    page = c.get("/").get_data(as_text=True)
    assert "DN-77" in page and "81231" not in page and "Nothing is waiting" in page
    
def test_nobody_can_order_receive_and_bill_the_same_goods(c):
    sup, m = ids(c)
    blade = m["Blade"]
    login(c, "proc")
    assert c.post("/deliveries/new", data={"supplier_id": sup, f"qty_{blade}": "5"}).status_code == 403
    assert c.post("/invoices/new", data={"supplier_id": sup, "invoice_number": "X1",
                                         f"qty_{blade}": "5", f"price_{blade}": "110"}).status_code == 403
    assert c.post("/invoices/1/recheck").status_code == 403
    login(c, "fin")
    assert c.post("/orders/new", data={"supplier_id": sup, f"qty_{blade}": "5"}).status_code == 403
    assert c.post("/deliveries/new", data={"supplier_id": sup, f"qty_{blade}": "5"}).status_code == 403
    login(c, "store")
    assert c.post("/orders/new", data={"supplier_id": sup, f"qty_{blade}": "5"}).status_code == 403
    assert c.post("/invoices/1/approve").status_code == 403
    page = ""
    for who, hidden in (("proc", ("Record delivery", "Enter invoice")),
                        ("fin", ("New order", "Record delivery")),
                        ("store", ("New order", "Enter invoice"))):
        login(c, who)
        page = c.get("/").get_data(as_text=True)
        assert not any(label in page for label in hidden), (who, hidden)


def test_photo_without_items_says_what_to_do(c):
    sup, _ = ids(c)
    login(c, "store")
    r = c.post("/deliveries/new", data={"supplier_id": sup, "photo": (io.BytesIO(png()), "a.png")},
               follow_redirects=True)
    assert b"Press Read photo" in r.data
    r = c.post("/deliveries/new", data={"supplier_id": sup}, follow_redirects=True)
    assert b"at least one item" in r.data and b"Press Read photo" not in r.data
    login(c, "fin")
    r = c.post("/invoices/new", data={"supplier_id": sup, "invoice_number": "P1",
                                      "photo": (io.BytesIO(png()), "a.png")}, follow_redirects=True)
    assert b"Press Read photo" in r.data


def test_number_boxes_accept_any_decimal_and_step_by_one(c):
    sup, _ = ids(c)
    login(c, "fin")
    page = c.get(f"/invoices/new?supplier={sup}").get_data(as_text=True)
    assert 'step="any"' in page and 'step="0.01"' not in page
    login(c, "admin")
    assert 'step="any"' in c.get(f"/admin/prices?supplier={sup}").get_data(as_text=True)


def test_all_photo_prices_are_loaded_and_every_name_exists(c):
    with Session(c.engine) as s:
        missing = [(sup, mat) for sup, mat, _, _ in load_prices.PRICES
                   if s.scalar(select(Supplier.id).where(Supplier.name == sup)) is None
                   or s.scalar(select(Material.id).where(Material.name == mat)) is None]
        assert missing == []
        priced = s.scalar(select(func.count(func.distinct(SupplierPrice.id))).where(
            SupplierPrice.unit_price.is_not(None)))
        assert priced == len(load_prices.PRICES)
        assert s.scalar(select(Supplier).where(Supplier.name.like("Irrigation%"))) is not None


def test_labour_invoice_needs_site_certification_before_payment(c):
    data = {"contractor": "Elijah Kinyua", "description": "1st floor floor finishing, 14 units",
            "amount": "93000", "invoice_number": "40"}
    for who in ("store", "proc"):
        login(c, who)
        assert c.post("/labour/new", data=data).status_code == 403
    login(c, "fin")
    r = c.post("/labour/new", data=data, follow_redirects=True)
    assert b"Waiting for site certification" in r.data
    assert b"already been entered" in c.post("/labour/new", data=data, follow_redirects=True).data
    dup = dict(data, invoice_number="41")
    assert b"duplicate" in c.post("/labour/new", data=dup, follow_redirects=True).data
    # finance cannot approve before the site certifies
    c.post("/labour/1/approve")
    assert b"Waiting for site" in c.get("/labour/1").data
    assert c.post("/labour/1/certify").status_code == 403          # finance cannot certify
    login(c, "proc")
    assert c.get("/labour").status_code == 200 and c.post("/labour/1/certify").status_code == 403
    login(c, "store")
    assert c.post("/labour/1/approve").status_code == 403
    c.post("/labour/1/certify")
    login(c, "fin")
    c.post("/labour/1/paid")                                        # not approved yet
    assert b"Work certified" in c.get("/labour/1").data
    c.post("/labour/1/approve")
    c.post("/labour/1/paid")
    page = c.get("/labour").get_data(as_text=True)
    assert "Paid" in page and "93,000.00" in page
    assert "Labour invoices" in c.get("/invoices").get_data(as_text=True)