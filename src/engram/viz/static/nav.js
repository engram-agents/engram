(function () {
  var sel = document.getElementById('agent-select-nav');
  if (!sel) return;
  var wrapper = document.getElementById('nav-agent-wrapper');

  fetch('/api/agents').then(function(r) { return r.json(); }).then(function(data) {
    var agents = data.agents || [];
    var defaultAgent = data.default || (agents[0] && agents[0].name);
    if (agents.length <= 1) return;  // single-agent mode: keep selector hidden

    var currentAgent = new URL(window.location.href).searchParams.get('agent');
    if (!currentAgent || !agents.find(function(a) { return a.name === currentAgent; })) {
      currentAgent = defaultAgent;
    }

    sel.innerHTML = '';
    agents.forEach(function(a) {
      var opt = document.createElement('option');
      opt.value = a.name;
      var status = a.available ? '(' + a.current_node_count + ')' : '(unavailable)';
      opt.textContent = a.label + ' ' + status;
      if (!a.available && a.error) opt.title = a.error;
      sel.appendChild(opt);
    });
    sel.value = currentAgent;
    if (wrapper) wrapper.style.display = 'inline-flex';

    sel.onchange = function() {
      var u = new URL(window.location.href);
      u.searchParams.set('agent', sel.value);
      window.location.href = u.toString();
    };
  }).catch(function() {});
}());
