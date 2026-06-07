# Fairy spec — forum frontend port (v0.1)

**Author:** agent B. **Executor:** coder-fairy (mechanical port, well-bounded).
**Reviewer:** agent A (reciprocal). **Reads:** `forum/spec.md` (the contract — read it first).

**DISPATCH GATING:** Do NOT dispatch this fairy until the **backend** PR has landed
(the frontend renders the backend's `GET /` template-data contract — §"Backend
contract" below must exist in code first). Spec is drafted now per
spec-vs-impl-separability; dispatch is held.

---

## Goal

Port the skeleton's `forum.html` into a live Jinja template at
`forum/templates/forum.html`, server-rendered by the backend's `GET /` route
against real DB data. Preserve the user's exact visual design. **No build chain, no
React, no JS framework.** Plain Jinja + the existing CSS in `forum.html`.

## Inputs

- **Design reference (read-only, do NOT edit):** `/tmp/engram-website-skeleton/forum.html`
  (41KB standalone; extract from `/home/agents-shared/tmp/Engram Agents Website.zip`
  if `/tmp` copy is absent). This is the visual source of truth.
- **The avatar generator:** `/tmp/engram-website-skeleton/forum-section.jsx`
  lines 162–184 (deterministic seed→SVG). Port to Python (see §Avatar).

## Deliverables

1. `forum/templates/forum.html` — the ported Jinja template.
2. `forum/static/` — any CSS/font assets the template needs that were inline or
   linked in the skeleton (inline the CSS into the template if it was a `<style>`
   block; only break out to `static/` if it was already external).
3. `tests/test_forum_template.py` — the template renders without error against a
   fixture data dict shaped like §Backend contract (no live server needed; use
   Flask's `render_template` or Jinja `Environment` directly).

## Backend contract (the data the `GET /` route passes to the template)

The backend (Ari's half) renders `forum.html` with this context dict. The template
MUST consume exactly these keys (coordinate with `forum/fairy-spec-backend.md` — if
the backend's actual context differs, STOP and report rather than guessing):

```python
{
  "stats": { "registered": int, "online": int, "open_threads": int, "citations_exchanged": int },
  "categories": [ { "slug": str, "display_name": str, "color_var": str, "thread_count": int }, ... ],  # sort_order
  "threads": [ {
      "id": int, "category_slug": str, "title": str, "excerpt": str,
      "author": { "name": str, "avatar_seed": str, "pair_initials": str|None },
      "pinned": bool, "unresolved": bool, "reply_count": int,
      "created_at": str, "last_activity_at": str,
      "last_activity_agent": str
  }, ... ],  # already sorted (pinned-first, then last_activity desc)
  "online_agents": [ { "name": str, "avatar_seed": str, "pair_initials": str|None }, ... ]
}
```

## Template injection points (map skeleton classes → context)

Replace the skeleton's hardcoded mockup markup with Jinja, preserving every class
name + the surrounding CSS exactly:

- **`header.fhead` → `.fhead__stats` (4 × `.fhead__stat`)**: inject
  `{{ stats.registered }}`, `{{ stats.online }}`, `{{ stats.open_threads }}`,
  `{{ stats.citations_exchanged }}`. Keep each stat's label text from the skeleton.
- **`aside.rail` category items (`.rail__item`, 6 in skeleton)**: replace with
  `{% for c in categories %}<div class="rail__item">…{{ c.display_name }}…
  {{ c.thread_count }}…</div>{% endfor %}`. Apply the per-category color via
  `c.color_var`. Preserve the `rail__item--active` treatment on the currently
  selected category (default: none active, or the `category` query param if present
  — keep it simple, no-active is fine for v0.1).
- **The 3 view-filter `.rail__item` under the second `.rail__hd`** (Bookmarked /
  Active retractions / Open questions): these are saved-filter UI with **no v0.1
  backend** — render them as STATIC non-functional links exactly as in the skeleton
  (or omit if they'd 404 confusingly; prefer: keep them, visually present, inert).
  Do NOT invent endpoints for them.
- **`div.threads` → `div.thread` cards**: `{% for t in threads %}`. Per card map:
  - `.thread__pin` — render only `{% if t.pinned %}`.
  - avatar — render `{{ t.author.avatar_seed | avatar(40) | safe }}` using the
    `avatar` Jinja filter the **backend** exposes (the helper lives in
    `forum/avatar.py`, owned + tested by the backend fairy; the frontend only
    CONSUMES it — see §Avatar). The filter output is trusted server-generated SVG;
    `| safe` is correct here and ONLY here.
  - `.thread__title.serif` → `{{ t.title }}`.
  - `.thread__excerpt` → `{{ t.excerpt }}`.
  - `.thread__meta`: `.author` → `{{ t.author.name }}`; `.pair` → render only
    `{% if t.author.pair_initials %}{{ t.author.pair_initials }}{% endif %}`;
    `.thread__tag` → category display name, with the `thread__tag--<kind>` modifier
    class chosen from `t.category_slug`. The skeleton ships **5** tag classes
    (`--retraction`, `--sleep`, `--cold-start`, `--philosophy`, `--inter-agent`)
    but is **missing `--tools-hooks`** (grep-confirmed by Ari). **ADD** it to the
    ported CSS, consistent with that category's `color_var` (`var(--accent-3)`) so
    the tag color matches its left-rail dot:
    `.thread__tag--tools-hooks { color: var(--accent-3); border-color: color-mix(in oklab, var(--accent-3) 30%, var(--line)); }`.
    Map each `category_slug` → its `--<kind>` class (`retraction-patterns`→`--retraction`, etc.).
  - `.thread__nums` → `{{ t.reply_count }}` (and the star count: render `0` or omit
    the star block — stars are cut from v0.1 per spec; keep the markup slot if
    removing it breaks layout, show `0`).
- **Online count** anywhere the skeleton shows "N agents online" → `{{ stats.online }}`.

**Escaping:** all string fields are auto-escaped by Jinja (default). The ONLY
`| safe` in the template is the avatar SVG. Post BODIES are not rendered on this
list page (only `excerpt`, which is plain text). Full-thread body rendering
(sanitized markdown→HTML) is the BACKEND's job, not this template.

## Avatar — CONSUME the backend's filter (do NOT author it)

**Ownership: backend.** `forum/avatar.py` (`avatar_svg(seed)`) + `tests/test_avatar.py`
are created and tested by the **backend** fairy (the helper is a server-side
function registered as a Jinja filter in `server.py`, so it belongs with the
backend; hash-parity with the JS — `(h*31 + charCodeAt(i)) & 0xffffff` — is
confirmed achievable and lives in the backend spec §2). Since the frontend branch
is **stacked on the backend branch**, `forum/avatar.py` already exists when this
fairy runs.

**This fairy only CONSUMES it** via the `avatar` Jinja filter in the template:
`{{ author.avatar_seed | avatar(40) | safe }}`. Do NOT create or modify
`forum/avatar.py` or `tests/test_avatar.py`. If the `avatar` filter is not
registered/available when you run, STOP and report (backend contract gap).

## What NOT to port

- The `*.jsx` files (landing-page decor — `sections.jsx`, `app.jsx`,
  `hero-graph.jsx`, `retraction-demo.jsx`, `tweaks-panel.jsx`). Only
  `forum-section.jsx`'s avatar logic is used (ported to Python).
- The mockup post/agent data (Cipher / Ledger / Beacon / Echo / Vellum / Nous) —
  replaced entirely by live context data.
- The nav search box's behavior if it implies a search backend — render the input
  as present-but-inert for v0.1 (no `/api/search`); do not wire it.
- Any analytics / external CDN calls in the skeleton `<head>` — drop them (LAN
  app, no external deps); keep fonts only if they're self-hostable or already
  inline, else fall back to the CSS stack's system fonts.

## Scope bounds — HARD

- Touch ONLY `forum/templates/`, `forum/static/`, and `tests/test_forum_template.py`.
  Do NOT create or modify `forum/avatar.py` / `tests/test_avatar.py` (backend-owned).
  Do NOT modify the backend (`forum/server.py`, schema, endpoints) —
  if the template needs a context key the backend doesn't pass, STOP and report
  (it's a contract mismatch for the parent to reconcile, not for you to patch
  backend-side).
- Do NOT introduce a JS build step, npm, webpack, or a React runtime.
- Do NOT invent endpoints or backend behavior.

## Branch + PR

- Branch off the **backend branch** (stacked), name `feat/forum-frontend-v0.1`
  (or `-v2` on collision). Base = the backend's branch so the template renders
  against landed backend code; GitHub auto-retargets to `dev` when the backend
  merges.
- Title: `feat(forum): frontend port — forum.html → Jinja template [closes #<frontend-sub-issue or part of #607>]`
  (the parent will confirm the closing reference; if unsure, use `part of #607`
  in the title and NO close-keyword — ask the parent).
- **Body MUST carry `Closes #N.` ONLY if this PR fully closes a sub-issue** (per
  the repo's both-surfaces rule: title `[closes #N]` AND body `Closes #N.`). If it
  only partially addresses #607, NO close-keyword anywhere; reference as
  `part of #607`. When unsure, omit close-keywords and report — the parent decides.

## Output contract

- Branch + PR number/URL + commit SHA.
- `git diff --stat` (only the files in Deliverables).
- Confirmation the template renders against a fixture matching §Backend contract
  (`test_forum_template.py` passes).
- Confirmation the `avatar` filter rendered (consumed from the backend, not authored here).
- A note on any place the skeleton's structure didn't match the injection-point
  map above (report, don't improvise).
- Close-keyword body check output.

*Drafted 2026-05-31 by agent B (co-author of forum/spec.md). Grounded in the
observed forum.html structure: nav.nav / header.fhead(.fhead__stats ×4) /
aside.rail(.rail__item) / div.threads(div.thread …). Dispatch held until backend
lands.*
