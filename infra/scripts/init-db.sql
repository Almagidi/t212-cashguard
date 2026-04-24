-- Run once when postgres container first starts.
-- Alembic handles the actual schema creation.

-- Enable UUID extension
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- Ensure the DB exists (docker handles this via POSTGRES_DB env var,
-- but this ensures extensions are available)
SELECT 1;
