import os

# Development: SQLite file on your laptop.
# Production: set DATABASE_URL to e.g.
#   mysql+pymysql://user:password@localhost/invoice_matcher
DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///invoice_matcher.db")

# Quantities within this fraction of ordered/delivered are treated as equal.
# 0 = must match exactly. Confirm acceptable tolerance with the company.
QUANTITY_TOLERANCE = 0.0
PRICE_TOLERANCE = 0.0