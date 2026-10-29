"""Database models for the Invoice & Delivery Note Matching System.

Uses SQLAlchemy so we can develop on SQLite and deploy on MySQL by only
changing the DATABASE_URL in config.py.
"""
from datetime import datetime, date

from sqlalchemy import (
    Column, Integer, String, Numeric, Date, DateTime, ForeignKey,
    Boolean, Text, UniqueConstraint,
)
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False)
    email = Column(String(120), unique=True, nullable=False)
    password_hash = Column(String(255), nullable=False)
    # storekeeper, procurement, finance, admin
    role = Column(String(20), nullable=False, default="storekeeper")
    active = Column(Boolean, default=True)


class Supplier(Base):
    __tablename__ = "suppliers"
    id = Column(Integer, primary_key=True)
    name = Column(String(150), unique=True, nullable=False)
    active = Column(Boolean, default=True)
    prices = relationship("SupplierPrice", back_populates="supplier")


class Material(Base):
    """One record per item AND size, e.g. 'PVC pipe 3"' and 'PVC pipe 4"'
    are two different materials."""
    __tablename__ = "materials"
    id = Column(Integer, primary_key=True)
    name = Column(String(150), unique=True, nullable=False)
    unit = Column(String(30), nullable=False)  # bag, piece, truckload, roll...
    active = Column(Boolean, default=True)


class SupplierPrice(Base):
    """Price history. Never overwrite: add a new row with a new effective_from.
    The price that applies is the latest row with effective_from <= order date."""
    __tablename__ = "supplier_prices"
    id = Column(Integer, primary_key=True)
    supplier_id = Column(ForeignKey("suppliers.id"), nullable=False)
    material_id = Column(ForeignKey("materials.id"), nullable=False)
    unit_price = Column(Numeric(12, 2), nullable=True)  # None until admin sets it
    effective_from = Column(Date, nullable=False, default=date.today)
    changed_by = Column(ForeignKey("users.id"), nullable=True)
    supplier = relationship("Supplier", back_populates="prices")
    material = relationship("Material")


class Project(Base):
    __tablename__ = "projects"
    id = Column(Integer, primary_key=True)
    name = Column(String(150), nullable=False)
    buildings = relationship("Building", back_populates="project")


class Building(Base):
    __tablename__ = "buildings"
    id = Column(Integer, primary_key=True)
    project_id = Column(ForeignKey("projects.id"), nullable=False)
    name = Column(String(100), nullable=False)
    project = relationship("Project", back_populates="buildings")


# Stages: foundation, typical_floor, top_floor, finishing, balcony
class PurchaseOrder(Base):
    __tablename__ = "purchase_orders"
    id = Column(Integer, primary_key=True)
    po_number = Column(String(50), unique=True, nullable=False)
    supplier_id = Column(ForeignKey("suppliers.id"), nullable=False)
    building_id = Column(ForeignKey("buildings.id"), nullable=True)
    stage = Column(String(30), nullable=True)
    floor = Column(String(30), nullable=True)
    order_date = Column(Date, default=date.today)
    status = Column(String(20), default="open")  # open, closed
    created_by = Column(ForeignKey("users.id"), nullable=True)
    lines = relationship("POLine", back_populates="po")


class POLine(Base):
    __tablename__ = "po_lines"
    id = Column(Integer, primary_key=True)
    po_id = Column(ForeignKey("purchase_orders.id"), nullable=False)
    material_id = Column(ForeignKey("materials.id"), nullable=False)
    quantity = Column(Numeric(12, 2), nullable=False)  # a quantity is mandatory
    agreed_price = Column(Numeric(12, 2), nullable=True)
    po = relationship("PurchaseOrder", back_populates="lines")
    material = relationship("Material")


class DeliveryNote(Base):
    """What physically arrived. Linked to the supplier (always) and to a
    purchase order only when one exists, because many suppliers' paperwork
    carries no LPO number."""
    __tablename__ = "delivery_notes"
    id = Column(Integer, primary_key=True)
    supplier_id = Column(ForeignKey("suppliers.id"), nullable=False)
    po_id = Column(ForeignKey("purchase_orders.id"), nullable=True)
    note_number = Column(String(50), nullable=True)
    delivery_date = Column(Date, default=date.today)
    received_by = Column(ForeignKey("users.id"), nullable=True)
    photo_path = Column(String(255), nullable=True)
    lines = relationship("DeliveryLine", back_populates="note")


class DeliveryLine(Base):
    __tablename__ = "delivery_lines"
    id = Column(Integer, primary_key=True)
    note_id = Column(ForeignKey("delivery_notes.id"), nullable=False)
    material_id = Column(ForeignKey("materials.id"), nullable=False)
    quantity = Column(Numeric(12, 2), nullable=False)       # arrived on site
    returned_qty = Column(Numeric(12, 2), nullable=False, default=0)  # sent back
    condition = Column(String(20), default="good")  # good, damaged
    note = relationship("DeliveryNote", back_populates="lines")
    material = relationship("Material")


class DeliveryAllocation(Base):
    """Records which invoice has already been paid/matched against which
    delivery quantity, so one delivery can never be billed twice."""
    __tablename__ = "delivery_allocations"
    id = Column(Integer, primary_key=True)
    invoice_id = Column(ForeignKey("invoices.id"), nullable=False)
    delivery_line_id = Column(ForeignKey("delivery_lines.id"), nullable=False)
    quantity = Column(Numeric(12, 2), nullable=False)


class SupplierAlias(Base):
    """How a supplier writes a material on its paperwork,
    e.g. Hekiwa's 'HOOP IRON' -> our 'Hoop iron 17 kg'. Stored normalised."""
    __tablename__ = "supplier_aliases"
    __table_args__ = (UniqueConstraint("supplier_id", "alias"),)
    id = Column(Integer, primary_key=True)
    supplier_id = Column(ForeignKey("suppliers.id"), nullable=False)
    material_id = Column(ForeignKey("materials.id"), nullable=False)
    alias = Column(String(150), nullable=False)


class Invoice(Base):
    __tablename__ = "invoices"
    __table_args__ = (UniqueConstraint("supplier_id", "invoice_number"),)
    id = Column(Integer, primary_key=True)
    supplier_id = Column(ForeignKey("suppliers.id"), nullable=False)
    po_id = Column(ForeignKey("purchase_orders.id"), nullable=True)
    invoice_number = Column(String(50), nullable=False)
    invoice_date = Column(Date, default=date.today)
    payment_status = Column(String(20), default="unpaid")  # unpaid, approved, paid
    extracted_by_ai = Column(Boolean, default=False)
    photo_path = Column(String(255), nullable=True)
    lines = relationship("InvoiceLine", back_populates="invoice")


class InvoiceLine(Base):
    __tablename__ = "invoice_lines"
    id = Column(Integer, primary_key=True)
    invoice_id = Column(ForeignKey("invoices.id"), nullable=False)
    material_id = Column(ForeignKey("materials.id"), nullable=False)
    quantity = Column(Numeric(12, 2), nullable=False)
    unit_price = Column(Numeric(12, 2), nullable=True)
    invoice = relationship("Invoice", back_populates="lines")
    material = relationship("Material")


class MatchResult(Base):
    __tablename__ = "match_results"
    id = Column(Integer, primary_key=True)
    invoice_id = Column(ForeignKey("invoices.id"), nullable=False)
    material_id = Column(ForeignKey("materials.id"), nullable=False)
    ordered_qty = Column(Numeric(12, 2))
    delivered_qty = Column(Numeric(12, 2))
    invoiced_qty = Column(Numeric(12, 2))
    returned_qty = Column(Numeric(12, 2))
    unit_price = Column(Numeric(12, 2))
    amount_blocked = Column(Numeric(14, 2), default=0)  # value that must not be paid
    status = Column(String(30), nullable=False)
    reason = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)


class AuditLog(Base):
    __tablename__ = "audit_log"
    id = Column(Integer, primary_key=True)
    user_id = Column(ForeignKey("users.id"), nullable=True)
    action = Column(String(100), nullable=False)
    details = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)