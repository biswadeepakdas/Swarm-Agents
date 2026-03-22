/* ═══════════════════════════════════════════════════════════════
   SWARM CONTROL — Dashboard Application
   Connects to the Swarm API + WebSocket for real-time updates.
   ═══════════════════════════════════════════════════════════════ */

// Configurable backend URL for Vercel deployment
// Set window.SWARM_API_URL before loading this script, or it defaults to same-origin
const _backendOrigin = window.SWARM_API_URL || window.location.origin;
const API_BASE = _backendOrigin + '/api';
const _wsOrigin = _backendOrigin.replace(/^https:/, 'wss:').replace(/^http:/, 'ws:');
const WS_BASE = _wsOrigin + '/api';

// ── State ────────────────────────────────────────────────────

let state = {
  projects: [],
  activeProjectId: null,
  ws: null,
  tasks: [],
  agents: [],
  artifacts: [],
  feedItems: [],
};

// ── DOM refs ─────────────────────────────────────────────────

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => document.querySelectorAll(sel);

const dom = {
  connectionStatus: $('#connectionStatus'),
  statAgents: $('#statAgents'),
  statTasks: $('#statTasks'),
  statArtifacts: $('#statArtifacts'),
  btnNewProject: $('#btnNewProject'),
  newProjectForm: $('#newProjectForm'),
  inputProjectName: $('#inputProjectName'),
  inputProjectBrief: $('#inputProjectBrief'),
  btnCancelProject: $('#btnCancelProject'),
  btnLaunchSwarm: $('#btnLaunchSwarm'),
  projectList: $('#projectList'),
  activityFeed: $('#activityFeed'),
  tasksPending: $('#tasksPending'),
  tasksActive: $('#tasksActive'),
  tasksWaiting: $('#tasksWaiting'),
  tasksCompleted: $('#tasksCompleted'),
  countPending: $('#countPending'),
  countActive: $('#countActive'),
  countWaiting: $('#countWaiting'),
  countCompleted: $('#countCompleted'),
  agentGrid: $('#agentGrid'),
  artifactList: $('#artifactList'),
  artifactDetail: $('#artifactDetail'),
  detailName: $('#detailName'),
  detailMeta: $('#detailMeta'),
  detailContent: $('#detailContent'),
  btnCloseDetail: $('#btnCloseDetail'),
  btnRefreshArtifacts: $('#btnRefreshArtifacts'),
  inputInject: $('#inputInject'),
  btnInject: $('#btnInject'),
};

// ── Tabs ─────────────────────────────────────────────────────

$$('.tab').forEach(tab => {
  tab.addEventListener('click', () => {
    $$('.tab').forEach(t => t.classList.remove('active'));
    $$('.tab-content').forEach(tc => tc.classList.remove('active'));
    tab.classList.add('active');
    $(`#tab${capitalize(tab.dataset.tab)}`).classList.add('active');
  });
});

function capitalize(s) {
  return s.charAt(0).toUpperCase() + s.slice(1);
}

// ── Project form ─────────────────────────────────────────────

dom.btnNewProject.addEventListener('click', () => {
  dom.newProjectForm.classList.toggle('hidden');
  if (!dom.newProjectForm.classList.contains('hidden')) {
    dom.inputProjectName.focus();
  }
});

dom.btnCancelProject.addEventListener('click', () => {
  dom.newProjectForm.classList.add('hidden');
  dom.inputProjectName.value = '';
  dom.inputProjectBrief.value = '';
});

dom.btnLaunchSwarm.addEventListener('click', async () => {
  const name = dom.inputProjectName.value.trim();
  const brief = dom.inputProjectBrief.value.trim();
  if (!name || brief.length < 10) {
    shakeElement(dom.inputProjectBrief);
    showToast('Brief must be at least 10 characters.', 'warn');
    return;
  }

  dom.btnLaunchSwarm.disabled = true;
  dom.btnLaunchSwarm.textContent = 'Launching...';

  try {
    const res = await fetch(`${API_BASE}/projects`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name, brief }),
    });
    const data = await res.json();
    if (res.ok) {
      dom.newProjectForm.classList.add('hidden');
      dom.inputProjectName.value = '';
      dom.inputProjectBrief.value = '';
      addFeedItem('task', `Project <strong>${escapeHtml(name)}</strong> created. Swarm activated.`);
      showToast('Swarm launched. Agents are spawning.', 'ok');
      await loadProjects();
      selectProject(data.id);
    } else {
      addFeedItem('fail', `Failed to create project: ${data.detail || 'Unknown error'}`);
      showToast(`Launch failed: ${data.detail || 'Unknown'}`, 'error');
    }
  } catch (err) {
    addFeedItem('fail', `Network error: ${err.message}`);
    showToast(`Network error: ${err.message}`, 'error');
  } finally {
    dom.btnLaunchSwarm.disabled = false;
    dom.btnLaunchSwarm.textContent = 'Launch Swarm';
  }
});

// ── Inject requirement ───────────────────────────────────────

dom.btnInject.addEventListener('click', async () => {
  if (!state.activeProjectId) {
    showToast('Select a project first.', 'warn');
    return;
  }
  const text = dom.inputInject.value.trim();
  if (text.length < 5) {
    showToast('Requirement too short (min 5 chars).', 'warn');
    return;
  }

  dom.btnInject.disabled = true;
  dom.btnInject.textContent = 'Injecting...';

  try {
    const res = await fetch(`${API_BASE}/projects/${state.activeProjectId}/inject`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ requirement: text }),
    });
    if (res.ok) {
      addFeedItem('task', `Requirement injected: <strong>${escapeHtml(text.slice(0, 60))}...</strong>`);
      showToast('Requirement injected. Agent will spawn shortly.', 'ok');
      dom.inputInject.value = '';
      // Refresh data after short delay to let the spawn loop pick it up
      setTimeout(() => { if (state.activeProjectId) refreshProject(state.activeProjectId); }, 3000);
    } else {
      const data = await res.json().catch(() => ({}));
      showToast(`Inject failed: ${data.detail || res.statusText}`, 'error');
    }
  } catch (err) {
    addFeedItem('fail', `Inject failed: ${err.message}`);
    showToast(`Network error: ${err.message}`, 'error');
  } finally {
    dom.btnInject.disabled = false;
    dom.btnInject.textContent = 'Inject';
  }
});

// ── Load projects ────────────────────────────────────────────

async function loadProjects() {
  try {
    const res = await origFetch(`${API_BASE}/projects`);
    if (res.ok) {
      const data = await res.json();
      const projects = data.projects || [];
      projects.forEach(p => {
        addProjectToSidebar(p.id, p.name, p.brief || '', p.status || 'active');
      });
      if (projects.length > 0 && !state.activeProjectId) {
        selectProject(projects[0].id);
      }
    }
  } catch (err) {
    // Server might not be up — that's fine for static preview
  }
}

function selectProject(projectId) {
  state.activeProjectId = projectId;

  // Highlight in sidebar
  $$('.project-card').forEach(c => c.classList.remove('active'));
  const card = $(`.project-card[data-id="${projectId}"]`);
  if (card) card.classList.add('active');

  // Connect WebSocket
  connectWebSocket(projectId);

  // Fetch initial data
  refreshProject(projectId);

  // Load any pending agent questions
  loadPendingInteractions(projectId);
}

async function refreshProject(projectId) {
  try {
    const [projectRes, tasksRes, agentsRes, artifactsRes] = await Promise.all([
      fetch(`${API_BASE}/projects/${projectId}`),
      fetch(`${API_BASE}/projects/${projectId}/tasks`),
      fetch(`${API_BASE}/projects/${projectId}/agents`),
      fetch(`${API_BASE}/projects/${projectId}/artifacts`),
    ]);

    if (projectRes.ok) {
      const data = await projectRes.json();
      dom.statAgents.textContent = data.total_agents || 0;
      dom.statTasks.textContent = data.total_tasks || 0;
      dom.statArtifacts.textContent = data.artifact_summary ? Object.values(data.artifact_summary).flat().length : 0;
    }

    if (tasksRes.ok) {
      const data = await tasksRes.json();
      state.tasks = data.tasks || [];
      renderTaskBoard();
    }

    if (agentsRes.ok) {
      const data = await agentsRes.json();
      state.agents = data.agents || [];
      renderAgentGrid();
    }

    if (artifactsRes.ok) {
      const data = await artifactsRes.json();
      state.artifacts = data.artifacts || [];
      renderArtifactList();
    }

    // Update progress banner
    renderSwarmProgress();
  } catch (err) {
    addFeedItem('fail', `Failed to load project data: ${err.message}`);
  }
}

// ── Swarm Progress Banner ───────────────────────────────────

// Track live agent phases from WebSocket events
const _agentPhases = {};

function renderSwarmProgress() {
  const banner = document.getElementById('swarmProgress');
  const statusEl = document.getElementById('swarmProgressStatus');
  const summaryEl = document.getElementById('swarmProgressSummary');
  const barEl = document.getElementById('swarmProgressBar');
  const agentsEl = document.getElementById('swarmProgressAgents');
  if (!banner) return;

  const tasks = state.tasks || [];
  const agents = state.agents || [];

  if (tasks.length === 0 && agents.length === 0) {
    banner.classList.add('hidden');
    return;
  }
  banner.classList.remove('hidden');

  // Count task states
  const completed = tasks.filter(t => t.status === 'completed' || t.status === 'dead').length;
  const active = tasks.filter(t => t.status === 'active').length;
  const pending = tasks.filter(t => t.status === 'pending').length;
  const waiting = tasks.filter(t => t.status === 'waiting').length;
  const total = tasks.length;
  const pct = total > 0 ? Math.round((completed / total) * 100) : 0;

  // Count agent states
  const aliveAgents = agents.filter(a => a.status === 'alive' || a.status === 'working');
  const deadAgents = agents.filter(a => a.status === 'dead');

  // Determine overall status
  let statusText, statusClass;
  if (active > 0 || aliveAgents.length > 0) {
    statusText = 'WORKING';
    statusClass = 'working';
  } else if (completed === total && total > 0) {
    statusText = 'COMPLETED';
    statusClass = 'completed';
  } else if (tasks.some(t => t.status === 'dead' && t.error)) {
    statusText = 'STALLED';
    statusClass = 'error';
  } else {
    statusText = 'IDLE';
    statusClass = 'idle';
  }

  statusEl.textContent = statusText;
  statusEl.className = `swarm-progress-status ${statusClass}`;

  // Summary text
  const parts = [];
  if (aliveAgents.length > 0) parts.push(`${aliveAgents.length} agent${aliveAgents.length > 1 ? 's' : ''} working`);
  parts.push(`${completed}/${total} tasks done`);
  if (active > 0) parts.push(`${active} active`);
  if (pending > 0) parts.push(`${pending} queued`);
  if (waiting > 0) parts.push(`${waiting} waiting`);
  const artifactCount = parseInt(dom.statArtifacts.textContent) || 0;
  if (artifactCount > 0) parts.push(`${artifactCount} artifacts`);
  summaryEl.textContent = parts.join(' · ');

  // Progress bar
  barEl.style.width = `${pct}%`;
  barEl.style.background = statusClass === 'error' ? 'var(--red)' : statusClass === 'completed' ? 'var(--accent)' : 'var(--green)';

  // Active agent chips
  const chipAgents = aliveAgents.length > 0 ? aliveAgents : deadAgents.slice(-5);
  agentsEl.innerHTML = chipAgents.map(a => {
    const isAlive = a.status === 'alive' || a.status === 'working';
    const phase = _agentPhases[a.id] || (isAlive ? 'working' : 'done');
    const persona = a.persona || a.name || 'Agent';
    return `<span class="swarm-agent-chip ${isAlive ? 'alive' : 'dead'}">
      <span class="chip-dot"></span>
      ${escapeHtml(persona)}
      <span class="chip-phase">${phase}</span>
    </span>`;
  }).join('');
}

// ── WebSocket ────────────────────────────────────────────────

function connectWebSocket(projectId) {
  if (state.ws) {
    state.ws.close();
    state.ws = null;
  }

  const ws = new WebSocket(`${WS_BASE}/projects/${projectId}/stream`);

  ws.onopen = () => {
    dom.connectionStatus.textContent = 'LIVE';
    dom.connectionStatus.classList.add('connected');
    addFeedItem('complete', 'Connected to swarm feed.');
  };

  ws.onclose = () => {
    dom.connectionStatus.textContent = 'DISCONNECTED';
    dom.connectionStatus.classList.remove('connected');
    // Auto-reconnect after 3s
    setTimeout(() => {
      if (state.activeProjectId === projectId) {
        connectWebSocket(projectId);
      }
    }, 3000);
  };

  ws.onerror = () => {
    dom.connectionStatus.textContent = 'ERROR';
    dom.connectionStatus.classList.remove('connected');
  };

  ws.onmessage = (event) => {
    try {
      const data = JSON.parse(event.data);
      handleSwarmEvent(data);
    } catch (err) {
      // ignore
    }
  };

  state.ws = ws;

  // Keep alive ping every 25s
  const pingInterval = setInterval(() => {
    if (ws.readyState === WebSocket.OPEN) {
      ws.send('ping');
    } else {
      clearInterval(pingInterval);
    }
  }, 25000);
}

function handleSwarmEvent(event) {
  switch (event.type) {
    case 'agent_spawned':
      if (event.agent_id) _agentPhases[event.agent_id] = 'spawned';
      addFeedItem('spawn',
        `Agent spawned: <strong>${event.agent_name}</strong> ` +
        `for <span class="tag">${event.task_type}</span>`
      );
      dom.statAgents.textContent = parseInt(dom.statAgents.textContent) + 1;
      renderSwarmProgress();
      break;

    case 'agent_died':
      if (event.agent_id) _agentPhases[event.agent_id] = event.error ? 'failed' : 'done';
      addFeedItem('death',
        `Agent retired: <strong>${event.agent_name}</strong>` +
        (event.error ? ` (error: ${event.error})` : '')
      );
      renderSwarmProgress();
      break;

    case 'task_submitted':
      addFeedItem('task',
        `Task queued: <span class="tag">${event.task_type}</span> ` +
        `priority=${priorityLabel(event.priority)}`
      );
      dom.statTasks.textContent = parseInt(dom.statTasks.textContent) + 1;
      break;

    case 'task_completed':
      addFeedItem('complete',
        `Task completed: <strong>${event.task_id}</strong>`
      );
      renderSwarmProgress();
      break;

    case 'task_failed':
      addFeedItem('fail',
        `Task failed: <strong>${event.task_id}</strong> — ${event.error} ` +
        `(retry ${event.retry_count})`
      );
      renderSwarmProgress();
      break;

    case 'artifact_created':
      addFeedItem('artifact',
        `Artifact produced: <strong>${event.artifact_name}</strong> ` +
        `<span class="tag">${event.artifact_type}</span>` +
        (event.tags ? ` [${event.tags.join(', ')}]` : '')
      );
      dom.statArtifacts.textContent = parseInt(dom.statArtifacts.textContent) + 1;
      break;

    case 'agent_progress':
      // Track phase for progress banner
      if (event.agent_id) _agentPhases[event.agent_id] = event.phase;
      addFeedItem('task',
        `<strong>${event.agent_name || 'Agent'}</strong> ` +
        `<span class="tag">${event.phase}</span> ` +
        (event.detail ? `— ${escapeHtml(event.detail)}` : '')
      );
      renderSwarmProgress();
      break;

    case 'project_completed':
      addFeedItem('complete',
        `<strong>Project completed!</strong> All tasks finished.`
      );
      showToast('All tasks completed. Swarm finished.', 'ok');
      renderSwarmProgress();
      break;

    case 'llm_usage':
      // Silent — just track
      break;

    case 'agent_question':
      // An agent is asking the user a question
      if (event.agent_id) _agentPhases[event.agent_id] = 'waiting_for_user';
      showAgentQuestion(event);
      addFeedItem('task',
        `<strong>${event.agent_name || 'Agent'}</strong> is asking: ` +
        `<em>${escapeHtml((event.question || '').slice(0, 100))}</em>`
      );
      showToast('An agent needs your input!', 'warn');
      renderSwarmProgress();
      break;

    case 'interaction_answered':
      addFeedItem('complete',
        `User responded to agent question: <em>${escapeHtml((event.response || '').slice(0, 80))}</em>`
      );
      // Remove the question card from the UI
      removeAgentQuestion(event.interaction_id);
      renderSwarmProgress();
      break;
  }

  // Refresh data every few events
  if (state.activeProjectId) {
    debounceRefresh();
  }
}

let refreshTimer = null;
function debounceRefresh() {
  if (refreshTimer) clearTimeout(refreshTimer);
  refreshTimer = setTimeout(() => {
    refreshProject(state.activeProjectId);
  }, 2000);
}

// ── Render: Activity Feed ────────────────────────────────────

function addFeedItem(type, html) {
  const now = new Date();
  const time = now.toLocaleTimeString('en-US', { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' });

  const iconMap = {
    spawn: { cls: 'spawn', icon: '▶' },
    complete: { cls: 'complete', icon: '✓' },
    fail: { cls: 'fail', icon: '✕' },
    artifact: { cls: 'artifact', icon: '◆' },
    task: { cls: 'task', icon: '◎' },
    death: { cls: 'death', icon: '■' },
  };

  const { cls, icon } = iconMap[type] || iconMap.task;

  const item = document.createElement('div');
  item.className = 'feed-item';
  item.innerHTML = `
    <span class="feed-time">${time}</span>
    <span class="feed-icon ${cls}">${icon}</span>
    <span class="feed-text">${html}</span>
  `;

  // Remove empty state if present
  const empty = dom.activityFeed.querySelector('.empty-state');
  if (empty) empty.remove();

  // Prepend (newest first)
  dom.activityFeed.prepend(item);

  // Cap at 200 items
  const items = dom.activityFeed.querySelectorAll('.feed-item');
  if (items.length > 200) {
    items[items.length - 1].remove();
  }

  state.feedItems.unshift({ type, html, time });
}

// ── Render: Task Board ───────────────────────────────────────

function renderTaskBoard() {
  const groups = { pending: [], active: [], waiting: [], completed: [] };

  for (const t of state.tasks) {
    const s = t.status;
    if (s === 'dead' || s === 'failed') {
      groups.completed.push(t);
    } else if (groups[s]) {
      groups[s].push(t);
    }
  }

  dom.tasksPending.innerHTML = groups.pending.map(renderTaskCard).join('') || '';
  dom.tasksActive.innerHTML = groups.active.map(renderTaskCard).join('') || '';
  dom.tasksWaiting.innerHTML = groups.waiting.map(renderTaskCard).join('') || '';
  dom.tasksCompleted.innerHTML = groups.completed.map(renderTaskCard).join('') || '';

  dom.countPending.textContent = groups.pending.length;
  dom.countActive.textContent = groups.active.length;
  dom.countWaiting.textContent = groups.waiting.length;
  dom.countCompleted.textContent = groups.completed.length;
}

function renderTaskCard(task) {
  const label = task.payload?.label || task.payload?.component || task.payload?.brief?.slice(0, 50) || task.type;
  const prio = parseInt(task.priority || 1);
  const prioLabels = { 0: 'LOW', 1: 'NORMAL', 2: 'HIGH', 3: 'CRITICAL' };
  return `
    <div class="task-card">
      <div class="task-type">${task.type}</div>
      <div class="task-label" title="${escapeHtml(String(label))}">${escapeHtml(String(label).slice(0, 60))}</div>
      <span class="task-priority priority-${prio}">${prioLabels[prio] || 'NORMAL'}</span>
    </div>
  `;
}

// ── Render: Agent Grid ───────────────────────────────────────

function renderAgentGrid() {
  if (state.agents.length === 0) {
    dom.agentGrid.innerHTML = '<div class="empty-state">No agents spawned yet.</div>';
    return;
  }

  // Sort: alive/working first, then by created_at desc
  const sorted = [...state.agents].sort((a, b) => {
    const order = { alive: 0, working: 0, waiting: 1, dead: 2 };
    const diff = (order[a.status] ?? 2) - (order[b.status] ?? 2);
    if (diff !== 0) return diff;
    return new Date(b.created_at) - new Date(a.created_at);
  });

  dom.agentGrid.innerHTML = sorted.map(agent => {
    const taskType = agent.personality?.task_type || '—';
    return `
      <div class="agent-card">
        <div class="agent-persona">${escapeHtml(agent.persona || agent.name)}</div>
        <div class="agent-id">${agent.id}</div>
        <div class="agent-task-type">${escapeHtml(taskType)}</div>
        <div class="agent-status ${agent.status}">
          <span class="agent-status-dot"></span>
          ${agent.status.toUpperCase()}
        </div>
      </div>
    `;
  }).join('');
}

// ── Render: Artifact List ────────────────────────────────────

function renderArtifactList() {
  if (state.artifacts.length === 0) {
    dom.artifactList.innerHTML = '<div class="empty-state">Artifacts will appear here as agents produce work.</div>';
    renderDeliverablesSummary();
    return;
  }

  const sorted = [...state.artifacts].sort((a, b) => new Date(b.created_at) - new Date(a.created_at));

  dom.artifactList.innerHTML = sorted.map(art => {
    const badgeClass = `badge-${art.type}`;
    const created = new Date(art.created_at).toLocaleTimeString('en-US', { hour12: false, hour: '2-digit', minute: '2-digit' });
    const icon = artifactIcon(art.type);
    const sizeLabel = art.content ? `${Math.round(art.content.length / 1024 * 10) / 10}KB` : '';
    return `
      <div class="artifact-card" data-id="${art.id}" onclick="viewArtifact('${art.id}')">
        <span class="artifact-type-badge ${badgeClass}">${icon} ${art.type.replace(/_/g, ' ').toUpperCase()}</span>
        <div class="artifact-name">${escapeHtml(art.name)}</div>
        <div class="artifact-meta">${created} ${sizeLabel ? '· ' + sizeLabel : ''} · ${(art.tags || []).join(', ')}</div>
      </div>
    `;
  }).join('');

  renderDeliverablesSummary();
}

function artifactIcon(type) {
  const icons = {
    code_file: '{ }',
    review: '✓',
    test_suite: '▶',
    deployment_config: '⚙',
    database_schema: '⊞',
    architecture_plan: '◫',
    requirements_doc: '▤',
    ui_design: '◧',
    frontend_component: '◩',
  };
  return icons[type] || '◆';
}

function renderDeliverablesSummary() {
  const summary = document.getElementById('deliverablesSummary');
  const stats = document.getElementById('deliverablesStats');
  if (!summary || !stats) return;

  if (state.artifacts.length === 0) {
    summary.classList.add('hidden');
    return;
  }

  summary.classList.remove('hidden');

  // Group by type
  const groups = {};
  state.artifacts.forEach(a => {
    const t = a.type || 'unknown';
    groups[t] = (groups[t] || 0) + 1;
  });

  stats.innerHTML = Object.entries(groups).map(([type, count]) => `
    <span class="deliverable-chip">
      ${artifactIcon(type)}
      <span class="chip-count">${count}</span>
      ${type.replace(/_/g, ' ')}
    </span>
  `).join('');
}

// ── Artifact detail viewer ───────────────────────────────────

let _viewingArtifactId = null;

window.viewArtifact = function(id) {
  const art = state.artifacts.find(a => String(a.id) === String(id));
  if (!art) return;
  _viewingArtifactId = id;

  dom.detailName.textContent = art.name;
  const ext = guessExtension(art);
  dom.detailMeta.textContent = `Type: ${art.type} · File: ${ext} · ${art.content ? Math.round(art.content.length/1024*10)/10 + 'KB' : ''} · Tags: ${(art.tags || []).join(', ')}`;
  dom.detailContent.textContent = art.content || '(empty)';
  dom.artifactDetail.classList.remove('hidden');
};

function guessExtension(art) {
  const t = art.type || '';
  const tags = (art.tags || []).join(' ').toLowerCase();
  if (t === 'code_file') {
    if (tags.includes('python') || tags.includes('fastapi')) return 'main.py';
    if (tags.includes('react') || tags.includes('next')) return 'index.tsx';
    if (tags.includes('javascript') || tags.includes('express')) return 'index.js';
    return 'code.py';
  }
  if (t === 'test_suite') return 'tests.py';
  if (t === 'deployment_config') return 'docker-compose.yml';
  if (t === 'database_schema') return 'schema.sql';
  if (t === 'requirements_doc') return 'requirements.md';
  if (t === 'architecture_plan') return 'architecture.md';
  if (t === 'review') return 'review.md';
  if (t === 'ui_design') return 'ui_design.md';
  if (t === 'frontend_component') return 'component.tsx';
  return 'artifact.txt';
}

function downloadText(filename, content) {
  const blob = new Blob([content], { type: 'text/plain;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

// Download single artifact
document.getElementById('btnDownloadArtifact')?.addEventListener('click', () => {
  const art = state.artifacts.find(a => String(a.id) === String(_viewingArtifactId));
  if (!art || !art.content) return;
  const safeName = art.name.replace(/[^a-zA-Z0-9_\-. ]/g, '_').replace(/\s+/g, '_');
  const ext = guessExtension(art);
  downloadText(`${safeName}.${ext.split('.').pop()}`, art.content);
});

// Copy artifact content
document.getElementById('btnCopyArtifact')?.addEventListener('click', () => {
  const art = state.artifacts.find(a => String(a.id) === String(_viewingArtifactId));
  if (!art || !art.content) return;
  navigator.clipboard.writeText(art.content).then(() => {
    const btn = document.getElementById('btnCopyArtifact');
    const old = btn.textContent;
    btn.textContent = 'Copied!';
    setTimeout(() => btn.textContent = old, 1500);
  });
});

// Download ALL as ZIP (hits backend API which assembles a proper project structure)
document.getElementById('btnDownloadAll')?.addEventListener('click', async () => {
  if (!state.activeProjectId) return;
  const btn = document.getElementById('btnDownloadAll');
  const orig = btn.textContent;
  btn.textContent = 'Downloading...';
  btn.disabled = true;

  try {
    const resp = await fetch(`${API_BASE}/projects/${state.activeProjectId}/download`);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const blob = await resp.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    const projectName = state.projects.find(p => p.id === state.activeProjectId)?.name || 'project';
    a.download = `${projectName.replace(/\s+/g, '_').toLowerCase()}_deliverables.zip`;
    a.click();
    URL.revokeObjectURL(url);
  } catch (e) {
    console.error('Download failed:', e);
    // Fallback: download as text bundle
    if (state.artifacts.length === 0) return;
    const projectName = state.projects.find(p => p.id === state.activeProjectId)?.name || 'project';
    let combined = `# ${projectName} — Swarm Deliverables\n# Generated: ${new Date().toISOString()}\n\n`;
    state.artifacts.forEach(art => {
      combined += `${'═'.repeat(60)}\n  [${art.type}] ${art.name}\n${'═'.repeat(60)}\n\n${art.content || '(empty)'}\n\n\n`;
    });
    downloadText(`${projectName.replace(/\s+/g, '_')}_deliverables.txt`, combined);
  } finally {
    btn.textContent = orig;
    btn.disabled = false;
  }
});

dom.btnCloseDetail.addEventListener('click', () => {
  dom.artifactDetail.classList.add('hidden');
  _viewingArtifactId = null;
});

dom.btnRefreshArtifacts.addEventListener('click', () => {
  if (state.activeProjectId) refreshProject(state.activeProjectId);
});

// ── Project sidebar management ───────────────────────────────

function addProjectToSidebar(id, name, brief, status) {
  const empty = dom.projectList.querySelector('.empty-state');
  if (empty) empty.remove();

  // Don't add duplicates
  if ($(`.project-card[data-id="${id}"]`)) return;

  const card = document.createElement('div');
  card.className = 'project-card';
  card.dataset.id = id;
  card.innerHTML = `
    <div class="project-card-top">
      <div class="project-card-name">
        <span class="project-status-dot ${status || 'active'}"></span>
        ${escapeHtml(name)}
      </div>
      <button class="btn-delete-project" title="Delete project" data-id="${id}">&times;</button>
    </div>
    <div class="project-card-brief">${escapeHtml(brief)}</div>
    <div class="project-card-meta">
      <span>${new Date().toLocaleDateString()}</span>
    </div>
  `;
  card.querySelector('.project-card-name').addEventListener('click', (e) => {
    e.stopPropagation();
    selectProject(id);
  });
  card.addEventListener('click', () => selectProject(id));
  card.querySelector('.btn-delete-project').addEventListener('click', (e) => {
    e.stopPropagation();
    deleteProject(id, name);
  });
  dom.projectList.prepend(card);
}

async function deleteProject(id, name) {
  if (!confirm(`Delete project "${name}" and all its agents, tasks, and artifacts?`)) return;

  try {
    const res = await fetch(`${API_BASE}/projects/${id}`, { method: 'DELETE' });
    if (res.ok) {
      // Remove card from sidebar
      const card = $(`.project-card[data-id="${id}"]`);
      if (card) card.remove();

      // If this was the active project, clear state
      if (state.activeProjectId === id) {
        state.activeProjectId = null;
        state.tasks = [];
        state.agents = [];
        state.artifacts = [];
        if (state.ws) { state.ws.close(); state.ws = null; }
        dom.activityFeed.innerHTML = '<div class="empty-state">Select a project to see live activity.</div>';
        dom.agentGrid.innerHTML = '<div class="empty-state">No agents spawned yet.</div>';
        dom.artifactList.innerHTML = '<div class="empty-state">Artifacts will appear here as agents produce work.</div>';
        dom.statAgents.textContent = '0';
        dom.statTasks.textContent = '0';
        dom.statArtifacts.textContent = '0';
      }

      // Show empty state if no projects left
      if (!dom.projectList.querySelector('.project-card')) {
        dom.projectList.innerHTML = '<div class="empty-state">No projects yet. Launch a build to start.</div>';
      }

      showToast(`Project "${name}" deleted.`, 'ok');
    } else {
      const data = await res.json().catch(() => ({}));
      showToast(`Delete failed: ${data.detail || res.statusText}`, 'error');
    }
  } catch (err) {
    showToast(`Delete failed: ${err.message}`, 'error');
  }
}

// Override the launch to also add to sidebar
const originalLaunchHandler = dom.btnLaunchSwarm.onclick;
dom.btnLaunchSwarm.addEventListener('click', async function handler(e) {
  // The main handler already runs. We just hook post-launch to add to sidebar.
});

// Patch: after creating a project, add to sidebar
const origFetch = window.fetch;
window.fetch = async function(...args) {
  const res = await origFetch.apply(this, args);

  // Clone the response so the original consumer can still use it
  const url = typeof args[0] === 'string' ? args[0] : args[0]?.url;
  if (url && url.endsWith('/projects') && args[1]?.method === 'POST') {
    const cloned = res.clone();
    cloned.json().then(data => {
      if (data.id) {
        const body = JSON.parse(args[1].body);
        addProjectToSidebar(data.id, body.name, body.brief, 'active');
      }
    }).catch(() => {});
  }

  return res;
};

// ── Utilities ────────────────────────────────────────────────

function escapeHtml(str) {
  const div = document.createElement('div');
  div.textContent = str;
  return div.innerHTML;
}

function priorityLabel(p) {
  return { 0: 'LOW', 1: 'NORMAL', 2: 'HIGH', 3: 'CRITICAL' }[p] || 'NORMAL';
}

function shakeElement(el) {
  el.style.borderColor = 'var(--red)';
  el.focus();
  setTimeout(() => { el.style.borderColor = ''; }, 1500);
}

// ── Toast notifications ─────────────────────────────────────

function showToast(message, type = 'ok') {
  let container = document.getElementById('toastContainer');
  if (!container) {
    container = document.createElement('div');
    container.id = 'toastContainer';
    container.className = 'toast-container';
    document.body.appendChild(container);
  }

  const toast = document.createElement('div');
  toast.className = `toast toast-${type}`;
  const icons = { ok: '&#10003;', error: '&#10007;', warn: '&#9888;' };
  toast.innerHTML = `<span class="toast-icon">${icons[type] || ''}</span><span>${message}</span>`;
  container.appendChild(toast);

  // Force reflow then animate in
  toast.offsetHeight;
  toast.classList.add('toast-visible');

  setTimeout(() => {
    toast.classList.remove('toast-visible');
    setTimeout(() => toast.remove(), 300);
  }, 4000);
}

// ── Agent Question UI ─────────────────────────────────────────

function showAgentQuestion(event) {
  // Create or find the questions container
  let container = document.getElementById('agentQuestions');
  if (!container) {
    container = document.createElement('div');
    container.id = 'agentQuestions';
    container.className = 'agent-questions-container';
    // Insert at the top of the activity feed
    const feed = document.getElementById('tabActivity');
    if (feed) feed.prepend(container);
  }

  const qid = event.interaction_id;
  const options = event.options || [];

  const card = document.createElement('div');
  card.className = 'agent-question-card';
  card.id = `question-${qid}`;
  card.innerHTML = `
    <div class="aq-header">
      <span class="aq-icon">?</span>
      <strong>${escapeHtml(event.agent_name || 'Agent')}</strong> needs your input
    </div>
    <div class="aq-question">${escapeHtml(event.question || '')}</div>
    ${event.context ? `<div class="aq-context">${escapeHtml(event.context)}</div>` : ''}
    ${options.length > 0 ? `
      <div class="aq-options">
        ${options.map((opt, i) => `
          <button class="btn btn-ghost btn-sm aq-option" data-qid="${qid}" data-answer="${escapeHtml(opt)}">${escapeHtml(opt)}</button>
        `).join('')}
      </div>
    ` : ''}
    <div class="aq-input-row">
      <input type="text" class="input aq-input" id="aq-input-${qid}" placeholder="Type your response...">
      <button class="btn btn-primary btn-sm aq-submit" data-qid="${qid}">Send</button>
    </div>
    <div class="aq-timer">Agent is waiting...</div>
  `;

  container.prepend(card);

  // Option buttons fill the input
  card.querySelectorAll('.aq-option').forEach(btn => {
    btn.addEventListener('click', () => {
      const input = card.querySelector('.aq-input');
      input.value = btn.dataset.answer;
    });
  });

  // Submit handler
  card.querySelector('.aq-submit').addEventListener('click', () => {
    const input = card.querySelector('.aq-input');
    const answer = input.value.trim();
    if (!answer) {
      shakeElement(input);
      return;
    }
    submitAgentAnswer(qid, answer, card);
  });

  // Enter key handler
  card.querySelector('.aq-input').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      const answer = e.target.value.trim();
      if (answer) submitAgentAnswer(qid, answer, card);
    }
  });
}

async function submitAgentAnswer(interactionId, answer, card) {
  const submitBtn = card.querySelector('.aq-submit');
  submitBtn.disabled = true;
  submitBtn.textContent = 'Sending...';

  try {
    const res = await fetch(`${API_BASE}/interactions/${interactionId}/answer`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ response: answer }),
    });
    if (res.ok) {
      card.classList.add('aq-answered');
      card.querySelector('.aq-timer').textContent = `Answered: ${answer}`;
      card.querySelector('.aq-input-row').remove();
      const optionsEl = card.querySelector('.aq-options');
      if (optionsEl) optionsEl.remove();
      showToast('Response sent to agent.', 'ok');
    } else {
      const data = await res.json().catch(() => ({}));
      showToast(`Failed: ${data.detail || res.statusText}`, 'error');
      submitBtn.disabled = false;
      submitBtn.textContent = 'Send';
    }
  } catch (err) {
    showToast(`Network error: ${err.message}`, 'error');
    submitBtn.disabled = false;
    submitBtn.textContent = 'Send';
  }
}

function removeAgentQuestion(interactionId) {
  const card = document.getElementById(`question-${interactionId}`);
  if (card) {
    card.classList.add('aq-answered');
    const timer = card.querySelector('.aq-timer');
    if (timer) timer.textContent = 'Answered';
    // Remove after animation
    setTimeout(() => card.remove(), 3000);
  }
}

// Load pending interactions on project select
async function loadPendingInteractions(projectId) {
  try {
    const res = await fetch(`${API_BASE}/projects/${projectId}/interactions?status=pending`);
    if (res.ok) {
      const data = await res.json();
      (data.interactions || []).forEach(interaction => {
        showAgentQuestion({
          interaction_id: interaction.id,
          agent_id: interaction.agent_id,
          agent_name: interaction.agent_id ? `Agent ${interaction.agent_id.slice(0, 8)}` : 'Agent',
          project_id: interaction.project_id,
          question: interaction.question,
          options: interaction.options || [],
          context: interaction.context || '',
        });
      });
    }
  } catch (err) {
    // Not critical
  }
}

// ── Init ─────────────────────────────────────────────────────

// Show system status
addFeedItem('complete', 'Swarm Control initialized. Waiting for connection.');
loadProjects();


// ═══════════════════════════════════════════════════════════════
// SWARM GRAPH — MiroFish-style Force-Directed Visualization
// Clean nodes, Bezier curves, dot-grid, proper physics
// ═══════════════════════════════════════════════════════════════

const PERSONA_COLORS = [
  '#FF6B35', '#004E89', '#7B2D8E', '#1A936F', '#C5283D',
  '#E9724C', '#3498db', '#9b59b6', '#27ae60', '#f39c12', '#1abc9c'
];

let _graphSim = null;
let _graphData = { nodes: [], edges: [], stats: {} };
let _graphZoom = null;
let _graphG = null;
let _showEdgeLabels = true;
let _graphLoading = false;
let _linkLabelsRef = null;
let _linkLabelBgRef = null;

// Build a stable color map for persona types
const _personaColorMap = {};
let _colorIdx = 0;
function getPersonaColor(persona) {
  if (!_personaColorMap[persona]) {
    _personaColorMap[persona] = PERSONA_COLORS[_colorIdx % PERSONA_COLORS.length];
    _colorIdx++;
  }
  return _personaColorMap[persona];
}

// ── Graph Loading ─────────────────────────────────────────────

function setGraphState(stateStr) {
  const container = document.getElementById('graphContainer');
  if (!container) return;
  const overlay = container.querySelector('.graph-state-overlay');
  if (overlay) overlay.remove();

  if (stateStr === 'none') return;

  const div = document.createElement('div');
  div.className = 'graph-state-overlay';

  if (stateStr === 'loading') {
    div.innerHTML = '<div class="graph-spinner"></div><p>Loading graph data...</p>';
  } else if (stateStr === 'empty') {
    div.innerHTML = '<div class="graph-empty-icon">&#10070;</div><p>No agents spawned yet.<br>Launch a project or inject a requirement.</p>';
  } else if (stateStr === 'error') {
    div.innerHTML = '<div class="graph-empty-icon" style="color:var(--red)">!</div><p>Failed to load graph.<br>The backend may be redeploying.</p>';
  }
  container.appendChild(div);
}

async function loadGraph() {
  if (!state.activeProjectId) return;
  _graphLoading = true;
  setGraphState('loading');

  try {
    const res = await origFetch(`${API_BASE}/projects/${state.activeProjectId}/graph`);
    if (!res.ok) {
      setGraphState('error');
      _graphLoading = false;
      return;
    }
    const data = await res.json();
    _graphData = {
      nodes: data.nodes || [],
      edges: data.edges || [],
      stats: data.stats || {},
    };

    if (_graphData.nodes.length === 0) {
      setGraphState('empty');
    } else {
      setGraphState('none');
      renderGraph();
    }
  } catch (e) {
    setGraphState('error');
  }
  _graphLoading = false;
}

// ── Graph Renderer (MiroFish-exact) ──────────────────────────
// Reference: MiroFish GraphPanel.vue — white bg, solid circles,
// thin gray edges, edge labels with white rect bg, node labels beside node.

// MiroFish edge/node color constants
const EDGE_COLOR = '#C0C0C0';
const EDGE_ARTIFACT_COLOR = '#B39DDB';
const NODE_STROKE = '#fff';
const NODE_STROKE_DEAD = '#ccc';
const HIGHLIGHT_COLOR = '#E91E63';
const LABEL_COLOR = '#333';
const EDGE_LABEL_COLOR = '#666';
const EDGE_LABEL_BG = 'rgba(255,255,255,0.95)';

function renderGraph() {
  const container = document.getElementById('graphContainer');
  const svgEl = document.getElementById('graphSvg');
  if (!container || !svgEl) return;

  // Stop previous simulation
  if (_graphSim) { _graphSim.stop(); _graphSim = null; }

  const rect = container.getBoundingClientRect();
  const width = rect.width || 800;
  const height = rect.height || 500;

  const svg = d3.select(svgEl)
    .attr('width', width)
    .attr('height', height)
    .attr('viewBox', `0 0 ${width} ${height}`);

  svg.selectAll('*').remove();

  const nodesData = _graphData.nodes;
  const edgesData = _graphData.edges;
  if (nodesData.length === 0) return;

  // Prep nodes — initialize positions around center to prevent drift
  const cx = width / 2, cy = height / 2;
  const nodes = nodesData.map((n, i) => {
    const angle = (2 * Math.PI * i) / nodesData.length;
    const radius = Math.min(width, height) * 0.25;
    return {
      id: n.id,
      label: n.label || 'Agent',
      status: n.status || 'dead',
      task_type: n.task_type || '',
      created_at: n.created_at,
      died_at: n.died_at,
      x: cx + radius * Math.cos(angle),
      y: cy + radius * Math.sin(angle),
    };
  });

  const nodeIds = new Set(nodes.map(n => n.id));

  // Prep edges — compute curvature for multi-edges (MiroFish quadratic Bezier)
  const edgePairCount = {};
  const edgePairIndex = {};
  const edges = [];

  edgesData.forEach(e => {
    if (!nodeIds.has(e.source) || !nodeIds.has(e.target)) return;
    const pairKey = [e.source, e.target].sort().join('|');
    edgePairCount[pairKey] = (edgePairCount[pairKey] || 0) + 1;
  });

  edgesData.forEach(e => {
    if (!nodeIds.has(e.source) || !nodeIds.has(e.target)) return;
    const pairKey = [e.source, e.target].sort().join('|');
    const total = edgePairCount[pairKey];
    const idx = edgePairIndex[pairKey] || 0;
    edgePairIndex[pairKey] = idx + 1;

    const isReversed = e.source > e.target;
    let curvature = 0;
    if (total > 1) {
      const range = Math.min(1.2, 0.6 + total * 0.15);
      curvature = ((idx / (total - 1)) - 0.5) * range * 2;
      if (isReversed) curvature = -curvature;
    }

    edges.push({
      source: e.source,
      target: e.target,
      label: (e.label || '').replace(/_/g, ' '),
      edgeType: e.type || 'spawned',
      curvature,
      pairTotal: total,
    });
  });

  // Build legend
  renderGraphLegend(nodes);

  // Main group
  _graphG = svg.append('g');

  // Zoom — MiroFish smooth
  _graphZoom = d3.zoom()
    .scaleExtent([0.1, 4])
    .on('zoom', (event) => _graphG.attr('transform', event.transform));
  svg.call(_graphZoom);

  // Force simulation — MiroFish-exact params
  // Strong center pull + forceX/Y to prevent drifting to corners
  const nodeCount = nodes.length;
  const chargeStrength = nodeCount <= 5 ? -300 : nodeCount <= 15 ? -400 : -500;
  const linkDist = nodeCount <= 5 ? 120 : 150;
  const centerPull = nodeCount <= 5 ? 0.08 : 0.04;

  _graphSim = d3.forceSimulation(nodes)
    .force('link', d3.forceLink(edges).id(d => d.id).distance(d => {
      return linkDist + ((d.pairTotal || 1) - 1) * 40;
    }).strength(0.7))
    .force('charge', d3.forceManyBody().strength(chargeStrength).distanceMax(500))
    .force('center', d3.forceCenter(cx, cy).strength(0.1))
    .force('collide', d3.forceCollide(40))
    .force('x', d3.forceX(cx).strength(centerPull))
    .force('y', d3.forceY(cy).strength(centerPull))
    .alphaDecay(0.02)
    .velocityDecay(0.4);

  // ── Draw edges — MiroFish: thin gray, no glow ──────────────

  const linkGroup = _graphG.append('g').attr('class', 'links');

  function getLinkPath(d) {
    const sx = d.source.x, sy = d.source.y;
    const tx = d.target.x, ty = d.target.y;
    if (d.curvature === 0) return `M${sx},${sy} L${tx},${ty}`;
    const dx = tx - sx, dy = ty - sy;
    const dist = Math.sqrt(dx * dx + dy * dy) || 1;
    const offsetRatio = 0.25 + (d.pairTotal || 1) * 0.05;
    const baseOffset = Math.max(35, dist * offsetRatio);
    const ox = (-dy / dist) * d.curvature * baseOffset;
    const oy = (dx / dist) * d.curvature * baseOffset;
    const qx = (sx + tx) / 2 + ox;
    const qy = (sy + ty) / 2 + oy;
    return `M${sx},${sy} Q${qx},${qy} ${tx},${ty}`;
  }

  function getLinkMid(d) {
    const sx = d.source.x, sy = d.source.y;
    const tx = d.target.x, ty = d.target.y;
    if (d.curvature === 0) return { x: (sx + tx) / 2, y: (sy + ty) / 2 };
    const dx = tx - sx, dy = ty - sy;
    const dist = Math.sqrt(dx * dx + dy * dy) || 1;
    const offsetRatio = 0.25 + (d.pairTotal || 1) * 0.05;
    const baseOffset = Math.max(35, dist * offsetRatio);
    const ox = (-dy / dist) * d.curvature * baseOffset;
    const oy = (dx / dist) * d.curvature * baseOffset;
    const qx = (sx + tx) / 2 + ox;
    const qy = (sy + ty) / 2 + oy;
    return { x: 0.25 * sx + 0.5 * qx + 0.25 * tx, y: 0.25 * sy + 0.5 * qy + 0.25 * ty };
  }

  const link = linkGroup.selectAll('path')
    .data(edges).enter().append('path')
    .attr('stroke', d => d.edgeType === 'artifact' ? EDGE_ARTIFACT_COLOR : EDGE_COLOR)
    .attr('stroke-width', 1.5)
    .attr('fill', 'none')
    .attr('stroke-dasharray', d => d.edgeType === 'artifact' ? '5,3' : 'none')
    .style('cursor', 'pointer')
    .on('click', (event, d) => {
      event.stopPropagation();
      resetHighlight();
      d3.select(event.target).attr('stroke', HIGHLIGHT_COLOR).attr('stroke-width', 3);
    });

  // Edge label backgrounds — MiroFish: white rect with slight opacity
  const linkLabelBg = linkGroup.selectAll('rect')
    .data(edges).enter().append('rect')
    .attr('fill', EDGE_LABEL_BG)
    .attr('rx', 3).attr('ry', 3)
    .style('pointer-events', 'none')
    .style('display', _showEdgeLabels ? 'block' : 'none');
  _linkLabelBgRef = linkLabelBg;

  // Edge labels — MiroFish: 9px, gray #666
  const linkLabels = linkGroup.selectAll('text')
    .data(edges).enter().append('text')
    .text(d => d.label)
    .attr('font-size', '9px')
    .attr('fill', EDGE_LABEL_COLOR)
    .attr('text-anchor', 'middle')
    .attr('dominant-baseline', 'middle')
    .style('pointer-events', 'none')
    .style('font-family', "'Inter', sans-serif")
    .style('display', _showEdgeLabels ? 'block' : 'none');
  _linkLabelsRef = linkLabels;

  // ── Draw nodes — MiroFish: solid circles, white stroke ─────

  const nodeGroup = _graphG.append('g').attr('class', 'nodes');

  const node = nodeGroup.selectAll('circle')
    .data(nodes).enter().append('circle')
    .attr('r', 10)
    .attr('fill', d => getPersonaColor(d.label))
    .attr('stroke', NODE_STROKE)
    .attr('stroke-width', 2.5)
    .attr('opacity', d => d.status === 'dead' ? 0.45 : 1)
    .style('cursor', 'pointer')
    .call(d3.drag()
      .on('start', (event, d) => {
        if (!event.active) _graphSim.alphaTarget(0.3).restart();
        d.fx = d.x; d.fy = d.y;
        d._dragStartX = event.x; d._dragStartY = event.y; d._isDragging = false;
      })
      .on('drag', (event, d) => {
        const ddx = event.x - d._dragStartX, ddy = event.y - d._dragStartY;
        if (!d._isDragging && Math.sqrt(ddx * ddx + ddy * ddy) > 3) {
          d._isDragging = true;
        }
        d.fx = event.x; d.fy = event.y;
      })
      .on('end', (event, d) => {
        if (!event.active) _graphSim.alphaTarget(0);
        d.fx = null; d.fy = null; d._isDragging = false;
      })
    )
    .on('click', (event, d) => {
      event.stopPropagation();
      resetHighlight();
      // Highlight selected node
      d3.select(event.target).attr('stroke', HIGHLIGHT_COLOR).attr('stroke-width', 4);
      // Highlight connected edges
      link.filter(l => l.source.id === d.id || l.target.id === d.id)
        .attr('stroke', HIGHLIGHT_COLOR).attr('stroke-width', 2.5);
      showGraphDetail(d);
    })
    .on('mouseenter', (event) => {
      const sel = d3.select(event.target);
      if (sel.attr('stroke') !== HIGHLIGHT_COLOR) sel.attr('stroke-width', 3.5);
    })
    .on('mouseleave', (event) => {
      const sel = d3.select(event.target);
      if (sel.attr('stroke') !== HIGHLIGHT_COLOR) sel.attr('stroke-width', 2.5);
    });

  // Node labels — MiroFish: 11px, #333, font-weight 500, beside node (dx=14, dy=4)
  const nodeLabels = nodeGroup.selectAll('text')
    .data(nodes).enter().append('text')
    .text(d => {
      const l = d.label || 'Agent';
      return l.length > 18 ? l.substring(0, 16) + '...' : l;
    })
    .attr('font-size', '11px')
    .attr('fill', LABEL_COLOR)
    .attr('font-weight', '500')
    .attr('dx', 14)
    .attr('dy', 4)
    .style('pointer-events', 'none')
    .style('font-family', "'Inter', sans-serif");

  // ── Tick — update positions ────────────────────────────────

  _graphSim.on('tick', () => {
    // Constrain nodes within bounds (with padding)
    const pad = 30;
    nodes.forEach(d => {
      d.x = Math.max(pad, Math.min(width - pad, d.x));
      d.y = Math.max(pad, Math.min(height - pad, d.y));
    });

    link.attr('d', d => getLinkPath(d));

    linkLabels.each(function(d) {
      const mid = getLinkMid(d);
      d3.select(this).attr('x', mid.x).attr('y', mid.y);
    });

    linkLabelBg.each(function(d, i) {
      const mid = getLinkMid(d);
      const textEl = linkLabels.nodes()[i];
      if (!textEl) return;
      try {
        const bbox = textEl.getBBox();
        d3.select(this)
          .attr('x', mid.x - bbox.width / 2 - 3)
          .attr('y', mid.y - bbox.height / 2 - 1)
          .attr('width', bbox.width + 6)
          .attr('height', bbox.height + 2);
      } catch(e) {}
    });

    node.attr('cx', d => d.x).attr('cy', d => d.y);
    nodeLabels.attr('x', d => d.x).attr('y', d => d.y);
  });

  // Helper: reset all highlights back to MiroFish defaults
  function resetHighlight() {
    node.attr('stroke', NODE_STROKE).attr('stroke-width', 2.5);
    link.attr('stroke', d => d.edgeType === 'artifact' ? EDGE_ARTIFACT_COLOR : EDGE_COLOR).attr('stroke-width', 1.5);
  }

  // Click background to deselect
  svg.on('click', () => {
    resetHighlight();
    hideGraphDetail();
  });
}

// ── Graph Detail Panel ────────────────────────────────────────

function showGraphDetail(d) {
  const panel = document.getElementById('graphDetailPanel');
  const title = document.getElementById('graphDetailTitle');
  const body = document.getElementById('graphDetailBody');
  if (!panel) return;

  title.textContent = d.label || 'Agent';

  const statusClass = (d.status === 'alive' || d.status === 'working') ? 'gd-status-alive' : d.status === 'waiting' ? 'gd-status-waiting' : 'gd-status-dead';
  const born = d.created_at && d.created_at !== 'None' ? new Date(d.created_at).toLocaleTimeString() : '--';
  const died = d.died_at && d.died_at !== 'None' && d.died_at !== 'null' && d.died_at !== '' ? new Date(d.died_at).toLocaleTimeString() : null;

  const connEdges = _graphData.edges.filter(e => e.source === d.id || e.target === d.id);
  const connHtml = connEdges.length > 0
    ? connEdges.map(e => {
        const other = e.source === d.id ? e.target : e.source;
        const otherNode = _graphData.nodes.find(n => n.id === other);
        const dir = e.source === d.id ? '&rarr;' : '&larr;';
        return `<div class="gd-conn">${dir} ${otherNode ? escapeHtml(otherNode.label) : other.slice(0, 8)} <span class="gd-conn-type">${escapeHtml((e.label || '').replace(/_/g, ' '))}</span></div>`;
      }).join('')
    : '<div class="gd-conn" style="color:var(--text-muted)">No connections yet</div>';

  body.innerHTML = `
    <div class="gd-row"><span class="gd-key">Status</span><span class="gd-val ${statusClass}">${(d.status || '').toUpperCase()}</span></div>
    <div class="gd-row"><span class="gd-key">Task</span><span class="gd-val">${escapeHtml(d.task_type || '--')}</span></div>
    <div class="gd-row"><span class="gd-key">Spawned</span><span class="gd-val">${born}</span></div>
    ${died ? `<div class="gd-row"><span class="gd-key">Retired</span><span class="gd-val">${died}</span></div>` : ''}
    <div class="gd-row"><span class="gd-key">ID</span><span class="gd-val">${d.id.slice(0, 12)}...</span></div>
    <div class="gd-connections">
      <span class="gd-key">Connections</span>
      ${connHtml}
    </div>
  `;
  panel.classList.remove('hidden');
}

function hideGraphDetail() {
  const panel = document.getElementById('graphDetailPanel');
  if (panel) panel.classList.add('hidden');
}

function renderGraphLegend(nodes) {
  const legend = document.getElementById('graphLegend');
  if (!legend) return;
  const types = {};
  (nodes || _graphData.nodes).forEach(n => {
    if (!types[n.label]) types[n.label] = 0;
    types[n.label]++;
  });
  // MiroFish-exact: red "ENTITY TYPES" title + grid of dot+label items
  legend.innerHTML =
    `<div class="graph-legend-title">ENTITY TYPES</div>
     <div class="graph-legend-grid">` +
    Object.entries(types).map(([label]) =>
      `<div class="legend-item">
        <span class="legend-dot" style="background:${getPersonaColor(label)}"></span>
        <span class="legend-label">${escapeHtml(label)}</span>
      </div>`
    ).join('') +
    `</div>`;
}

// ── Graph controls ────────────────────────────────────────────

document.getElementById('btnGraphReset')?.addEventListener('click', () => {
  if (_graphZoom) {
    d3.select('#graphSvg').transition().duration(400).call(_graphZoom.transform, d3.zoomIdentity);
  }
});

document.getElementById('btnGraphRefresh')?.addEventListener('click', () => {
  if (state.activeProjectId) loadGraph();
});

document.getElementById('chkEdgeLabels')?.addEventListener('change', (e) => {
  _showEdgeLabels = e.target.checked;
  if (_linkLabelsRef) _linkLabelsRef.style('display', _showEdgeLabels ? 'block' : 'none');
  if (_linkLabelBgRef) _linkLabelBgRef.style('display', _showEdgeLabels ? 'block' : 'none');
});

document.getElementById('btnCloseGraphDetail')?.addEventListener('click', () => {
  hideGraphDetail();
});

// ── Auto-load graph on tab switch + project select ────────────

// Load graph whenever Graph tab is activated
$$('.tab').forEach(tab => {
  tab.addEventListener('click', () => {
    if (tab.dataset.tab === 'graph' && state.activeProjectId) {
      setTimeout(loadGraph, 50);
    }
  });
});

// Also load graph on project select (override selectProject)
const _origSelectProject = selectProject;
selectProject = function(projectId) {
  _origSelectProject(projectId);
  // Pre-load graph data in background
  setTimeout(loadGraph, 500);
};

// Refresh graph on swarm events (debounced)
const _origHandleSwarmEvent = handleSwarmEvent;
handleSwarmEvent = function(event) {
  _origHandleSwarmEvent(event);
  if (['agent_spawned', 'agent_died', 'task_completed', 'artifact_created'].includes(event.type)) {
    clearTimeout(window._graphRefreshTimer);
    window._graphRefreshTimer = setTimeout(() => {
      const graphTab = document.querySelector('.tab[data-tab="graph"]');
      if (graphTab && graphTab.classList.contains('active')) loadGraph();
    }, 2000);
  }
};

// Resize handler
window.addEventListener('resize', () => {
  clearTimeout(window._graphResizeTimer);
  window._graphResizeTimer = setTimeout(() => {
    const graphTab = document.querySelector('.tab[data-tab="graph"]');
    if (graphTab && graphTab.classList.contains('active') && _graphData.nodes.length > 0) renderGraph();
  }, 300);
});
