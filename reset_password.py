"""
One-shot admin password reset utility.
Run from: ~/Downloads/t212-cashguard/apps/api
  python ../../reset_password.py
"""

import asyncio
import os
import sys
import uuid
from pathlib import Path

# Load env vars from .env
root_dir = Path(__file__).resolve().parent
env_path = root_dir / ".env"
with env_path.open() as f:
    for line in f:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

# Must be set before importing app modules
sys.path.insert(0, str(root_dir / "apps" / "api"))


async def reset():
    from sqlalchemy import select

    from app.core.security import hash_password
    from app.db.models import User
    from app.db.session import AsyncSessionLocal

    email = os.environ.get("ADMIN_EMAIL", "admin@localhost")
    password = os.environ.get("ADMIN_PASSWORD")
    if not password:
        raise RuntimeError("ADMIN_PASSWORD must be set in the environment or .env before reset")

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User).where(User.email == email))
        user = result.scalar_one_or_none()
        if user:
            user.hashed_password = hash_password(password)
            await db.commit()
            print(f"✓ Admin credential for {email} reset successfully")
        else:
            # User doesn't exist at all — create them
            user = User(
                id=uuid.uuid4(),
                email=email,
                hashed_password=hash_password(password),
                is_active=True,
                is_admin=True,
            )
            db.add(user)
            await db.commit()
            print(f"✓ Admin user {email} created with credential from .env")


asyncio.run(reset())
