-- Enable pgvector extension
CREATE EXTENSION IF NOT EXISTS vector;

-- Projects
CREATE TABLE IF NOT EXISTS projects (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL,
    brief TEXT NOT NULL,
    status TEXT DEFAULT 'active',
    config JSONB DEFAULT '{}',
    total_tokens_used BIGINT DEFAULT 0,
    total_cost NUMERIC(10, 4) DEFAULT 0.0,
    agent_count INT DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT now()
);

-- Tasks
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    project_id UUID REFERENCES projects(id) ON DELETE CASCADE,
    type TEXT NOT NULL,
    payload JSONB DEFAULT '{}',
    priority INT DEFAULT 2,
    status TEXT DEFAULT 'pending',
    parent_task_id TEXT REFERENCES tasks(id) ON DELETE SET NULL,
    spawned_by_agent_id TEXT,
    assigned_agent_id TEXT,
    result JSONB,
    error TEXT,
    retry_count INT DEFAULT 0,
    dependencies TEXT[] DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT now(),
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ
);

-- Agents
CREATE TABLE IF NOT EXISTS agents (
    id TEXT PRIMARY KEY,
    project_id UUID REFERENCES projects(id) ON DELETE CASCADE,
    task_id TEXT REFERENCES tasks(id) ON DELETE SET NULL,
    persona TEXT NOT NULL,
    role TEXT NOT NULL,
    name TEXT NOT NULL,
    personality JSONB DEFAULT '{}',
    status TEXT DEFAULT 'alive',
    created_at TIMESTAMPTZ DEFAULT now(),
    died_at TIMESTAMPTZ
);

-- Artifacts
CREATE TABLE IF NOT EXISTS artifacts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id UUID REFERENCES projects(id) ON DELETE CASCADE,
    task_id TEXT REFERENCES tasks(id) ON DELETE SET NULL,
    agent_id TEXT REFERENCES agents(id) ON DELETE SET NULL,
    type TEXT NOT NULL,
    name TEXT NOT NULL,
    content TEXT NOT NULL,
    tags TEXT[] DEFAULT '{}',
    dependencies UUID[] DEFAULT '{}',
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT now()
);

-- Long-term memories with vector embeddings
CREATE TABLE IF NOT EXISTS memories (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id UUID REFERENCES projects(id) ON DELETE CASCADE,
    agent_id TEXT,
    content TEXT NOT NULL,
    tags TEXT[] DEFAULT '{}',
    embedding vector(384),
    created_at TIMESTAMPTZ DEFAULT now()
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_memories_embedding ON memories USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
CREATE INDEX IF NOT EXISTS idx_artifacts_tags ON artifacts USING gin (tags);
CREATE INDEX IF NOT EXISTS idx_artifacts_project ON artifacts (project_id, type);
CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks (project_id, status);
CREATE INDEX IF NOT EXISTS idx_tasks_parent ON tasks (parent_task_id);
CREATE INDEX IF NOT EXISTS idx_agents_project ON agents (project_id, status);
