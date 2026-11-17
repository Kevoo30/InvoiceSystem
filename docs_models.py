"""Tables for delivery notes, supplier invoices and supplier-specific item names.

Imported by app.py BEFORE Base.metadata.create_all(), so the tables are created
automatically the next time the site starts. Table names start with dm_ so they
cannot clash with tables you already have in models.py.
"""
from datetime import datetime

from sqlalchemy import (Column, Date, DateTime, ForeignKey, Integer, Numeric,
                        String, UniqueConstraint)
from sqlalchemy.orm import relationship

from models import Base, Material, Supplier, User


def _fk(model):
    # Uses the real table name from models.py, whatever it is called.
    return ForeignKey(f"{model.__tablename__}.id")


class DocAlias(Base):
    """How one supplier writes a material on paper -> your material."""
    __tablename__ = "dm_aliases"
    __table_args__ = (UniqueConstraint("supplier_id", "alias"),)
    id = Column(Integer, primary_key=True)
    supplier_id = Column(Integer, _fk(Supplier), nullable=False)
    material_id = Column(Integer, _fk(Material), nullable=False)
    alias = Column(String(200), nullable=False)


class DocDelivery(Base):
    __tablename__ = "dm_deliveries"
    id = Column(Integer, primary_key=True)
    supplier_id = Column(Integer, _fk(Supplier), nullable=False)
    note_no = Column(String(60), nullable=False, default="")
    delivery_date = Column(Date, nullable=False)
    created_by = Column(Integer, _fk(User), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    image_path = Column(String(80), nullable=False, default="")
    supplier = relationship(Supplier)
    lines = relationship("DocDeliveryLine", cascade="all, delete-orphan",
                         back_populates="delivery", order_by="DocDeliveryLine.id")


class DocDeliveryLine(Base):
    __tablename__ = "dm_delivery_lines"
    id = Column(Integer, primary_key=True)
    delivery_id = Column(Integer, ForeignKey("dm_deliveries.id"), nullable=False)
    material_id = Column(Integer, _fk(Material), nullable=False)
    written_name = Column(String(200), nullable=False, default="")
    qty_delivered = Column(Numeric(12, 2), nullable=False)
    qty_returned = Column(Numeric(12, 2), nullable=False, default=0)
    delivery = relationship("DocDelivery", back_populates="lines")
    material = relationship(Material)


class DocInvoice(Base):
    __tablename__ = "dm_invoices"
    id = Column(Integer, primary_key=True)
    supplier_id = Column(Integer, _fk(Supplier), nullable=False)
    invoice_no = Column(String(60), nullable=False, default="")
    invoice_date = Column(Date, nullable=False)
    stated_total = Column(Numeric(14, 2), nullable=True)
    created_by = Column(Integer, _fk(User), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    image_path = Column(String(80), nullable=False, default="")
    supplier = relationship(Supplier)
    lines = relationship("DocInvoiceLine", cascade="all, delete-orphan",
                         back_populates="invoice", order_by="DocInvoiceLine.id")


class DocInvoiceLine(Base):
    __tablename__ = "dm_invoice_lines"
    id = Column(Integer, primary_key=True)
    invoice_id = Column(Integer, ForeignKey("dm_invoices.id"), nullable=False)
    material_id = Column(Integer, _fk(Material), nullable=False)
    written_name = Column(String(200), nullable=False, default="")
    qty = Column(Numeric(12, 2), nullable=False)
    unit_price = Column(Numeric(12, 2), nullable=False)
    invoice = relationship("DocInvoice", back_populates="lines")
    material = relationship(Material)