"""Create the first admin user. Run once:  python create_admin.py"""
import getpass

from dotenv import load_dotenv

load_dotenv()

from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402
from werkzeug.security import generate_password_hash  # noqa: E402

import config  # noqa: E402
from models import Base, User  # noqa: E402


def main():
    name = input("Full name: ").strip()
    email = input("Email (used to log in): ").strip().lower()
    password = getpass.getpass("Password (min 8 characters): ")
    if not name or "@" not in email or len(password) < 8:
        raise SystemExit("Invalid name, email or password (min 8 characters).")

    engine = create_engine(config.DATABASE_URL)
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        if s.scalar(select(User).where(User.email == email)):
            raise SystemExit("That email already exists.")
        s.add(User(name=name, email=email, role="admin",
                   password_hash=generate_password_hash(password)))
        s.commit()
    print("Admin created. Now run: python app.py")


if __name__ == "__main__":
    main()