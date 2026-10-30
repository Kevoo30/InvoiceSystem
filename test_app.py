import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from werkzeug.security import generate_password_hash

from app import create_app
from models import AuditLog, Material, Supplier, SupplierPrice, User
from seed import seed


@pytest.fixture
def client(tmp_path):
    url = f"sqlite:///{tmp_path/'t.db'}"
    app = create_app(url, testing=True)
    engine = create_engine(url)
    with Session(engine) as s:
        seed(s)
        for name, email, role in [("Ann Admin", "admin@x.com", "admin"),
                                  ("Sam Store", "store@x.com", "storekeeper")]:
            s.add(User(name=name, email=email, role=role,
                       password_hash=generate_password_hash("password123")))
        s.commit()
    c = app.test_client()
    c.engine = engine
    return c


def login(c, email, password="password123"):
    return c.post("/login", data={"email": email, "password": password},
                  follow_redirects=True)


def ids(c, supplier, material):
    with Session(c.engine) as s:
        sup = s.scalar(select(Supplier).where(Supplier.name == supplier))
        mat = s.scalar(select(Material).where(Material.name == material))
        return sup.id, mat.id


def test_login_required(client):
    r = client.get("/admin/prices")
    assert r.status_code == 302 and "/login" in r.headers["Location"]


def test_wrong_password(client):
    r = login(client, "admin@x.com", "wrongpass")
    assert b"Wrong email or password" in r.data


def test_admin_login_and_pages(client):
    assert b"Ann Admin" in login(client, "admin@x.com").data
    for path in ["/admin/prices", "/admin/suppliers", "/admin/users"]:
        assert client.get(path).status_code == 200


def test_storekeeper_cannot_open_admin(client):
    login(client, "store@x.com")
    assert client.get("/").status_code == 200
    for path in ["/admin/prices", "/admin/suppliers", "/admin/users"]:
        assert client.get(path).status_code == 403
    sid, mid = ids(client, "Genesis Chuma Ltd", "Bucket")
    assert client.post(f"/admin/prices/{sid}/{mid}", data={"price": "100"}).status_code == 403


def test_disabled_user_cannot_login(client):
    with Session(client.engine) as s:
        u = s.scalar(select(User).where(User.email == "store@x.com"))
        u.active = False
        s.commit()
    assert b"Wrong email or password" in login(client, "store@x.com").data


def test_price_change_keeps_history(client):
    login(client, "admin@x.com")
    sid, mid = ids(client, "Garden Vineyard Hardware & Electricals", "Tembo cement")
    # first price dated in the past, then a newer one
    client.post(f"/admin/prices/{sid}/{mid}",
                data={"price": "750", "effective_from": "2026-10-01"})
    assert b"750.00" in client.get("/admin/prices").data
    client.post(f"/admin/prices/{sid}/{mid}",
                data={"price": "780.50", "effective_from": "2026-10-20"})
    page = client.get("/admin/prices").data
    assert b"780.50" in page
    with Session(client.engine) as s:
        rows = s.scalars(select(SupplierPrice).where(
            SupplierPrice.supplier_id == sid, SupplierPrice.material_id == mid)).all()
        assert len(rows) == 3                      # initial empty row + 2 changes
        assert sorted(str(r.unit_price) for r in rows if r.unit_price) == ["750.00", "780.50"]
        assert s.scalar(select(AuditLog).where(AuditLog.action == "price_change"))
    hist = client.get(f"/admin/prices/{sid}/{mid}/history").data
    assert b"750.00" in hist and b"780.50" in hist


def test_future_dated_price_not_current_yet(client):
    login(client, "admin@x.com")
    sid, mid = ids(client, "Genesis Chuma Ltd", "Bucket")
    client.post(f"/admin/prices/{sid}/{mid}",
                data={"price": "200", "effective_from": "2026-01-01"})
    client.post(f"/admin/prices/{sid}/{mid}",
                data={"price": "999", "effective_from": "2099-01-01"})
    page = client.get(f"/admin/prices?supplier={sid}").data
    assert b"200.00" in page
    assert b"999.00" not in page.split(b"<tbody>")[1]   # table only, not the flash message


def test_invalid_price_rejected(client):
    login(client, "admin@x.com")
    sid, mid = ids(client, "Genesis Chuma Ltd", "Bucket")
    for bad in ["", "abc", "-5", "0"]:
        client.post(f"/admin/prices/{sid}/{mid}", data={"price": bad})
    with Session(client.engine) as s:
        n = len(s.scalars(select(SupplierPrice).where(
            SupplierPrice.supplier_id == sid, SupplierPrice.material_id == mid)).all())
        assert n == 1


def test_add_supplier_with_new_and_existing_material(client):
    login(client, "admin@x.com")
    r = client.post("/admin/suppliers/new", data={"name": "New Partner Ltd"})
    assert r.status_code == 302
    with Session(client.engine) as s:
        sup = s.scalar(select(Supplier).where(Supplier.name == "New Partner Ltd"))
        sid = sup.id
        bucket = s.scalar(select(Material).where(Material.name == "Bucket")).id
    # existing material
    client.post(f"/admin/suppliers/{sid}/materials", data={"material_id": bucket, "price": "250"})
    # brand-new material
    client.post(f"/admin/suppliers/{sid}/materials",
                data={"new_name": 'PVC pipe 2"', "unit": "piece", "price": "900"})
    # duplicate is refused
    client.post(f"/admin/suppliers/{sid}/materials", data={"material_id": bucket})
    with Session(client.engine) as s:
        links = s.scalars(select(SupplierPrice).where(SupplierPrice.supplier_id == sid)).all()
        assert len(links) == 2
        assert s.scalar(select(Material).where(Material.name == 'PVC pipe 2"'))


def test_duplicate_supplier_refused(client):
    login(client, "admin@x.com")
    r = client.post("/admin/suppliers/new", data={"name": "Genesis Chuma Ltd"})
    assert r.status_code == 200 and b"already exists" in r.data


def test_create_user_and_short_password(client):
    login(client, "admin@x.com")
    client.post("/admin/users", data={"name": "Fin", "email": "fin@x.com",
                                      "role": "finance", "password": "short"})
    client.post("/admin/users", data={"name": "Fin", "email": "fin@x.com",
                                      "role": "finance", "password": "longenough1"})
    with Session(client.engine) as s:
        assert s.scalar(select(User).where(User.email == "fin@x.com")).role == "finance"


def test_logout(client):
    login(client, "admin@x.com")
    client.post("/logout")
    assert client.get("/admin/prices").status_code == 302


def test_open_redirect_blocked(client):
    r = client.post("/login?next=//evil.com",
                    data={"email": "admin@x.com", "password": "password123"})
    assert r.headers["Location"].endswith("/")