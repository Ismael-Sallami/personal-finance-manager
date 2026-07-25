"""Create the tables (if you do not use Alembic) and the single user from .env.

Usage:  python -m scripts.seed
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app.config import settings  # noqa: E402
from app.db import Base, SessionLocal, engine  # noqa: E402
from app.models import User  # noqa: E402
from app.security import hash_password  # noqa: E402


def main() -> None:
    print(f"DB: {'SQLite (dev)' if settings.is_sqlite else 'Postgres'}")
    Base.metadata.create_all(engine)
    print("Tables created or already present.")

    email = settings.app_user_email.strip().lower()
    with SessionLocal() as db:
        existing = db.scalar(select(User).where(User.email == email))
        if existing:
            print(f"User already exists: {email}")
            return
        user = User(email=email, password_hash=hash_password(settings.app_user_password))
        db.add(user)
        db.commit()
        print(f"User created: {email}")


if __name__ == "__main__":
    main()
