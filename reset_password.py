"""Set a new login password for a user. Run:  python reset_password.py"""
import getpass

from dotenv import load_dotenv

load_dotenv()

from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402
from werkzeug.security import generate_password_hash  # noqa: E402

import config  # noqa: E402
from models import User  # noqa: E402


def main():
    engine = create_engine(config.DATABASE_URL)
    with Session(engine) as s:
        users = s.scalars(select(User).order_by(User.name)).all()
        print("Users:")
        for u in users:
            print(f"  {u.email}  ({u.role}){'' if u.active else '  [deactivated]'}")
        email = input("\nEmail of the user to reset: ").strip().lower()
        user = s.scalar(select(User).where(User.email == email))
        if user is None:
            raise SystemExit("No user with that email.")
        password = getpass.getpass("New password (min 8 characters): ")
        if len(password) < 8:
            raise SystemExit("The password must be at least 8 characters.")
        if password != getpass.getpass("Type it again: "):
            raise SystemExit("The two passwords do not match.")
        user.password_hash = generate_password_hash(password)
        s.commit()
        print(f"Done. {user.name} can now log in with the new password.")


if __name__ == "__main__":
    main()