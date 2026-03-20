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


// ═══════════════════════════════════════════════════════════════
// SWARM GRAPH — D3.js Force-Directed Visualization
// MiroFish-inspired with animated agent spawning/dying
// ═══════════════════════════════════════════════════════════════

const GRAPH_COLORS = {
  // Agent persona → color mapping
  'Product Manager':    '#FF6B35',
  'System Architect':   '#004E89',
  'Database Engineer':  '#fbbf24',
  'UI/UX Designer':     '#e879f9',
  'Backend Engineer':   '#34d399',
  'Frontend Engineer':  '#22d3ee',
  'Tech Lead':          '#a78bfa',
  'QA Engineer':        '#f87171',
  'Technical Writer':   '#8b919e',
  'Research Analyst':   '#fb923c',
  'DevOps Engineer':    '#4ade80',
};

const STATUS_GLOW = {
  alive:   '#34d399',
  working: '#34d399',
  waiting: '#fb923c',
  dead:    '#555c6b',
};

let graphSim = null;
let graphData = { nodes: [], edges: [] };
let graphZoom = null;
let graphG = null;
let showEdgeLabels = true;
let selectedGraphNode = null;

function getAgentColor(label) {
  return GRAPH_COLORS[label] || '#4f8ff7';
}

async function loadGraph() {
  if (!state.activeProjectId) return;
  try {
    const res = await origFetch(`${API_BASE}/projects/${state.activeProjectId}/graph`);
    if (!res.ok) return;
    const data = await res.json();
    graphData = { nodes: data.nodes || [], edges: data.edges || [] };
    renderGraph();
  } catch (e) {
    // Graph not available yet
  }
}

function renderGraph() {
  const container = document.getElementById('graphContainer');
  const svg = d3.select('#graphSvg');
  if (!container || !svg.node()) return;

  const rect = container.getBoundingClientRect();
  const width = rect.width || 800;
  const height = (rect.height - 40) || 500; // subtract toolbar

  svg.attr('width', width).attr('height', height);
  svg.selectAll('*').remove();

  // Defs for glow filter + arrow markers
  const defs = svg.append('defs');

  // Glow filter
  const glow = defs.append('filter').attr('id', 'glow');
  glow.append('feGaussianBlur').attr('stdDeviation', '4').attr('result', 'coloredBlur');
  const merge = glow.append('feMerge');
  merge.append('feMergeNode').attr('in', 'coloredBlur');
  merge.append('feMergeNode').attr('in', 'SourceGraphic');

  // Pulse glow for alive agents
  const pulseGlow = defs.append('filter').attr('id', 'pulseGlow');
  pulseGlow.append('feGaussianBlur').attr('stdDeviation', '6').attr('result', 'blur');
  const pMerge = pulseGlow.append('feMerge');
  pMerge.append('feMergeNode').attr('in', 'blur');
  pMerge.append('feMergeNode').attr('in', 'SourceGraphic');

  // Arrow marker
  defs.append('marker')
    .attr('id', 'arrowhead')
    .attr('viewBox', '0 -5 10 10')
    .attr('refX', 28)
    .attr('refY', 0)
    .attr('markerWidth', 8)
    .attr('markerHeight', 8)
    .attr('orient', 'auto')
    .append('path')
    .attr('d', 'M0,-5L10,0L0,5')
    .attr('fill', '#555c6b');

  // Artifact arrow (different color)
  defs.append('marker')
    .attr('id', 'arrowhead-artifact')
    .attr('viewBox', '0 -5 10 10')
    .attr('refX', 28)
    .attr('refY', 0)
    .attr('markerWidth', 8)
    .attr('markerHeight', 8)
    .attr('orient', 'auto')
    .append('path')
    .attr('d', 'M0,-5L10,0L0,5')
    .attr('fill', '#a78bfa');

  if (graphData.nodes.length === 0) {
    svg.append('text')
      .attr('x', width / 2)
      .attr('y', height / 2)
      .attr('text-anchor', 'middle')
      .attr('fill', '#555c6b')
      .attr('font-size', '14px')
      .text('No agents yet. Launch a project to see the swarm graph.');
    return;
  }

  // Build legend
  renderGraphLegend();

  // Main group for zoom/pan
  graphG = svg.append('g');

  // Zoom behavior
  graphZoom = d3.zoom()
    .scaleExtent([0.1, 4])
    .on('zoom', (event) => {
      graphG.attr('transform', event.transform);
    });
  svg.call(graphZoom);

  // Pre-process edges for curve distribution
  const edgePairs = {};
  graphData.edges.forEach(e => {
    const key = [e.source, e.target].sort().join('|');
    edgePairs[key] = (edgePairs[key] || 0) + 1;
    e._pairIndex = edgePairs[key];
  });
  graphData.edges.forEach(e => {
    const key = [e.source, e.target].sort().join('|');
    e._pairTotal = edgePairs[key];
  });

  // Force simulation
  graphSim = d3.forceSimulation(graphData.nodes)
    .force('link', d3.forceLink(graphData.edges)
      .id(d => d.id)
      .distance(d => {
        const base = 180;
        return base + ((d._pairTotal || 1) - 1) * 40;
      })
    )
    .force('charge', d3.forceManyBody().strength(-500))
    .force('center', d3.forceCenter(width / 2, height / 2))
    .force('collide', d3.forceCollide(60))
    .force('x', d3.forceX(width / 2).strength(0.03))
    .force('y', d3.forceY(height / 2).strength(0.03));

  // Draw edges
  const linkGroup = graphG.append('g').attr('class', 'graph-links');

  const links = linkGroup.selectAll('path')
    .data(graphData.edges)
    .enter()
    .append('path')
    .attr('fill', 'none')
    .attr('stroke', d => d.type === 'artifact' ? '#a78bfa' : '#3a4050')
    .attr('stroke-width', d => d.type === 'artifact' ? 1.5 : 2)
    .attr('stroke-dasharray', d => d.type === 'artifact' ? '6,3' : 'none')
    .attr('marker-end', d => d.type === 'artifact' ? 'url(#arrowhead-artifact)' : 'url(#arrowhead)')
    .attr('opacity', 0.6)
    .style('transition', 'opacity 0.3s');

  // Edge labels
  const edgeLabels = linkGroup.selectAll('text')
    .data(graphData.edges)
    .enter()
    .append('text')
    .attr('text-anchor', 'middle')
    .attr('font-size', '9px')
    .attr('fill', '#8b919e')
    .attr('font-family', "'JetBrains Mono', monospace")
    .attr('dy', -6)
    .text(d => d.label.replace(/_/g, ' '))
    .attr('opacity', showEdgeLabels ? 0.8 : 0);

  // Draw nodes
  const nodeGroup = graphG.append('g').attr('class', 'graph-nodes');

  const nodeGs = nodeGroup.selectAll('g')
    .data(graphData.nodes)
    .enter()
    .append('g')
    .attr('class', 'graph-node')
    .style('cursor', 'pointer')
    .call(d3.drag()
      .on('start', dragStart)
      .on('drag', dragging)
      .on('end', dragEnd)
    )
    .on('click', (event, d) => {
      event.stopPropagation();
      selectGraphNode(d, links, nodeGs);
    });

  // Outer glow ring for alive/working agents
  nodeGs.append('circle')
    .attr('r', 24)
    .attr('fill', 'none')
    .attr('stroke', d => STATUS_GLOW[d.status] || 'transparent')
    .attr('stroke-width', d => (d.status === 'alive' || d.status === 'working') ? 2 : 0)
    .attr('opacity', 0.4)
    .attr('class', 'node-glow');

  // Main node circle
  nodeGs.append('circle')
    .attr('r', 18)
    .attr('fill', d => getAgentColor(d.label))
    .attr('stroke', '#1a1e27')
    .attr('stroke-width', 2.5)
    .attr('filter', d => (d.status === 'alive' || d.status === 'working') ? 'url(#glow)' : 'none')
    .attr('opacity', d => d.status === 'dead' ? 0.4 : 1);

  // Status indicator dot
  nodeGs.append('circle')
    .attr('r', 4)
    .attr('cx', 13)
    .attr('cy', -13)
    .attr('fill', d => STATUS_GLOW[d.status] || '#555c6b')
    .attr('stroke', '#0c0e12')
    .attr('stroke-width', 1.5);

  // Node label
  nodeGs.append('text')
    .attr('text-anchor', 'middle')
    .attr('dy', 32)
    .attr('font-size', '10px')
    .attr('font-weight', '600')
    .attr('fill', '#e2e5ea')
    .attr('font-family', "'Inter', sans-serif")
    .text(d => {
      const label = d.label || 'Agent';
      return label.length > 16 ? label.slice(0, 14) + '..' : label;
    });

  // Persona initial inside node
  nodeGs.append('text')
    .attr('text-anchor', 'middle')
    .attr('dy', 5)
    .attr('font-size', '13px')
    .attr('font-weight', '700')
    .attr('fill', '#fff')
    .attr('font-family', "'Inter', sans-serif")
    .text(d => {
      const parts = (d.label || 'A').split(' ');
      return parts.map(p => p[0]).join('').slice(0, 2);
    });

  // Spawn animation: nodes appear with scale-up
  nodeGs.attr('transform', d => `translate(${width / 2},${height / 2}) scale(0)`)
    .transition()
    .duration(600)
    .delay((d, i) => i * 80)
    .ease(d3.easeBackOut.overshoot(1.5))
    .attr('transform', d => `translate(${d.x || width / 2},${d.y || height / 2}) scale(1)`);

  // Links fade in
  links.attr('opacity', 0)
    .transition()
    .duration(400)
    .delay((d, i) => 300 + i * 50)
    .attr('opacity', 0.6);

  // Pulse animation for alive nodes
  function pulseAlive() {
    nodeGs.selectAll('.node-glow')
      .filter(d => d.status === 'alive' || d.status === 'working')
      .transition()
      .duration(1200)
      .attr('r', 28)
      .attr('opacity', 0.15)
      .transition()
      .duration(1200)
      .attr('r', 24)
      .attr('opacity', 0.4)
      .on('end', function() {
        // Only continue if element still exists
        if (this.parentNode) pulseAlive();
      });
  }
  pulseAlive();

  // Tick function
  graphSim.on('tick', () => {
    // Update node positions
    nodeGs.attr('transform', d => `translate(${d.x},${d.y})`);

    // Update link paths with curves for multiple edges
    links.attr('d', d => {
      const dx = d.target.x - d.source.x;
      const dy = d.target.y - d.source.y;
      if (d._pairTotal <= 1) {
        return `M${d.source.x},${d.source.y}L${d.target.x},${d.target.y}`;
      }
      const curvature = ((d._pairIndex - 1) - (d._pairTotal - 1) / 2) * 50;
      const mx = (d.source.x + d.target.x) / 2 + (-dy / Math.sqrt(dx * dx + dy * dy + 1)) * curvature;
      const my = (d.source.y + d.target.y) / 2 + (dx / Math.sqrt(dx * dx + dy * dy + 1)) * curvature;
      return `M${d.source.x},${d.source.y}Q${mx},${my} ${d.target.x},${d.target.y}`;
    });

    // Update edge label positions
    edgeLabels
      .attr('x', d => (d.source.x + d.target.x) / 2)
      .attr('y', d => (d.source.y + d.target.y) / 2);
  });

  // Click background to deselect
  svg.on('click', () => {
    deselectGraphNode(links, nodeGs);
  });
}

function selectGraphNode(d, links, nodeGs) {
  selectedGraphNode = d;

  // Dim everything
  nodeGs.selectAll('circle').transition().duration(200).attr('opacity', 0.15);
  nodeGs.selectAll('text').transition().duration(200).attr('opacity', 0.15);
  links.transition().duration(200).attr('opacity', 0.05);

  // Highlight selected + connected
  const connected = new Set();
  connected.add(d.id);
  graphData.edges.forEach(e => {
    const sid = typeof e.source === 'object' ? e.source.id : e.source;
    const tid = typeof e.target === 'object' ? e.target.id : e.target;
    if (sid === d.id) connected.add(tid);
    if (tid === d.id) connected.add(sid);
  });

  nodeGs.filter(n => connected.has(n.id))
    .selectAll('circle').transition().duration(200).attr('opacity', 1);
  nodeGs.filter(n => connected.has(n.id))
    .selectAll('text').transition().duration(200).attr('opacity', 1);

  links.filter(e => {
    const sid = typeof e.source === 'object' ? e.source.id : e.source;
    const tid = typeof e.target === 'object' ? e.target.id : e.target;
    return sid === d.id || tid === d.id;
  }).transition().duration(200)
    .attr('opacity', 0.9)
    .attr('stroke-width', 3);

  // Show detail panel
  showGraphDetail(d);
}

function deselectGraphNode(links, nodeGs) {
  selectedGraphNode = null;
  nodeGs.selectAll('circle').transition().duration(200)
    .attr('opacity', d => d.status === 'dead' ? 0.4 : 1);
  nodeGs.selectAll('text').transition().duration(200).attr('opacity', 1);
  links.transition().duration(200)
    .attr('opacity', 0.6)
    .attr('stroke-width', d => d.type === 'artifact' ? 1.5 : 2);
  hideGraphDetail();
}

function showGraphDetail(d) {
  const panel = document.getElementById('graphDetailPanel');
  const title = document.getElementById('graphDetailTitle');
  const body = document.getElementById('graphDetailBody');
  if (!panel) return;

  title.textContent = d.label || 'Agent';
  body.innerHTML = `
    <div class="gd-row"><span class="gd-key">ID</span><span class="gd-val">${d.id}</span></div>
    <div class="gd-row"><span class="gd-key">Status</span><span class="gd-val gd-status-${d.status}">${(d.status || '').toUpperCase()}</span></div>
    <div class="gd-row"><span class="gd-key">Task</span><span class="gd-val">${d.task_type || '—'}</span></div>
    <div class="gd-row"><span class="gd-key">Born</span><span class="gd-val">${d.created_at ? new Date(d.created_at).toLocaleTimeString() : '—'}</span></div>
    ${d.died_at && d.died_at !== 'None' && d.died_at !== 'null' ? `<div class="gd-row"><span class="gd-key">Died</span><span class="gd-val">${new Date(d.died_at).toLocaleTimeString()}</span></div>` : ''}
    <div class="gd-connections">
      <span class="gd-key">Connections</span>
      ${graphData.edges.filter(e => {
        const sid = typeof e.source === 'object' ? e.source.id : e.source;
        const tid = typeof e.target === 'object' ? e.target.id : e.target;
        return sid === d.id || tid === d.id;
      }).map(e => {
        const sid = typeof e.source === 'object' ? e.source.id : e.source;
        const tid = typeof e.target === 'object' ? e.target.id : e.target;
        const other = sid === d.id ? tid : sid;
        const otherNode = graphData.nodes.find(n => n.id === other);
        const dir = sid === d.id ? '→' : '←';
        return `<div class="gd-conn">${dir} ${otherNode ? otherNode.label : other.slice(0, 8)} <span class="gd-conn-type">${e.label}</span></div>`;
      }).join('') || '<div class="gd-conn">No connections</div>'}
    </div>
  `;
  panel.classList.remove('hidden');
}

function hideGraphDetail() {
  const panel = document.getElementById('graphDetailPanel');
  if (panel) panel.classList.add('hidden');
}

function renderGraphLegend() {
  const legend = document.getElementById('graphLegend');
  if (!legend) return;

  // Collect unique persona types from current data
  const types = {};
  graphData.nodes.forEach(n => {
    if (!types[n.label]) types[n.label] = 0;
    types[n.label]++;
  });

  legend.innerHTML = Object.entries(types).map(([label, count]) =>
    `<div class="legend-item">
      <span class="legend-dot" style="background:${getAgentColor(label)}"></span>
      <span class="legend-label">${label}</span>
      <span class="legend-count">${count}</span>
    </div>`
  ).join('');
}

// Drag handlers
function dragStart(event, d) {
  if (!event.active) graphSim.alphaTarget(0.3).restart();
  d.fx = d.x;
  d.fy = d.y;
}

function dragging(event, d) {
  d.fx = event.x;
  d.fy = event.y;
}

function dragEnd(event, d) {
  if (!event.active) graphSim.alphaTarget(0);
  d.fx = null;
  d.fy = null;
}

// Graph controls
document.getElementById('btnGraphReset')?.addEventListener('click', () => {
  const svg = d3.select('#graphSvg');
  svg.transition().duration(500).call(graphZoom.transform, d3.zoomIdentity);
});

document.getElementById('chkEdgeLabels')?.addEventListener('change', (e) => {
  showEdgeLabels = e.target.checked;
  d3.selectAll('.graph-links text')
    .transition().duration(200)
    .attr('opacity', showEdgeLabels ? 0.8 : 0);
});

document.getElementById('btnCloseGraphDetail')?.addEventListener('click', () => {
  hideGraphDetail();
});

// Auto-refresh graph when switching to graph tab or on new events
const origTabClick = function(tab) {
  if (tab.dataset.tab === 'graph' && state.activeProjectId) {
    setTimeout(loadGraph, 100);
  }
};

// Hook into tab switching
$$('.tab').forEach(tab => {
  tab.addEventListener('click', () => origTabClick(tab));
});

// Refresh graph on swarm events
const origHandleSwarmEvent = handleSwarmEvent;
handleSwarmEvent = function(event) {
  origHandleSwarmEvent(event);
  // Auto-refresh graph on relevant events
  if (['agent_spawned', 'agent_died', 'task_completed', 'artifact_created'].includes(event.type)) {
    const graphTab = document.querySelector('.tab[data-tab="graph"]');
    if (graphTab && graphTab.classList.contains('active')) {
      clearTimeout(window._graphRefreshTimer);
      window._graphRefreshTimer = setTimeout(loadGraph, 2500);
    }
  }
};

// Handle window resize
window.addEventListener('resize', () => {
  const graphTab = document.querySelector('.tab[data-tab="graph"]');
  if (graphTab && graphTab.classList.contains('active')) {
    clearTimeout(window._graphResizeTimer);
    window._graphResizeTimer = setTimeout(renderGraph, 300);
  }
});
