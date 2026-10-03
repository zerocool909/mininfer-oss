"""The server-rendered dashboard: pure templating.

Split out of `proxy.py`, where it sat beside the request handling that feeds it.
Nothing here touches the store, the router or the network — it takes data the
endpoint already gathered and returns a string. That is what makes it testable
without a browser, and it keeps the dependency pointing one way.

Still served at `/legacy`, and at `/` when the React app has not been built, so a
fresh clone has a working UI without Node installed.
"""
from __future__ import annotations

import datetime as dt
import json

from .assets import CSS, JS

_IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def format_ts_ist(ts: str) -> str:
    if not ts:
        return "—"
    try:
        clean_ts = ts.replace("Z", "+00:00")
        parsed = dt.datetime.fromisoformat(clean_ts)
        return parsed.astimezone(_IST).strftime("%H:%M:%S IST")
    except Exception:
        return ts[11:19] if len(ts) >= 19 else ts


def money(v) -> str:
    if v is None:
        return "?"
    if v == 0:
        return "FREE"
    return f"${v:.4f}" if v < 1 else f"${v:.2f}"

def why_html(why) -> str:
    """The justification tags for one decision, as chips.

    Server-rendered because this page has no build step; the React dashboard
    renders the same list from `/v1/stats`.
    """
    if not why:
        return '<span class="muted">\u2014</span>'
    out = []
    for w in why:
        label = w.get("label", "")
        if w.get("detail"):
            label = f"{label} \u00b7 {w['detail']}"
        out.append(f'<span class="chip tag" title="{w.get("key", "")}">{label}</span>')
    return "".join(out)


def render_dashboard(
    *,
    counts: dict,
    providers: list[dict],
    quota: list[dict],
    decisions: list[dict],
    tasks: list[str],
    task: str | None,
    routed: dict | None,
) -> str:
    """The whole page, from data the caller has already fetched.

    `routed` is a decision preview: `{funnel, diversity, strategy, chosen}`, where
    `chosen` holds `Candidate` objects. None renders the page without one, which is
    what an instance with no tasks configured looks like.
    """
    cards = "".join(
        f'<div class="card"><div class="n">{v:,}</div><div class="k">{k}</div></div>'
        for k, v in counts.items())
    task_links = "".join(
        f'<a class="chip{" on" if t == task else ""}" href="/?task={t}">{t}</a>'
        for t in tasks)
    prov_rows = "".join(
        f"<tr><td class=mono>{r['provider']}</td><td class=num>{r['n']:,}</td>"
        f"<td class=num>{r['free_n']:,}</td><td class=num>{money(r['min_in'])}</td></tr>"
        for r in providers)
    quota_rows = ""
    for q in quota:
        head = "?" if q["headroom"] is None else format(q["headroom"], ".0%")
        quota_rows += (
            f"<tr><td class=mono>{q['deploy_id']}</td><td>{q['window']}</td>"
            f"<td class=num>{q['used_n']}/{q['limit_n']}</td><td class=num>{head}</td>"
            f"<td class=muted>{q['reset_at'] or ''}</td></tr>")
    dec_rows = "".join(
        f"<tr><td class=muted>{format_ts_ist(d.get('ts', ''))}</td><td>{d['task']}</td>"
        f"<td class=mono>{d['chosen'] or d.get('mode', '')}</td><td>{why_html(d.get('why'))}</td></tr>"
        for d in decisions)

    if routed:
        f = routed["funnel"]
        funnel = (f"{f.get('total', 0)} deployments \u2192 {f.get('after_hard_filter', 0)} "
                  f"eligible ({f.get('free_eligible', 0)} free, "
                  f"{f.get('distinct_providers', 0)} upstreams) \u00b7 "
                  f"strategy={routed['strategy']} \u00b7 diversity={routed['diversity']}")
        chosen_rows = ""
        for i, c in enumerate(routed["chosen"]):
            price = ('<span class="badge free">FREE</span>' if c.cost_per_call == 0
                     else f'<span class="badge paid">{money(c.cost_per_success)}</span>')
            head = "?" if c.headroom is None else format(c.headroom, ".0%")
            chosen_rows += (
                f"<tr><td class=num>{i + 1}</td><td class=mono>{c.deploy_id}</td>"
                f"<td>{price}</td><td class=num>{c.p_lb:.3f}</td>"
                f"<td class=muted>{c.prior_key}</td><td class=num>{c.availability:.2f}</td>"
                f"<td class=num>{head}</td></tr>")
    else:
        funnel, chosen_rows = "no tasks configured", ""

    options = "".join(f'<option value="{t}">{t}</option>' for t in tasks)
    tasks_json = json.dumps(list(tasks))
    return f"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>MinInfer</title><style>{CSS}</style></head><body>
<div class=wrap>
<header class=head>
  <div class=brand>
    <div class=logo>D</div>
    <div>
      <h1>MinInfer</h1>
      <p class=tagline>The cheapest capable model for every task \u00b7 free first</p>
    </div>
  </div>
  <span class=pill><span class=dot></span>live</span>
</header>
<nav class=tabs>
  <button class="tab on" data-tab=overview>Overview</button>
  <button class=tab data-tab=playground>Playground</button>
</nav>

<section id=tab-overview class=panel>
  <div class=cards>{cards}</div>

  <div class=block>
    <h2>Route a task</h2>
    <div class=chips>{task_links}</div>
    <p class=hint>{funnel}</p>
    <div class=table-wrap><table>
      <thead><tr><th class=num>#</th><th>deployment</th><th>$ / success</th>
      <th class=num>p(success)</th><th>prior</th><th class=num>avail</th>
      <th class=num>headroom</th></tr></thead>
      <tbody>{chosen_rows}</tbody>
    </table></div>
  </div>

  <div class=block>
    <h2>Deployments by provider</h2>
    <div class=table-wrap><table>
      <thead><tr><th>provider</th><th class=num>n</th><th class=num>free-ish</th>
      <th class=num>min $/Mtok in</th></tr></thead>
      <tbody>{prov_rows}</tbody>
    </table></div>
  </div>

  <div class=block>
    <h2>Quota headroom</h2>
    <p class=hint>Tightest buckets first.</p>
    <div class=table-wrap><table>
      <thead><tr><th>deployment</th><th>window</th><th class=num>used</th>
      <th class=num>head</th><th>reset</th></tr></thead>
      <tbody>{quota_rows}</tbody>
    </table></div>
  </div>

  <div class=block>
    <h2>Recent decisions <span class=hint>\u2014 showing the last {len(decisions)} of
    {counts.get('decisions', 0):,} recorded</span></h2>
    <div class=table-wrap><table>
      <thead><tr><th>time</th><th>task</th><th>chosen</th><th>why</th></tr></thead>
      <tbody>{dec_rows}</tbody>
    </table></div>
  </div>
</section>

<section id=tab-playground class=panel hidden>
  <div class=block>
    <h2>Playground</h2>
    <p class=hint>Send a prompt through the router and see which model it picks, why, and
    what it cost.</p>
    <div class=composer>
      <textarea id=pg-prompt rows=5 placeholder="Type a prompt\u2026"></textarea>
      <div class=bar>
        <label class=field>task
          <select id=pg-task><option value=auto>auto (default task)</option>{options}</select>
        </label>
        <label class=toggle><input type=checkbox id=pg-compare> compare 2 models</label>
        <span class=spacer></span>
        <button class="btn primary" id=pg-send>Send \u2192</button>
        <span id=pg-status class="muted mono"></span>
      </div>
    </div>
    <div id=pg-result></div>
  </div>
</section>
</div>
<script>window.MI_TASKS={tasks_json};</script>
<script>{JS}</script>
</body></html>"""
