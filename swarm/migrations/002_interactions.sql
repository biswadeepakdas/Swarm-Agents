-- Agent-User interaction table
-- Agents can pause and ask the user clarifying questions.
-- The user responds via the dashboard, and the agent resumes.

CREATE TABLE IF NOT EXISTS interactions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id UUID REFERENCES projects(id) ON DELETE CASCADE,
    task_id TEXT REFERENCES tasks(id) ON DELETE SET NULL,
    agent_id TEXT REFERENCES agents(id) ON DELETE SET NULL,
    question TEXT NOT NULL,
    options TEXT[] DEFAULT '{}',         -- optional multiple-choice options
    context TEXT DEFAULT '',             -- why the agent is asking
    response TEXT,                       -- user's answer (NULL until answered)
    status TEXT DEFAULT 'pending',       -- pending, answered, expired, cancelled
    created_at TIMESTAMPTZ DEFAULT now(),
    answered_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_interactions_project ON interactions (project_id, status);
CREATE INDEX IF NOT EXISTS idx_interactions_task ON interactions (task_id);
