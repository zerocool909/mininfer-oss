"""The server-rendered dashboard's stylesheet and script.

Their own module because they are 600 lines of strings that nothing reads
except the template that inlines them — keeping them beside the request
handling that never looks at them is what made `proxy.py` unmanageable.
"""
CSS = """
:root{
  --background:#09090b; --foreground:#fafafa; --card:#0c0c0e;
  --muted:#18181b; --muted-foreground:#a1a1aa;
  --border:#27272a; --input:#27272a; --ring:#52525b;
  --primary:#fafafa; --primary-foreground:#18181b; --accent:#1f1f23;
  --free:#4ade80; --paid:#fbbf24; --danger:#f87171;
  --radius:.625rem;
}
*{box-sizing:border-box}
body{margin:0;background:var(--background);color:var(--foreground);
  font:14px/1.55 ui-sans-serif,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  -webkit-font-smoothing:antialiased;-moz-osx-font-smoothing:grayscale}
.wrap{max-width:1120px;margin:0 auto;padding:28px 24px 72px}
a{color:inherit;text-decoration:none}

/* header */
.head{display:flex;align-items:center;justify-content:space-between;gap:16px;margin-bottom:26px}
.brand{display:flex;align-items:center;gap:12px}
.logo{width:34px;height:34px;border-radius:9px;display:grid;place-items:center;font-weight:700;
  font-size:15px;background:linear-gradient(140deg,#fafafa,#9ca3af);color:#09090b}
h1{font-size:17px;margin:0;letter-spacing:-.015em;font-weight:650}
.tagline{color:var(--muted-foreground);font-size:12.5px;margin:2px 0 0}
.pill{display:inline-flex;align-items:center;gap:7px;padding:4px 11px;border-radius:999px;
  border:1px solid var(--border);background:var(--card);color:var(--muted-foreground);font-size:12px}
.dot{width:6px;height:6px;border-radius:50%;background:var(--free);box-shadow:0 0 0 3px rgba(34,197,94,.16)}

/* tabs */
.tabs{display:inline-flex;gap:2px;padding:3px;background:var(--muted);border:1px solid var(--border);
  border-radius:10px;margin-bottom:24px}
.tab{appearance:none;border:0;background:transparent;color:var(--muted-foreground);padding:6px 15px;
  border-radius:7px;cursor:pointer;font:inherit;font-size:13px;font-weight:550;transition:.15s}
.tab:hover{color:var(--foreground)}
.tab.on{background:var(--background);color:var(--foreground);box-shadow:0 1px 2px rgba(0,0,0,.45)}
.panel[hidden]{display:none}

/* stat cards */
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(122px,1fr));gap:12px;margin-bottom:30px}
.card{background:var(--card);border:1px solid var(--border);border-radius:var(--radius);padding:14px 16px}
.card .n{font-size:22px;font-weight:650;letter-spacing:-.02em;font-variant-numeric:tabular-nums}
.card .k{color:var(--muted-foreground);font-size:10.5px;text-transform:uppercase;letter-spacing:.06em;margin-top:3px}

/* sections */
.block{margin-bottom:32px}
h2{font-size:13px;font-weight:600;margin:0 0 10px;letter-spacing:.005em}
.hint{color:var(--muted-foreground);font-size:12.5px;margin:0 0 12px;line-height:1.6}
.note{color:var(--muted-foreground);font-size:12px;margin:0 0 12px;padding-left:13px;position:relative}
.note:before{content:"›";position:absolute;left:0;color:var(--ring)}

/* chips */
.chips{display:flex;flex-wrap:wrap;gap:7px;margin-bottom:14px}
.chip{display:inline-flex;align-items:center;padding:5px 12px;border:1px solid var(--border);
  border-radius:999px;font-size:12.5px;color:var(--muted-foreground);transition:.15s}
.chip:hover{color:var(--foreground);border-color:var(--ring)}
.chip.on{background:var(--primary);border-color:var(--primary);color:var(--primary-foreground);font-weight:550}
.chip-row{display:flex;flex-wrap:wrap;gap:6px;padding:11px 14px 2px}
.chip.tag{font-size:11.5px;padding:3px 10px;color:var(--muted-foreground);cursor:help}
.chip.tag b{color:var(--foreground);font-weight:600}

/* tables */
.table-wrap{border:1px solid var(--border);border-radius:var(--radius);overflow:auto;background:var(--card)}
table{border-collapse:collapse;width:100%;font-size:13px}
th,td{text-align:left;padding:10px 14px;border-bottom:1px solid var(--border);white-space:nowrap}
th{color:var(--muted-foreground);font-weight:500;font-size:10.5px;text-transform:uppercase;
  letter-spacing:.055em;background:rgba(255,255,255,.018)}
tbody tr:last-child td{border-bottom:0}
tbody tr:hover td{background:rgba(255,255,255,.018)}
.num{text-align:right;font-variant-numeric:tabular-nums}

/* badges */
.badge{display:inline-flex;align-items:center;padding:2px 9px;border-radius:999px;font-size:11.5px;
  font-weight:550;border:1px solid transparent;white-space:nowrap}
.badge.free{color:#86efac;background:rgba(34,197,94,.14);border-color:rgba(34,197,94,.3)}
.badge.paid{color:#fcd34d;background:rgba(245,158,11,.13);border-color:rgba(245,158,11,.28)}

/* form controls */
.field{display:flex;flex-direction:column;gap:6px;font-size:11.5px;color:var(--muted-foreground);
  text-transform:uppercase;letter-spacing:.05em}
select,input,textarea{background:var(--background);color:var(--foreground);border:1px solid var(--input);
  border-radius:8px;padding:8px 11px;font:inherit;font-size:13px;outline:none;transition:.15s}
select:focus,textarea:focus{border-color:var(--ring);box-shadow:0 0 0 3px rgba(82,82,91,.32)}
textarea{width:100%;resize:vertical;min-height:112px;letter-spacing:normal;text-transform:none;
  font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12.5px;line-height:1.65}
.toggle{display:inline-flex;align-items:center;gap:8px;font-size:12.5px;color:var(--muted-foreground);
  cursor:pointer;user-select:none;padding-bottom:9px}
.toggle input{accent-color:var(--free);width:15px;height:15px;padding:0}

/* buttons */
.btn{display:inline-flex;align-items:center;gap:6px;border:1px solid var(--border);background:var(--muted);
  color:var(--foreground);padding:8px 14px;border-radius:8px;font:inherit;font-size:13px;font-weight:550;
  cursor:pointer;transition:.15s}
.btn:hover{background:var(--accent)}
.btn.primary{background:var(--primary);color:var(--primary-foreground);border-color:var(--primary)}
.btn.primary:hover{opacity:.88}
.btn:disabled{opacity:.45;cursor:default}

/* playground */
.composer{background:var(--card);border:1px solid var(--border);border-radius:var(--radius);
  padding:14px;margin-bottom:20px}
.composer .bar{display:flex;align-items:flex-end;gap:12px;flex-wrap:wrap;margin-top:14px}
.spacer{flex:1 1 auto}
.answer-card,.opt{background:var(--card);border:1px solid var(--border);border-radius:var(--radius);
  overflow:hidden}
.answer-card{margin-bottom:16px}
.row{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:11px 14px;
  border-bottom:1px solid var(--border);background:rgba(255,255,255,.014)}
.answer-body{padding:15px;white-space:pre-wrap;word-break:break-word;font-size:13.5px;line-height:1.65}

/* Markdown in an answer. The React build under web/ owns the real renderer;
   this is the same element set for the dependency-free fallback page, so a
   table stops reading as a wall of pipes here too. */
.answer-body.md{white-space:normal}
.answer-body.md>*:first-child{margin-top:0}
.answer-body.md>*:last-child{margin-bottom:0}
.answer-body.md h1,.answer-body.md h2,.answer-body.md h3,
.answer-body.md h4,.answer-body.md h5,.answer-body.md h6{
  margin:15px 0 7px;line-height:1.3;font-weight:600}
.answer-body.md h1{font-size:16px}
.answer-body.md h2{font-size:15px}
.answer-body.md h3{font-size:14px}
.answer-body.md h4,.answer-body.md h5{font-size:13.5px}
.answer-body.md h6{font-size:11.5px;color:var(--muted-foreground);text-transform:uppercase;
  letter-spacing:.05em}
.answer-body.md p{margin:8px 0}
.answer-body.md ul,.answer-body.md ol{margin:8px 0;padding-left:20px}
.answer-body.md ul{list-style:disc}
.answer-body.md ol{list-style:decimal}
.answer-body.md li{margin:3px 0}
.answer-body.md hr{border:0;border-top:1px solid var(--border);margin:14px 0}
.answer-body.md blockquote{margin:10px 0;padding-left:12px;border-left:2px solid var(--border);
  color:var(--muted-foreground)}
.answer-body.md a{color:var(--ring)}
.answer-body.md strong{font-weight:600}
.answer-body.md del{color:var(--muted-foreground)}
.answer-body.md code{background:var(--muted);border-radius:4px;padding:1px 5px;
  font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.88em}
.answer-body.md pre{background:var(--muted);border:1px solid var(--border);border-radius:8px;
  padding:11px 12px;overflow:auto;margin:10px 0}
.answer-body.md pre code{background:none;padding:0;font-size:12.5px;line-height:1.55}
.answer-body.md table{width:100%;border-collapse:collapse;font-size:13px;margin:10px 0;
  display:block;overflow-x:auto}
.answer-body.md th,.answer-body.md td{border:1px solid var(--border);padding:7px 10px;
  text-align:left;white-space:normal;vertical-align:top}
.answer-body.md th{background:rgba(255,255,255,.025);font-weight:600}
.timer{font-variant-numeric:tabular-nums}
.live-row{display:flex;align-items:center;gap:10px;padding:11px 14px;
  border-bottom:1px solid var(--border);background:rgba(255,255,255,.014)}
.meta{display:grid;grid-template-columns:repeat(auto-fit,minmax(132px,1fr));gap:1px;
  background:var(--border);border-top:1px solid var(--border)}
.meta .cell{background:var(--card);padding:10px 14px}
.meta .k{color:var(--muted-foreground);font-size:10px;text-transform:uppercase;letter-spacing:.055em}
.meta .v{margin-top:3px;font-size:13px;font-variant-numeric:tabular-nums}
.opts{display:grid;grid-template-columns:repeat(auto-fit,minmax(330px,1fr));gap:14px}
.opt{display:flex;flex-direction:column;transition:border-color .15s}
.opt:hover{border-color:var(--ring)}
.opt.chosen{border-color:var(--free);box-shadow:0 0 0 1px rgba(34,197,94,.4)}
.opt .answer-body{flex:1 1 auto}
.error{background:rgba(248,113,113,.08);border:1px solid rgba(248,113,113,.32);color:#fecaca;
  border-radius:var(--radius);padding:12px 14px;font-size:13px}

.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px}
.free{color:var(--free)}.paid{color:var(--paid)}.muted{color:var(--muted-foreground)}
"""

JS = r"""
(function () {
  var tabs = [].slice.call(document.querySelectorAll('.tab'));
  tabs.forEach(function (t) {
    t.addEventListener('click', function () {
      tabs.forEach(function (x) { x.classList.remove('on'); });
      t.classList.add('on');
      [].slice.call(document.querySelectorAll('.panel')).forEach(function (p) {
        p.hidden = (p.id !== 'tab-' + t.dataset.tab);
      });
    });
  });

  var sel = document.getElementById('pg-task');
  var prompt = document.getElementById('pg-prompt');
  var btn = document.getElementById('pg-send');
  var status = document.getElementById('pg-status');
  var result = document.getElementById('pg-result');
  var compare = document.getElementById('pg-compare');
  if (!btn) { return; }

  function esc(s) {
    var d = document.createElement('div');
    d.textContent = (s === null || s === undefined) ? '' : String(s);
    return d.innerHTML;
  }

  function nowMs() { return window.performance ? performance.now() : Date.now(); }

  function inline(s) {
    return s
      .replace(/`([^`]+)`/g, '<code>$1</code>')
      .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
      .replace(/(^|[^*])\*([^*]+)\*/g, '$1<em>$2</em>')
      .replace(/~~([^~]+)~~/g, '<del>$1</del>')
      .replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g,
        '<a href="$2" target="_blank" rel="noreferrer noopener">$1</a>');
  }

  // Markdown subset, escape-first. `esc` runs before any tag is inserted, so a
  // model answer cannot inject markup: the only tags in the output are the ones
  // added below. Without this a table rendered as pipes and headings as '###'.
  function md(src) {
    var text = esc(src).replace(/\r\n/g, '\n');
    var blocks = [];
    text = text.replace(/```[a-z0-9+#-]*\n?([\s\S]*?)```/g, function (_, body) {
      blocks.push('<pre><code>' + body.replace(/\n$/, '') + '</code></pre>');
      return '\u0000' + (blocks.length - 1) + '\u0000';
    });
    var lines = text.split('\n');
    var out = [];
    var i = 0;
    while (i < lines.length) {
      var line = lines[i];
      var ph = /^\u0000(\d+)\u0000$/.exec(line.trim());
      if (ph) { out.push(blocks[+ph[1]]); i++; continue; }
      if (!line.trim()) { i++; continue; }
      var h = /^(#{1,6})\s+(.*)$/.exec(line);
      if (h) {
        var n = h[1].length;
        out.push('<h' + n + '>' + inline(h[2]) + '</h' + n + '>');
        i++; continue;
      }
      if (/^\s*(?:-{3,}|\*{3,}|_{3,})\s*$/.test(line)) { out.push('<hr>'); i++; continue; }
      if (line.indexOf('|') >= 0 && i + 1 < lines.length &&
          /^\s*\|?[\s:|-]*-[\s:|-]*\|?\s*$/.test(lines[i + 1])) {
        var cells = function (l) {
          return l.replace(/^\s*\|/, '').replace(/\|\s*$/, '').split('|');
        };
        var head = cells(line);
        i += 2;
        var rows = [];
        while (i < lines.length && lines[i].indexOf('|') >= 0) {
          rows.push(cells(lines[i])); i++;
        }
        var t = '<table><thead><tr>';
        head.forEach(function (c) { t += '<th>' + inline(c.trim()) + '</th>'; });
        t += '</tr></thead><tbody>';
        rows.forEach(function (r) {
          t += '<tr>';
          r.forEach(function (c) { t += '<td>' + inline(c.trim()) + '</td>'; });
          t += '</tr>';
        });
        out.push(t + '</tbody></table>');
        continue;
      }
      if (/^\s*>\s?/.test(line)) {
        var q = [];
        while (i < lines.length && /^\s*>\s?/.test(lines[i])) {
          q.push(lines[i].replace(/^\s*>\s?/, '')); i++;
        }
        out.push('<blockquote>' + inline(q.join(' ')) + '</blockquote>');
        continue;
      }
      var li = /^\s*([-*+]|\d+[.)])\s+(.*)$/.exec(line);
      if (li) {
        var ordered = /\d/.test(li[1]);
        var items = [];
        while (i < lines.length) {
          var m = /^\s*([-*+]|\d+[.)])\s+(.*)$/.exec(lines[i]);
          if (!m) { break; }
          items.push('<li>' + inline(m[2]) + '</li>'); i++;
        }
        out.push('<' + (ordered ? 'ol' : 'ul') + '>' + items.join('') +
                 '</' + (ordered ? 'ol' : 'ul') + '>');
        continue;
      }
      var para = [];
      while (i < lines.length && lines[i].trim() &&
             !/^(#{1,6}\s|\s*[>*+-]\s|\s*\d+[.)]\s)/.test(lines[i]) &&
             !/^\u0000\d+\u0000$/.test(lines[i].trim())) {
        para.push(lines[i]); i++;
      }
      if (para.length) {
        out.push('<p>' + inline(para.join('\n')).replace(/\n/g, '<br>') + '</p>');
      } else { i++; }
    }
    return out.join('');
  }
  function money(v) {
    if (v === 0) { return 'FREE'; }
    if (v === null || v === undefined) { return '\u2014'; }
    return '$' + Number(v).toFixed(4);
  }
  function badge(v) {
    return (v === 0) ? '<span class="badge free">FREE</span>'
                     : '<span class="badge paid">' + money(v) + ' / success</span>';
  }
  function meta(cells) {
    return '<div class="meta">' + cells.map(function (c) {
      return '<div class="cell"><div class="k">' + esc(c[0]) +
             '</div><div class="v">' + c[1] + '</div></div>';
    }).join('') + '</div>';
  }
  function note(text) { return '<p class="note">' + text + '</p>'; }

  function lbChips(list) {
    if (!list || !list.length) { return ''; }
    return '<div class="chip-row">' + list.map(function (t) {
      var v = (t.value === undefined || t.value === null) ? ''
            : ' <b>' + esc(t.value) + '</b>';
      return '<span class="chip tag" title="' + esc(t.source) + '">' +
        esc(t.source) + ' · ' + esc(t.label) + v + '</span>';
    }).join('') + '</div>';
  }

  function hint(type) {
    if (type === 'no_api_key') {
      return note('Add a provider key to <span class="mono">.env</span> (e.g. ' +
        '<span class="mono">AI_GATEWAY_API_KEY</span>), then restart ' +
        '<span class="mono">mi proxy</span>.');
    }
    if (type === 'empty_content') {
      return note('The model returned no text; the router fell through every candidate.');
    }
    return '';
  }

  function render(j, ms) {
    var d = j.mininfer || {}, reason = d.reason || {};
    var selected = reason.selected || [];
    var primary = selected[0] || {};
    var top = primary, i;
    for (i = 0; i < selected.length; i++) {
      if (selected[i].deploy_id === d.selected_model) { top = selected[i]; break; }
    }
    var msg = (j.choices && j.choices[0] && j.choices[0].message) || {};
    var u = j.usage || {};
    var html = '';
    var skipped = reason.skipped_no_key || [];
    var primarySkipped = primary.deploy_id && skipped.indexOf(primary.deploy_id) >= 0;
    if (primary.deploy_id && !primarySkipped && primary.deploy_id !== d.selected_model &&
        selected.length) {
      html += note('primary <span class="mono">' + esc(primary.deploy_id) +
        '</span> failed \u2014 fell back to <span class="mono">' +
        esc(d.selected_model) + '</span>');
    }
    if (skipped.length) {
      html += note('skipped, no key: <span class="mono">' + skipped.map(esc).join(', ') +
        '</span>');
    }
    var f = reason.funnel || {};
    if (f.rejected_no_key) {
      html += note(f.rejected_no_key + ' deployment(s) not considered \u2014 provider has ' +
        'no API key');
    }
    if (reason.needs_approval && !(compare && compare.checked)) {
      html += note('this arm has no track record \u2014 tick &ldquo;compare 2 models&rdquo; ' +
        'for a second opinion');
    }
    html += '<div class="answer-card">';
    html += '<div class="row"><span class="mono">' + esc(d.selected_model) + '</span>' +
            badge(top.cost_per_success) + '</div>';
    html += '<div class="answer-body md">' + md(msg.content || '(no text returned)') + '</div>';
    html += lbChips(top.leaderboards);
    html += meta([
      ['task', esc(d.task)],
      ['p(success)', (top.p_lb === null || top.p_lb === undefined)
        ? '\u2014' : Number(top.p_lb).toFixed(3)],
      ['latency', ms + ' ms'],
      ['tokens', (u.prompt_tokens || 0) + ' in / ' + (u.completion_tokens || 0) + ' out'],
      ['diversity', esc(reason.diversity || '\u2014')],
      ['alternatives', (d.alternatives || []).length
        ? '<span class="mono">' + d.alternatives.map(esc).join('<br>') + '</span>'
        : '<span class="muted">none</span>']
    ]);
    html += '</div>';
    result.innerHTML = html;
  }

  function renderOptions(j) {
    var d = j.mininfer || {}, opts = d.options || [];
    var html = (opts.length > 1)
      ? note('Two independent answers. Pick one \u2014 your choice is recorded and ' +
             'teaches the router.')
      : note('Only one of the compared models returned an answer.');
    html += '<div class="opts">';
    for (var i = 0; i < opts.length; i++) {
      var o = opts[i] || {};
      var msg = (j.choices && j.choices[i] && j.choices[i].message) || {};
      html += '<div class="opt" id="pg-opt-' + i + '">';
      html += '<div class="row"><span class="mono">' + esc(o.deploy_id) + '</span>' +
              badge(o.cost_per_success) + '</div>';
      html += '<div class="answer-body md">' + md(msg.content || '(no text returned)') + '</div>';
      html += lbChips(o.leaderboards);
      html += '<div class="row" style="border-bottom:0;border-top:1px solid var(--border)">' +
              '<span class="muted" style="font-size:11.5px">p(success) ' +
              ((o.p_lb === null || o.p_lb === undefined) ? '\u2014'
                : Number(o.p_lb).toFixed(3)) +
              ((o.latency_ms === null || o.latency_ms === undefined) ? ''
                : ' \u00b7 ' + Math.round(o.latency_ms) + ' ms') + '</span>' +
              '<button class="btn primary pg-use" data-i="' + i + '">Use this</button></div>';
      html += '</div>';
    }
    html += '</div><p id="pg-approve-status" class="note" style="margin-top:12px"></p>';
    result.innerHTML = html;
    [].slice.call(result.querySelectorAll('.pg-use')).forEach(function (b) {
      b.addEventListener('click', function () { approve(parseInt(b.dataset.i, 10), j); });
    });
  }

  function approve(i, j) {
    var d = j.mininfer || {}, opts = d.options || [];
    var chosen = (opts[i] || {}).deploy_id;
    var rejected = opts.filter(function (o, k) { return k !== i; })
                       .map(function (o) { return o.deploy_id; });
    fetch('/v1/approve', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ task: d.task, chosen: chosen, rejected: rejected })
    }).then(function (r) { return r.json(); }).then(function () {
      var st = document.getElementById('pg-approve-status');
      if (st) {
        st.innerHTML = 'Approved <span class="mono">' + esc(chosen) +
          '</span> \u2014 recorded as an observation.';
      }
      for (var k = 0; k < opts.length; k++) {
        var el = document.getElementById('pg-opt-' + k);
        if (el) { el.className = 'opt' + (k === i ? ' chosen' : ''); }
      }
      [].slice.call(result.querySelectorAll('.pg-use')).forEach(function (b) {
        b.disabled = true;
      });
    });
  }

  function streamChat(body, onDelta, onHead) {
    return fetch('/v1/chat/completions', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    }).then(function (r) {
      if (!r.ok) {
        return r.json().catch(function () { return null; }).then(function (j) {
          var e = (j && j.error) || {};
          var err = new Error(e.message || ('HTTP ' + r.status));
          err.type = e.type; err.status = r.status;
          throw err;
        });
      }
      // The decision envelope only exists on the non-streamed path, so the
      // parts a streamed reply still needs arrive as X-MI-* headers.
      onHead({
        deploy: r.headers.get('X-MI-Deploy') || '',
        task: r.headers.get('X-MI-Task') || body.model,
        policy: r.headers.get('X-MI-Policy') || '',
        needsApproval: r.headers.get('X-MI-Needs-Approval') === 'true'
      });
      var reader = r.body.getReader();
      var dec = new TextDecoder();
      var buf = '';
      var raw = '';
      var text = '';
      function pump() {
        return reader.read().then(function (res) {
          if (res.done) { return text; }
          var chunk = dec.decode(res.value, { stream: true });
          raw += chunk;
          buf += chunk;
          var lines = buf.split('\n');
          buf = lines.pop();
          lines.forEach(function (line) {
            var s = line.trim();
            if (s.indexOf('data:') !== 0) { return; }
            var p = s.slice(5).trim();
            if (!p || p === '[DONE]') { return; }
            try {
              var j = JSON.parse(p);
              var c = j.choices && j.choices[0] && j.choices[0].delta &&
                      j.choices[0].delta.content;
              if (c) { text += c; onDelta(text); }
            } catch (e) { /* partial frame — the next read completes it */ }
          });
          return pump();
        });
      }
      return pump().then(function (t) {
        // An upstream that ignored `stream` answers with one JSON body; left
        // alone that reads as an empty reply.
        if (!t && raw) {
          try {
            var j = JSON.parse(raw.trim());
            var m = j.choices && j.choices[0] && j.choices[0].message;
            t = (m && m.content) || '';
          } catch (e) { /* not a completion */ }
        }
        return t;
      });
    });
  }

  function send() {
    var text = prompt.value.trim();
    if (!text) { prompt.focus(); return; }
    btn.disabled = true;
    var t0 = nowMs();
    var tick = setInterval(function () {
      var el = document.getElementById('pg-timer');
      if (el) { el.textContent = Math.round(nowMs() - t0) + ' ms'; }
    }, 100);
    status.textContent = 'routing\u2026';
    var isCompare = !!(compare && compare.checked);

    function done() { clearInterval(tick); btn.disabled = false; }
    function fail(err) {
      done();
      status.textContent = 'failed';
      result.innerHTML = '<div class="error"><b>' + esc(err.type || 'error') +
        '</b> \u2014 ' + esc(err.message) + '</div>' + hint(err.type);
    }

    var body = { model: sel.value, messages: [{ role: 'user', content: text }] };
    // Without this the proxy answers with one JSON body: the reader would find
    // no SSE frames and the JSON fallback below would quietly mask it, so the
    // page would look like it worked while never streaming a byte.
    if (isCompare) { body.mi_options = 2; }
    else { body.stream = true; }

    // Stream into the card so the answer arrives as it is written, with a
    // running clock — a single blocked request cannot distinguish "slow" from
    // "stuck". Compare mode stays non-streamed: it runs both arms and returns
    // them together, so there is nothing to stream until both finish.
    result.innerHTML = '<div class="answer-card"><div class="live-row">' +
      '<span class="mono">' + esc(sel.value) + '</span>' +
      '<span class="spacer"></span>' +
      '<span class="mono timer" id="pg-timer">0 ms</span></div>' +
      '<div class="answer-body md" id="pg-live"><span class="muted">working\u2026</span></div></div>';

    if (isCompare) {
      fetch('/v1/chat/completions', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body)
      }).then(function (r) {
        return r.json().then(function (j) { return { ok: r.ok, status: r.status, body: j }; });
      }).then(function (res) {
        done();
        var ms = Math.round(nowMs() - t0);
        status.textContent = ms + ' ms';
        if (!res.ok) {
          var e = (res.body && res.body.error) || {};
          result.innerHTML = '<div class="error"><b>' + esc(e.type || 'error') +
            '</b> \u2014 ' + esc(e.message || ('HTTP ' + res.status)) + '</div>' +
            hint(e.type);
          return;
        }
        var dm = res.body.mininfer || {};
        if (dm.options && dm.options.length) {
          status.textContent = ms + ' ms \u00b7 ' + dm.options.length + ' answers';
          renderOptions(res.body, ms);
          return;
        }
        render(res.body, ms);
      }).catch(fail);
      return;
    }

    var head = null;
    streamChat(body, function (t) {
      var el = document.getElementById('pg-live');
      if (el) { el.innerHTML = md(t); }
    }, function (h) { head = h; })
      .then(function (t) {
        var ms = Math.round(nowMs() - t0);
        done();
        status.textContent = ms + ' ms';
        var taskName = (head && head.task) || sel.value;
        // Cost and leaderboards are not in the stream; the plan endpoint has
        // them without spending a second model call.
        return fetch('/v1/plan?task=' + encodeURIComponent(taskName))
          .then(function (r) { return r.ok ? r.json() : null; })
          .catch(function () { return null; })
          .then(function (plan) {
            plan = plan || { chosen: [], funnel: {}, diversity: '' };
            var chosen = plan.chosen || [];
            render({
              mininfer: {
                selected_model: (head && head.deploy) || sel.value,
                task: taskName,
                policy: (head && head.policy) || '',
                alternatives: chosen.slice(1).map(function (c) { return c.deploy_id; }),
                needs_approval: !!(head && head.needsApproval),
                reason: {
                  selected: chosen,
                  funnel: plan.funnel || {},
                  diversity: plan.diversity || '',
                  needs_approval: !!(head && head.needsApproval)
                }
              },
              choices: [{ message: { content: t } }],
              usage: {}
            }, ms);
          });
      })
      .catch(fail);
  }

  btn.addEventListener('click', send);
  prompt.addEventListener('keydown', function (e) {
    if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') { send(); }
  });
})();
"""
