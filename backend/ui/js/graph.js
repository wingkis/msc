/**
 * MSC Memory — Vis.js Graph Visualization
 */

let graph = null;
let graphNetwork = null;
let allGraphData = null;
let filteredGraphData = null;

function getPrioritySize(p) {
  return { critical: 10, high: 8, medium: 6, low: 4 }[p] || 6;
}

const CATEGORY_COLORS = {
  config: '#3b82f6',
  project: '#22c55e',
  user: '#eab308',
  knowledge: '#a855f7',
  reference: '#6b7280'
};

function getCategoryColor(cat) {
  return CATEGORY_COLORS[cat] || '#6b7280';
}

async function loadGraph() {
  try {
    const data = await API.getGraph();
    allGraphData = data;
    filteredGraphData = data;
    renderGraph(data);
  } catch (e) {
    console.error('Graph load error:', e);
    renderGraphFallback(allGraphData, String(e.message || e));
  }
}

function renderGraphFallback(data, reason) {
  const container = document.getElementById('graph-container');
  const edges = (data && data.edges) || [];
  let html = '<div class="empty-hint">图形库未能加载（' + escHtml(reason) + '），以下用列表展示关联。</div>';
  if (edges.length === 0) {
    html += '<div class="empty-state"><div class="empty-state-icon">🔗</div><div class="empty-state-text">暂无关联数据</div></div>';
  } else {
    html += '<div class="fallback-list">';
    edges.forEach(e => {
      html += '<div class="fallback-row"><span>' + escHtml(e.source) + '</span>' +
        '<span class="fallback-rel">' + escHtml(e.relation_type || 'related') + '</span>' +
        '<span>' + escHtml(e.target) + '</span></div>';
    });
    html += '</div>';
  }
  container.innerHTML = html;
}

function renderGraph(data) {
  const container = document.getElementById('graph-container');

  if (typeof vis === 'undefined') {
    renderGraphFallback(data, 'vis-network CDN 不可达');
    return;
  }

  // Destroy existing graph
  if (graphNetwork) {
    graphNetwork.destroy();
    graphNetwork = null;
  }

  if (!data || !data.nodes || data.nodes.length === 0) {
    container.innerHTML = '<div class="empty-state"><div class="empty-state-icon">🔗</div><div class="empty-state-text">暂无关联数据</div></div>';
    return;
  }

  const visNodes = data.nodes.map(n => ({
    id: n.id,
    label: n.slug,
    title: `<strong>#${n.id} ${escHtml(n.name)}</strong><br>${n.category} · ${n.priority}`,
    size: getPrioritySize(n.priority),
    color: {
      background: getCategoryColor(n.category),
      border: getCategoryColor(n.category),
      highlight: { background: getCategoryColor(n.category), border: '#fff' },
      hover: { background: getCategoryColor(n.category), border: '#fff' }
    },
    font: { color: '#e4e6ed', size: 12, face: '-apple-system, sans-serif' },
    shape: 'dot'
  }));

  const visEdges = data.edges.map(e => ({
    from: e.source,
    to: e.target,
    arrows: { to: { enabled: e.relation_type === 'depends_on' } },
    color: { color: 'rgba(139,143,163,0.4)' },
    smooth: { type: 'continuous', roundness: 0.2 },
    title: e.relation_type
  }));

  const options = {
    nodes: {
      shape: 'dot',
      size: 8,
      font: { size: 11, color: '#e4e6ed', face: '-apple-system, sans-serif' },
      borderWidth: 2,
      shadow: { enabled: true, color: 'rgba(0,0,0,0.3)', size: 4, x: 0, y: 0 }
    },
    edges: {
      width: 1.5,
      smooth: { type: 'continuous', roundness: 0.2 },
      font: { size: 10, color: 'rgba(139,143,163,0.6)', face: '-apple-system, sans-serif' }
    },
    physics: {
      enabled: true,
      stabilization: { iterations: 100 },
      barnesHut: { gravitationalConstant: -3000, springConstant: 0.04, springLength: 120 }
    },
    interaction: {
      hover: true,
      tooltipDelay: 100,
      dragNodes: true,
      dragView: true,
      zoomView: true,
      zoomSpeed: 0.3
    }
  };

  graphNetwork = new vis.Network(container, { nodes: visNodes, edges: visEdges }, options);

  // Click handler
  graphNetwork.on('click', function(params) {
    if (params.nodes.length > 0) {
      const nodeId = params.nodes[0];
      const nodeData = visNodes.find(n => n.id == nodeId);
      if (nodeData) showGraphNodeDetail(nodeData.id);
    }
  });

  graphNetwork.on('doubleClick', function(params) {
    if (params.nodes.length > 0) {
      const nodeId = params.nodes[0];
      const nodeData = visNodes.find(n => n.id == nodeId);
      if (nodeData) {
        // Same detail modal as the list view: id, history, relations, delete.
        showMemoryDetail(nodeData.label);
      }
    }
  });
}

function showGraphNodeDetail(nodeId) {
  if (!allGraphData) return;
  const node = allGraphData.nodes.find(n => n.id === nodeId);
  if (!node) return;

  const edges = allGraphData.edges.filter(e => e.source === nodeId || e.target === nodeId);
  const related = edges.map(e => {
    const otherId = e.source === nodeId ? e.target : e.source;
    const other = allGraphData.nodes.find(n => n.id === otherId);
    return other ? `${other.name} (${e.relation_type})` : 'unknown';
  });

  const container = document.getElementById('graph-detail');
  container.innerHTML = `
    <p><span class="memory-id">#${node.id}</span> <strong>${escHtml(node.name)}</strong> <em>(${escHtml(node.slug)})</em></p>
    <p><span class="category-badge category-${node.category}">${node.category}</span> · ${node.priority}</p>
    ${related.length > 0 ? `<p style="margin-top:8px;color:var(--text-muted)">关联: ${related.join(', ')}</p>` : '<p style="margin-top:8px;color:var(--text-dim)">无关联</p>'}
  `;
}

function filterGraphNodes() {
  if (!allGraphData) return;

  const searchTerm = document.getElementById('graph-search').value.toLowerCase();
  const categoryFilter = document.getElementById('graph-category-filter').value;

  let nodes = allGraphData.nodes;
  let edges = allGraphData.edges;

  if (searchTerm) {
    const matchedIds = new Set(nodes.filter(n =>
      n.slug.toLowerCase().includes(searchTerm) || n.name.toLowerCase().includes(searchTerm)
    ).map(n => n.id));
    nodes = nodes.filter(n => matchedIds.has(n.id));
    edges = edges.filter(e => matchedIds.has(e.source) && matchedIds.has(e.target));
  }

  if (categoryFilter) {
    const catIds = new Set(nodes.filter(n => n.category === categoryFilter).map(n => n.id));
    nodes = nodes.filter(n => catIds.has(n.id));
    edges = edges.filter(e => catIds.has(e.source) && catIds.has(e.target));
  }

  renderGraph({ nodes, edges });
}

function fitGraph() {
  if (graphNetwork) {
    graphNetwork.fit({ animation: { duration: 300, easingFunction: 'easeInOutQuad' } });
  }
}

function reloadGraph() {
  loadGraph();
}

function escHtml(str) {
  if (!str) return '';
  const div = document.createElement('div');
  div.textContent = str;
  return div.innerHTML;
}
