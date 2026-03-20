/* ═══════════════════════════════════════════════════════════════
   SWARM CONTROL — Dashboard Application
   Connects to the Swarm API + WebSocket for real-time updates.
   ═══════════════════════════════════════════════════════════════ */

const API_BASE = window.location.origin + '/api';
const WS_BASE = (window.location.protocol === 'https:' ? 'wss:' : 'ws:') + '//' + window.location.host + '/api';

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
      addFeedItem('task', `Project <strong>${name}</strong> created. Swarm activated.`);
      await loadProjects();
      selectProject(data.id);
    } else {
      addFeedItem('fail', `Failed to create project: ${data.detail || 'Unknown error'}`);
    }
  } catch (err) {
    addFeedItem('fail', `Network error: ${err.message}`);
  } finally {
    dom.btnLaunchSwarm.disabled = false;
    dom.btnLaunchSwarm.textContent = 'Launch Swarm';
  }
});

// ── Inject requirement ───────────────────────────────────────

dom.btnInject.addEventListener('click', async () => {
  if (!state.activeProjectId) return;
  const text = dom.inputInject.value.trim();
  if (text.length < 5) return;

  try {
    const res = await fetch(`${API_BASE}/projects/${state.activeProjectId}/inject`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ requirement: text }),
    });
    if (res.ok) {
      addFeedItem('task', `Requirement injected: <strong>${text.slice(0, 60)}...</strong>`);
      dom.inputInject.value = '';
    }
  } catch (err) {
    addFeedItem('fail', `Inject failed: ${err.message}`);
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
  } catch (err) {
    addFeedItem('fail', `Failed to load project data: ${err.message}`);
  }
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
      addFeedItem('spawn',
        `Agent spawned: <strong>${event.agent_name}</strong> ` +
        `for <span class="tag">${event.task_type}</span>`
      );
      dom.statAgents.textContent = parseInt(dom.statAgents.textContent) + 1;
      break;

    case 'agent_died':
      addFeedItem('death',
        `Agent retired: <strong>${event.agent_name}</strong>` +
        (event.error ? ` (error: ${event.error})` : '')
      );
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
      break;

    case 'task_failed':
      addFeedItem('fail',
        `Task failed: <strong>${event.task_id}</strong> — ${event.error} ` +
        `(retry ${event.retry_count})`
      );
      break;

    case 'artifact_created':
      addFeedItem('artifact',
        `Artifact produced: <strong>${event.artifact_name}</strong> ` +
        `<span class="tag">${event.artifact_type}</span>` +
        (event.tags ? ` [${event.tags.join(', ')}]` : '')
      );
      dom.statArtifacts.textContent = parseInt(dom.statArtifacts.textContent) + 1;
      break;

    case 'llm_usage':
      // Silent — just track
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
    return;
  }

  const sorted = [...state.artifacts].sort((a, b) => new Date(b.created_at) - new Date(a.created_at));

  dom.artifactList.innerHTML = sorted.map(art => {
    const badgeClass = `badge-${art.type}`;
    const created = new Date(art.created_at).toLocaleTimeString('en-US', { hour12: false, hour: '2-digit', minute: '2-digit' });
    return `
      <div class="artifact-card" data-id="${art.id}" onclick="viewArtifact('${art.id}')">
        <span class="artifact-type-badge ${badgeClass}">${art.type.replace(/_/g, ' ').toUpperCase()}</span>
        <div class="artifact-name">${escapeHtml(art.name)}</div>
        <div class="artifact-meta">${created} · ${(art.tags || []).join(', ')}</div>
      </div>
    `;
  }).join('');
}

// ── Artifact detail viewer ───────────────────────────────────

window.viewArtifact = function(id) {
  const art = state.artifacts.find(a => String(a.id) === String(id));
  if (!art) return;

  dom.detailName.textContent = art.name;
  dom.detailMeta.textContent = `Type: ${art.type} · Agent: ${art.agent_id || '—'} · Tags: ${(art.tags || []).join(', ')}`;
  dom.detailContent.textContent = art.content || '(empty)';
  dom.artifactDetail.classList.remove('hidden');
};

dom.btnCloseDetail.addEventListener('click', () => {
  dom.artifactDetail.classList.add('hidden');
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
    <div class="project-card-name">
      <span class="project-status-dot ${status || 'active'}"></span>
      ${escapeHtml(name)}
    </div>
    <div class="project-card-brief">${escapeHtml(brief)}</div>
    <div class="project-card-meta">
      <span>${new Date().toLocaleDateString()}</span>
    </div>
  `;
  card.addEventListener('click', () => selectProject(id));
  dom.projectList.prepend(card);
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

// ── Init ─────────────────────────────────────────────────────

addFeedItem('complete', 'Swarm Control dashboard loaded. Create a project to begin.');
loadProjects();
