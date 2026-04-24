"""
One-shot admin password reset utility.
Run from: ~/Downloads/t212-cashguard/apps/api
  python ../../reset_password.py
"""
import asyncio
import os
import sys

# Load env vars from .env
env_path = os.path.join(os.path.dirname(__file__), ".env")
with open(env_path) as f:
    for line in f:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

# Must be set before importing app modules
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "apps", "api"))

from app.core.security import hash_password
from app.db.models import User
from app.db.session import AsyncSessionLocal
from sqlalchemy import select


async def reset():
    email = os.environ.get("ADMIN_EMAIL", "admin@localhost")
    password = os.environ.get("ADMIN_PASSWORD", "mero5564")

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User).where(User.email == email))
        user = result.scalar_one_or_none()
        if user:
            user.hashed_password = hash_password(password)
            await db.commit()
            print(f"✓ Password for {email} reset to value from .env ({password})")
        else:
            # User doesn't exist at all — create them
            import uuid
            user = User(
                id=uuid.uuid4(),
                email=email,
                hashed_password=hash_password(password),
                is_active=True,
                is_admin=True,
            )
            db.add(user)
            await db.commit()
            print(f"✓ Admin user {email} created with password from .env ({password})")


asyncio.run(reset())
