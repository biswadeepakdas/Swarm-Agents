-- Scheduled tasks (cron-like recurring workflows)
CREATE TABLE IF NOT EXISTS scheduled_tasks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL,
    description TEXT DEFAULT '',
    project_id UUID REFERENCES projects(id) ON DELETE SET NULL,
    trigger_type TEXT DEFAULT 'cron',        -- 'cron', 'once', 'event'
    cron_expression TEXT DEFAULT '',          -- e.g., '0 9 * * 1' = Monday 9am
    workflow JSONB DEFAULT '{}',             -- task graph definition
    status TEXT DEFAULT 'active',            -- active, paused, completed, failed
    last_run_at TIMESTAMPTZ,
    next_run_at TIMESTAMPTZ,
    run_count INT DEFAULT 0,
    max_runs INT,                            -- NULL = unlimited
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_scheduled_status ON scheduled_tasks (status);

-- Skills / workflow templates
CREATE TABLE IF NOT EXISTS skills (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT DEFAULT '',
    category TEXT DEFAULT 'custom',           -- research, build, quality, monitoring, custom
    workflow JSONB DEFAULT '{}',             -- task definitions
    input_fields JSONB DEFAULT '[]',        -- schema for required inputs
    source_project_id UUID REFERENCES projects(id) ON DELETE SET NULL,
    usage_count INT DEFAULT 0,
    builtin BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_skills_category ON skills (category);

-- Council deliberation logs
CREATE TABLE IF NOT EXISTS council_sessions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id UUID REFERENCES projects(id) ON DELETE CASCADE,
    question TEXT NOT NULL,
    context TEXT DEFAULT '',
    votes JSONB DEFAULT '[]',               -- array of {model, content, latency_ms}
    synthesis TEXT DEFAULT '',
    agreement_score NUMERIC(3,2) DEFAULT 0,
    chosen_approach TEXT DEFAULT '',
    reasoning TEXT DEFAULT '',
    total_latency_ms INT DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_council_project ON council_sessions (project_id);

-- Project summaries (generated on completion)
ALTER TABLE projects ADD COLUMN IF NOT EXISTS summary TEXT DEFAULT '';
ALTER TABLE projects ADD COLUMN IF NOT EXISTS archived BOOLEAN DEFAULT FALSE;
ALTER TABLE projects ADD COLUMN IF NOT EXISTS completed_at TIMESTAMPTZ;
