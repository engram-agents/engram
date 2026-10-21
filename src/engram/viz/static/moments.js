// Moments page (engram-alpha#3511). Read-only: fetches /api/moments and
// renders what the agent already wrote. Every agent-written string goes into
// the DOM through textContent -- never as HTML.
(function () {
  var root = document.getElementById('moments-root');
  if (!root) return;

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined && text !== null) e.textContent = text;
    return e;
  }

  function formatDate(iso) {
    if (!iso) return null;
    var d = new Date(iso);
    if (isNaN(d.getTime())) return String(iso);
    return d.toLocaleDateString(undefined, { year: 'numeric', month: 'long', day: 'numeric' });
  }

  function renderMoment(m) {
    var card = el('div', 'moment');
    card.appendChild(el('div', 'desc', m.description));
    if (m.node_exists && m.node_summary) {
      var linked = el('div', 'linked', m.node_summary);
      if (m.node_id) linked.title = m.node_id;
      card.appendChild(linked);
    } else if (!m.node_exists) {
      var gone = el('div', 'linked gone', 'The record this points to is no longer current.');
      if (m.node_id) gone.title = m.node_id;
      card.appendChild(gone);
    }
    var when = formatDate(m.added_at);
    if (when) card.appendChild(el('div', 'meta', 'added ' + when));
    return card;
  }

  function render(groups) {
    root.textContent = '';
    if (!groups.length) {
      root.appendChild(el('div', 'status', 'No moments yet.'));
      return;
    }
    groups.forEach(function (g) {
      var section = el('div', 'person');
      section.appendChild(el('h2', null, g.name));
      if (g.relation) section.appendChild(el('div', 'relation', g.relation));
      g.moments.forEach(function (m) { section.appendChild(renderMoment(m)); });
      root.appendChild(section);
    });
  }

  var agent = new URL(window.location.href).searchParams.get('agent');
  var url = '/api/moments' + (agent ? '?agent=' + encodeURIComponent(agent) : '');
  fetch(url).then(function (r) { return r.json(); }).then(function (data) {
    if (data.error) {
      root.textContent = '';
      root.appendChild(el('div', 'status', data.error));
      return;
    }
    render(data.groups || []);
  }).catch(function () {
    root.textContent = '';
    root.appendChild(el('div', 'status', 'Could not load moments.'));
  });
}());
