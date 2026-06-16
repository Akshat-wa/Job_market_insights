-- Deploy schema: session-scoped jobs, global canonical skills.
-- Safe to run multiple times (IF NOT EXISTS / conditional alters).

-- Bootstrap core tables on empty Neon DB (minimal; run jobs_db.py for full load)
CREATE TABLE IF NOT EXISTS jobs(
    job_id      SERIAL PRIMARY KEY,
    session_id  TEXT NOT NULL DEFAULT 'demo',
    job_link    TEXT,
    title       TEXT,
    company     TEXT,
    location    TEXT,
    title_lc    TEXT,
    location_lc TEXT,
    country     TEXT,
    country_lc  TEXT
);

CREATE TABLE IF NOT EXISTS skills(
    skill_id  SERIAL PRIMARY KEY,
    name      TEXT UNIQUE,
    name_lc   TEXT UNIQUE
);

CREATE TABLE IF NOT EXISTS job_skills(
    session_id TEXT NOT NULL DEFAULT 'demo',
    job_id     INTEGER NOT NULL,
    skill_id   INTEGER NOT NULL,
    PRIMARY KEY (session_id, job_id, skill_id)
);

-- Jobs: add session_id if migrating existing DB
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'jobs' AND column_name = 'session_id'
    ) THEN
        ALTER TABLE jobs ADD COLUMN session_id TEXT NOT NULL DEFAULT 'demo';
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'job_skills' AND column_name = 'session_id'
    ) THEN
        ALTER TABLE job_skills ADD COLUMN session_id TEXT NOT NULL DEFAULT 'demo';
    END IF;
END $$;

-- Replace global job_link unique with per-session unique
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'jobs_job_link_key'
    ) THEN
        ALTER TABLE jobs DROP CONSTRAINT jobs_job_link_key;
    END IF;
EXCEPTION WHEN OTHERS THEN NULL;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_session_link
    ON jobs(session_id, job_link);

CREATE INDEX IF NOT EXISTS idx_jobs_session_id ON jobs(session_id);
CREATE INDEX IF NOT EXISTS idx_job_skills_session ON job_skills(session_id);

-- Extend job_skills PK to include session_id
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'job_skills_pkey' AND contype = 'p'
    ) THEN
        ALTER TABLE job_skills DROP CONSTRAINT job_skills_pkey;
        ALTER TABLE job_skills ADD PRIMARY KEY (session_id, job_id, skill_id);
    END IF;
EXCEPTION WHEN OTHERS THEN
    BEGIN
        ALTER TABLE job_skills ADD PRIMARY KEY (session_id, job_id, skill_id);
    EXCEPTION WHEN OTHERS THEN NULL;
    END;
END $$;

-- Session metadata (optional bookkeeping / TTL cleanup later)
CREATE TABLE IF NOT EXISTS upload_sessions (
    session_id TEXT PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_used_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    label TEXT
);

CREATE INDEX IF NOT EXISTS idx_upload_sessions_created ON upload_sessions(created_at);
