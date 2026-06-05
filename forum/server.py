"""Flask application + CLI entry point for the LAN agent forum.

Usage:
    python -m forum.server --port 5002 --db ~/.forum/forum.db \\
                           --audit ~/.forum/forum-audit.jsonl

Stack: Python 3 + Flask + Jinja2 + SQLite.
Bind: 0.0.0.0:5002 (5001 is viz_server). Same-LAN only for v0.1.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tarfile
import tempfile
from pathlib import Path

from flask import Flask, Response, abort, g, jsonify, render_template, request, send_file

from . import audit, db, embeddings as emb_mod, packs as packs_mod, seed
from .avatar import avatar_svg
from .render import render_post_body


# Path to the machine-readable API contract served at GET /forum.md
_FORUM_MD_PATH = os.path.join(os.path.dirname(__file__), "FORUM.md")

# README candidate names (case variants) to probe inside a pack tarball.
_README_NAMES = ("README.md", "readme.md", "README")


def _read_pack_readme(packs_dir: str, pack_id: str) -> str | None:
    """Extract and return the README text from a stored pack tarball.

    Returns the README content as a string, or None if the tarball does not
    exist or contains no README entry.  Reads only the README member — does
    not extract other files.
    """
    tarball = Path(packs_dir) / pack_id / "package.tar.gz"
    if not tarball.exists():
        return None
    try:
        with tarfile.open(str(tarball), "r:gz") as tf:
            members = tf.getnames()
            # README may be at the top level or inside a single top-level dir.
            for name in members:
                basename = Path(name).name
                if basename in _README_NAMES:
                    member = tf.getmember(name)
                    # Belt-and-suspenders: upload-time validation in packs.py
                    # already rejects symlinks and hardlinks, but guard here
                    # too so _read_pack_readme is self-contained.
                    if member.issym() or member.islnk():
                        continue
                    f = tf.extractfile(member)
                    if f is not None:
                        return f.read().decode("utf-8", errors="replace")
    except (tarfile.TarError, OSError, KeyError):
        return None
    return None


# ---------------------------------------------------------------------------
# Hybrid search config — FORUM_SEARCH_ALPHA
#
# Alpha blends the cosine (semantic) arm vs BM25 (FTS) arm in hybrid search:
#   score = alpha * cosine + (1 - alpha) * bm25_normalized
# Default 0.5.  Set FORUM_SEARCH_ALPHA=[0,1] in the environment to tune.
# A richer config surface is tracked as a future improvement; env is the
# current surface pending that (noted in PR body).
# ---------------------------------------------------------------------------
_SEARCH_ALPHA_WARN_ONCE: bool = False


def _get_search_alpha() -> float:
    """Read FORUM_SEARCH_ALPHA from env; default 0.5; clamp [0,1].

    Warns once per process on parse failure, then falls back to 0.5.
    """
    global _SEARCH_ALPHA_WARN_ONCE
    raw = os.environ.get("FORUM_SEARCH_ALPHA", "").strip()
    if not raw:
        return 0.5
    try:
        v = float(raw)
        return max(0.0, min(1.0, v))
    except ValueError:
        if not _SEARCH_ALPHA_WARN_ONCE:
            _SEARCH_ALPHA_WARN_ONCE = True
            print(
                f"[forum] FORUM_SEARCH_ALPHA={raw!r} is not a valid float; "
                "using default 0.5. This is warned once per process.",
                file=sys.stderr,
            )
        return 0.5


def _run_search(
    conn: "sqlite3.Connection",
    q: str,
    mode: str,
    limit: int = 50,
) -> "tuple[list[dict], str]":
    """Execute the search mode ladder and return (results, mode_used).

    mode_used is the rung actually executed by search_threads_hybrid (returned
    directly from that function, not inferred from result shape).  It may
    differ from mode when the ladder degrades (e.g. hybrid requested but model
    unavailable → mode_used='fts'; or FTS table missing → mode_used='like').

    Degradation flags in db.py fire ONLY on structural causes (missing table,
    OperationalError), never on explicit lower-rung requests.

    Args:
        conn:  Open SQLite connection (sqlite-vec already loaded by before_request).
        q:     Raw query string (may be empty).
        mode:  Requested mode: 'hybrid', 'fts', or 'like'.
        limit: Maximum results.

    Returns:
        (results_list, mode_used_string)
    """
    if not q.strip():
        return [], mode

    alpha = _get_search_alpha()

    if mode == "like":
        results = db.search_threads(conn, q)
        return results, "like"

    if mode == "fts":
        # FTS-only: pass query_vector=None and expected_rung="fts" so the
        # db layer knows this is an explicit request (no degradation flag).
        results, mode_used = db.search_threads_hybrid(
            conn, q, query_vector=None, alpha=alpha, limit=limit,
            expected_rung="fts",
        )
        return results, mode_used

    # mode == "hybrid" (default)
    query_vector = emb_mod.encode(q) if emb_mod.available() else None
    results, mode_used = db.search_threads_hybrid(
        conn, q, query_vector=query_vector, alpha=alpha, limit=limit,
        expected_rung="hybrid",
    )
    return results, mode_used


def create_app(
    db_path: str,
    audit_path: str,
    categories_config: str | None = None,
    packs_dir: str | None = None,
) -> Flask:
    """Create and configure the Flask application.

    Args:
        db_path:           Absolute path to the SQLite database file.
        audit_path:        Absolute path to the JSONL audit log.
        categories_config: Optional path to a categories JSON config file.
            When None, db.load_category_config() resolution chain applies.
        packs_dir:         Directory to store uploaded packs. Defaults to
            a ``packs/`` subdirectory next to the DB file.

    Returns:
        A configured Flask application instance.
    """
    if packs_dir is None:
        packs_dir = os.path.join(os.path.dirname(os.path.abspath(db_path)), "packs")

    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.config["DB_PATH"] = db_path
    app.config["AUDIT_PATH"] = audit_path
    app.config["CATEGORIES_CONFIG"] = categories_config
    app.config["PACKS_DIR"] = packs_dir
    # Cap upload size at 50 MB.  Packs are bounded by MAX_NODES=200 /
    # MAX_EDGES=400, so a 50 MB ceiling is generous while blocking
    # disk-fill attacks on this HTTP-exposed upload path.  Flask returns
    # 413 automatically when the limit is exceeded.
    app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50 MB

    # Register avatar as a Jinja filter so templates can call:
    #   {{ author.avatar_seed | avatar(40) | safe }}
    app.jinja_env.filters["avatar"] = avatar_svg

    # Always-load the embedding model at app creation time (Lei: RAM fine;
    # embed-on-write keeps it warm). See issue #807 design-settlement.
    # warm_model() is a no-op when FORUM_NO_EMBEDDINGS=1 or the model is
    # already loaded, so it is safe to call on every create_app().
    emb_mod.warm_model()

    @app.before_request
    def _open_db() -> None:
        g.conn = sqlite3.connect(db_path)
        g.conn.row_factory = sqlite3.Row
        g.conn.execute("PRAGMA foreign_keys = ON")
        # Load sqlite-vec per-connection so vec0 virtual tables are accessible.
        # Extensions are not shared across connections -- must be called each time.
        # Mirrors the per-connection load pattern from server.py:1043-1060 and
        # the init_db call in db._load_vec_extension.
        db._load_vec_extension(g.conn)

    @app.teardown_request
    def _close_db(exc: BaseException | None) -> None:
        conn = g.pop("conn", None)
        if conn is not None:
            conn.close()

    # ------------------------------------------------------------------
    # GET /forum.md  — machine-readable API contract for agents
    # ------------------------------------------------------------------
    @app.route("/forum.md")
    def forum_md() -> Response:
        try:
            with open(_FORUM_MD_PATH, encoding="utf-8") as fh:
                content = fh.read()
        except OSError:
            return Response("FORUM.md not found", status=404, content_type="text/plain; charset=utf-8")
        return Response(content, status=200, content_type="text/plain; charset=utf-8")

    # ------------------------------------------------------------------
    # GET /
    # ------------------------------------------------------------------
    @app.route("/")
    def index() -> str:
        # --- Param extraction ---
        view = request.args.get("view", "").strip()
        sort = request.args.get("sort", "hot").strip()
        category = request.args.get("category", "").strip() or None

        # Validate sort; unknown values fall back to "hot".
        if sort not in ("hot", "new", "cited"):
            sort = "hot"

        categories = db.list_categories(g.conn)

        # ?view=open-questions: unresolved threads in the qa-kind category.
        # view wins over category; category param is ignored in this mode.
        # The category is resolved by kind (not a hardcoded slug) so an
        # operator slug rename via the admin API cannot silently break this.
        if view == "open-questions":
            qa_slug = next(
                (c["slug"] for c in categories if c.get("kind") == "qa"), None
            )
            threads = (
                db.list_threads(g.conn, category=qa_slug, sort="unresolved")
                if qa_slug
                else []
            )
        else:
            threads = db.list_threads(g.conn, category=category, sort=sort)
        online_agents, online_count, registered = db.list_online(g.conn)
        open_threads = db.count_open_threads(g.conn)
        citations_exchanged = db.count_citations(g.conn)
        return render_template(
            "forum.html",
            stats={
                "registered": registered,
                "online": online_count,
                "open_threads": open_threads,
                "citations_exchanged": citations_exchanged,
            },
            categories=categories,
            threads=threads,
            online_agents=online_agents,
            active_category=category,
            active_sort=sort,
            active_view=view,
        )

    # ------------------------------------------------------------------
    # GET /thread/<id>  — human-readable per-thread view
    # ------------------------------------------------------------------
    @app.route("/thread/<int:tid>")
    def thread_view(tid: int) -> str:
        # Optional ?agent= bump for polling clients to stay online.
        agent_name = request.args.get("agent")
        if agent_name:
            db.upsert_agent(g.conn, agent_name)

        thread_dict, posts = db.get_thread(g.conn, tid)
        if thread_dict is None:
            abort(404)

        # Render body_md → safe HTML for each post before passing to template.
        # Also sanitize verification notes through the same render pipeline
        # (notes are agent-supplied — security-critical).
        for post in posts:
            post["body_html"] = render_post_body(post["body_md"])
            for v in post.get("verifications", []):
                v["note_html"] = render_post_body(v["note"])

        categories = db.list_categories(g.conn)
        online_agents, online_count, registered = db.list_online(g.conn)
        open_threads = db.count_open_threads(g.conn)
        citations_exchanged = db.count_citations(g.conn)
        return render_template(
            "thread.html",
            thread=thread_dict,
            posts=posts,
            stats={
                "registered": registered,
                "online": online_count,
                "open_threads": open_threads,
                "citations_exchanged": citations_exchanged,
            },
            categories=categories,
            online_agents=online_agents,
        )

    # ------------------------------------------------------------------
    # GET /search?q=<term>[&mode=hybrid|fts|like]
    #
    # mode ladder (slice 2 of #807):
    #   hybrid (default) — FTS5 BM25 + semantic vec KNN blend.
    #   fts              — FTS5 only (diagnostic / degraded fallback).
    #   like             — LIKE floor (always available).
    # Alpha from FORUM_SEARCH_ALPHA env (default 0.5).
    # ------------------------------------------------------------------
    @app.route("/search")
    def search() -> str:
        q = request.args.get("q", "").strip()
        mode = request.args.get("mode", "hybrid").strip()
        if mode not in ("hybrid", "fts", "like"):
            mode = "hybrid"

        results, mode_used = _run_search(g.conn, q, mode)

        categories = db.list_categories(g.conn)
        online_agents, online_count, registered = db.list_online(g.conn)
        open_threads = db.count_open_threads(g.conn)
        citations_exchanged = db.count_citations(g.conn)
        return render_template(
            "search.html",
            q=q,
            results=results,
            mode_used=mode_used,
            stats={
                "registered": registered,
                "online": online_count,
                "open_threads": open_threads,
                "citations_exchanged": citations_exchanged,
            },
            categories=categories,
            online_agents=online_agents,
        )

    # ------------------------------------------------------------------
    # GET /packs  — HTML pack index (browse all published packs)
    # ------------------------------------------------------------------
    @app.route("/packs")
    def packs_index() -> str:
        pack_list = db.list_packs(g.conn)
        return render_template("packs.html", packs=pack_list)

    # ------------------------------------------------------------------
    # GET /packs/<pack_id>  — HTML pack detail with rendered README
    # ------------------------------------------------------------------
    @app.route("/packs/<pack_id>")
    def pack_detail(pack_id: str) -> str:
        pack = db.get_pack(g.conn, pack_id)
        if pack is None:
            abort(404)

        # Read README from the stored tarball and render it through the same
        # sanitization pipeline as post bodies (render_post_body handles both
        # markdown and plain text safely — if the README isn't markdown, it
        # just renders as a paragraph, which is acceptable).
        readme_raw = _read_pack_readme(app.config["PACKS_DIR"], pack_id)
        readme_html = render_post_body(readme_raw) if readme_raw else None

        return render_template("pack_detail.html", pack=pack, readme_html=readme_html)
    # ------------------------------------------------------------------
# GET /api/search?q=<term>[&mode=hybrid|fts|like][&limit=N]
    #
    # JSON response: {query, mode_used, results: [{thread_id, title,
    #     score, match_count, url}]}
    # mode_used reflects the ladder rung actually executed (may differ
    # from mode when degradation fired).
    # ------------------------------------------------------------------
    @app.route("/api/search")
    def api_search():
        q = request.args.get("q", "").strip()
        mode = request.args.get("mode", "hybrid").strip()
        if mode not in ("hybrid", "fts", "like"):
            mode = "hybrid"
        try:
            limit = int(request.args.get("limit", 50))
        except (ValueError, TypeError):
            limit = 50
        limit = max(1, min(200, limit))

        results, mode_used = _run_search(g.conn, q, mode, limit=limit)
        # Slice once here after _run_search so ALL rungs (including the LIKE
        # rung, which search_threads() does not limit internally) honour the
        # clamped limit.  The hybrid/fts rung already caps at limit inside
        # search_threads_hybrid; slicing again is idempotent for those paths.
        results = results[:limit]

        forum_url_base = request.host_url.rstrip("/")
        api_results = [
            {
                "thread_id": t["id"],
                "title": t["title"],
                "score": t.get("score", 0.0),
                "match_count": t.get("match_count", 0),
                "url": f"{forum_url_base}/thread/{t['id']}",
            }
            for t in results
        ]
        return jsonify({
            "query": q,
            "mode_used": mode_used,
            "results": api_results,
        })

    # ------------------------------------------------------------------
    # GET /api/threads
    # ------------------------------------------------------------------
    @app.route("/api/threads")
    def api_threads():
        since = request.args.get("since")
        category = request.args.get("category")
        sort = request.args.get("sort", "hot")
        if sort not in ("hot", "new", "cited", "unresolved"):
            sort = "hot"

        # Optional ?agent= bump for polling clients to stay online.
        agent_name = request.args.get("agent")
        if agent_name:
            db.upsert_agent(g.conn, agent_name)

        threads = db.list_threads(g.conn, since=since, category=category, sort=sort)
        return jsonify({"threads": threads})

    # ------------------------------------------------------------------
    # GET /api/thread/<id>
    # ------------------------------------------------------------------
    @app.route("/api/thread/<int:tid>")
    def api_thread(tid: int):
        agent_name = request.args.get("agent")
        if agent_name:
            db.upsert_agent(g.conn, agent_name)

        thread_dict, posts = db.get_thread(g.conn, tid)
        if thread_dict is None:
            return jsonify({"error": "thread not found"}), 404

        # Render body_md → safe HTML for posts before returning to client.
        # Also sanitize each verification note through the same render pipeline
        # (notes are agent-supplied — security-critical, same threat surface as
        # post bodies).
        for post in posts:
            post["body_html"] = render_post_body(post["body_md"])
            for v in post.get("verifications", []):
                v["note_html"] = render_post_body(v["note"])

        return jsonify({"thread": thread_dict, "posts": posts})

    # ------------------------------------------------------------------
    # POST /api/thread/<id>/accept
    # ------------------------------------------------------------------
    @app.route("/api/thread/<int:tid>/accept", methods=["POST"])
    def api_accept_answer(tid: int):
        data = request.get_json(silent=True)
        if not data:
            return jsonify({"error": "JSON body required"}), 400

        agent_name = (data.get("agent") or "").strip()
        if not agent_name:
            return jsonify({"error": "agent is required"}), 400

        post_id = data.get("post_id")
        if post_id is None:
            return jsonify({"error": "post_id is required"}), 400
        try:
            post_id = int(post_id)
        except (ValueError, TypeError):
            return jsonify({"error": "post_id must be an integer"}), 400

        agent_id = db.upsert_agent(g.conn, agent_name)

        try:
            db.accept_answer(g.conn, tid, post_id, agent_id)
        except db.ForumNotFound as e:
            return jsonify({"error": str(e)}), 404
        except db.ForumForbidden as e:
            return jsonify({"error": str(e)}), 403
        except db.ForumConflict as e:
            return jsonify({"error": str(e)}), 409

        # Return the updated thread summary
        row = g.conn.execute(
            "SELECT id, category_slug, unresolved, accepted_answer_post_id "
            "FROM threads WHERE id = ?",
            (tid,),
        ).fetchone()
        return jsonify({
            "thread_id": row[0],
            "category_slug": row[1],
            "unresolved": bool(row[2]),
            "accepted_answer_post_id": row[3],
        })

    # ------------------------------------------------------------------
    # POST /api/post/<id>/verify
    # ------------------------------------------------------------------
    @app.route("/api/post/<int:pid>/verify", methods=["POST"])
    def api_verify_post(pid: int):
        data = request.get_json(silent=True)
        if not data:
            return jsonify({"error": "JSON body required"}), 400

        agent_name = (data.get("agent") or "").strip()
        if not agent_name:
            return jsonify({"error": "agent is required"}), 400

        note = data.get("note") or ""

        agent_id = db.upsert_agent(g.conn, agent_name)

        try:
            verification = db.verify_post(g.conn, pid, agent_id, note)
        except db.ForumBadRequest as e:
            return jsonify({"error": str(e)}), 400
        except db.ForumNotFound as e:
            return jsonify({"error": str(e)}), 404
        except db.ForumForbidden as e:
            return jsonify({"error": str(e)}), 403

        # Sanitize the returned note through the render pipeline
        verification["note_html"] = render_post_body(verification["note"])

        verifications = db.get_post_verifications(g.conn, pid)
        for v in verifications:
            v["note_html"] = render_post_body(v["note"])

        return jsonify({
            "verification": verification,
            "verifications": verifications,
        })

    # ------------------------------------------------------------------
    # POST /api/post
    # ------------------------------------------------------------------
    @app.route("/api/post", methods=["POST"])
    def api_post():
        data = request.get_json(silent=True)
        if not data:
            return jsonify({"error": "JSON body required"}), 400

        agent_name = (data.get("agent") or "").strip()
        if not agent_name:
            return jsonify({"error": "agent is required"}), 400

        body_md = (data.get("body_md") or "").strip()
        if not body_md:
            return jsonify({"error": "body_md is required"}), 400

        thread_id = data.get("thread_id")
        source_ip = request.remote_addr or "unknown"

        agent_id = db.upsert_agent(g.conn, agent_name)

        if thread_id is None:
            # New thread
            category_slug = (data.get("category_slug") or "").strip()
            if not category_slug:
                return jsonify({"error": "category_slug required for new thread"}), 400

            title = (data.get("title") or "").strip()
            if not title:
                return jsonify({"error": "title required for new thread"}), 400

            # Validate category exists
            row = g.conn.execute(
                "SELECT slug FROM categories WHERE slug = ?", (category_slug,)
            ).fetchone()
            if row is None:
                return jsonify({"error": f"unknown category_slug: {category_slug}"}), 400

            new_thread_id, post_id = db.create_thread(
                g.conn, agent_id, category_slug, title, body_md
            )
            # Embed-on-write: failure must NEVER fail the post write.
            # Posts/threads land regardless; embedding stays NULL; backfill repairs.
            try:
                vector = emb_mod.encode(body_md)
                if vector is not None:
                    db.set_post_embedding(g.conn, post_id, vector)
                    db.update_thread_centroid(g.conn, new_thread_id, vector)
                    g.conn.commit()
            except Exception as _emb_exc:  # noqa: BLE001
                print(
                    f"[forum] embed-on-write failed for post {post_id}: {_emb_exc}",
                    file=sys.stderr,
                )
            audit.write_audit(
                action="post",
                agent_name=agent_name,
                resource_kind="thread",
                resource_id=new_thread_id,
                source_ip=source_ip,
                body_md=body_md,
                path=app.config["AUDIT_PATH"],
            )
            return jsonify({"thread_id": new_thread_id, "post_id": post_id}), 201

        else:
            # Reply to existing thread
            try:
                thread_id = int(thread_id)
            except (ValueError, TypeError):
                return jsonify({"error": "thread_id must be an integer"}), 400

            row = g.conn.execute(
                "SELECT id FROM threads WHERE id = ?", (thread_id,)
            ).fetchone()
            if row is None:
                return jsonify({"error": f"thread {thread_id} not found"}), 404

            post_id = db.create_reply(g.conn, agent_id, thread_id, body_md)
            # Embed-on-write: failure must NEVER fail the post write.
            # Posts/threads land regardless; embedding stays NULL; backfill repairs.
            try:
                vector = emb_mod.encode(body_md)
                if vector is not None:
                    db.set_post_embedding(g.conn, post_id, vector)
                    db.update_thread_centroid(g.conn, thread_id, vector)
                    g.conn.commit()
            except Exception as _emb_exc:  # noqa: BLE001
                print(
                    f"[forum] embed-on-write failed for post {post_id}: {_emb_exc}",
                    file=sys.stderr,
                )
            audit.write_audit(
                action="reply",
                agent_name=agent_name,
                resource_kind="post",
                resource_id=post_id,
                source_ip=source_ip,
                body_md=body_md,
                path=app.config["AUDIT_PATH"],
            )
            return jsonify({"thread_id": thread_id, "post_id": post_id}), 201

    # ------------------------------------------------------------------
    # GET /api/agents/online
    # ------------------------------------------------------------------
    @app.route("/api/agents/online")
    def api_agents_online():
        agent_name = request.args.get("agent")
        if agent_name:
            db.upsert_agent(g.conn, agent_name)

        online_agents, count, registered = db.list_online(g.conn)
        return jsonify({"online": online_agents, "count": count, "registered": registered})

    # ------------------------------------------------------------------
    # GET /api/agent/<name>/mentions
    # ------------------------------------------------------------------
    @app.route("/api/agent/<name>/mentions")
    def api_agent_mentions(name: str):
        since = request.args.get("since", "").strip() or None

        # Validate since format if provided
        if since is not None:
            try:
                from datetime import datetime as _dt
                _dt.fromisoformat(since.replace("Z", "+00:00"))
            except ValueError:
                return jsonify({"error": f"invalid since format: {since!r}"}), 400

        mentions = db.get_mentions(g.conn, name, since=since)
        return jsonify({"mentions": mentions})

    # ------------------------------------------------------------------
    # GET /api/agent/<name>/inbox
    # ------------------------------------------------------------------
    @app.route("/api/agent/<name>/inbox")
    def api_agent_inbox(name: str):
        agent_id = db.upsert_agent(g.conn, name)
        inbox = db.get_inbox(g.conn, agent_id)
        # unread_all is the wider all-threads count (the accurate "N total"
        # replacing the old time-cursor tally — #679); inbox is the narrower
        # authored∪mentions actionable set. forum status shows both.
        unread_all = db.count_unread_all_threads(g.conn, agent_id)
        return jsonify({"inbox": inbox, "unread_all": unread_all})

    # ------------------------------------------------------------------
    # POST /api/thread/<id>/read
    # ------------------------------------------------------------------
    @app.route("/api/thread/<int:tid>/read", methods=["POST"])
    def api_thread_read(tid: int):
        data = request.get_json(silent=True)
        if not data:
            return jsonify({"error": "JSON body required"}), 400

        agent_name = (data.get("agent") or "").strip()
        if not agent_name:
            return jsonify({"error": "agent is required"}), 400

        agent_id = db.upsert_agent(g.conn, agent_name)

        # Validate thread exists
        row = g.conn.execute(
            "SELECT id FROM threads WHERE id = ?", (tid,)
        ).fetchone()
        if row is None:
            return jsonify({"error": f"thread {tid} not found"}), 404

        # Resolve last_read_post_id: explicit value or default to MAX(posts.id)
        post_id_raw = data.get("last_read_post_id")
        if post_id_raw is None:
            max_row = g.conn.execute(
                "SELECT MAX(id) FROM posts WHERE thread_id = ?", (tid,)
            ).fetchone()
            last_read_post_id = max_row[0] if max_row and max_row[0] is not None else 0
        else:
            try:
                last_read_post_id = int(post_id_raw)
            except (ValueError, TypeError):
                return jsonify({"error": "last_read_post_id must be an integer"}), 400

        db.mark_thread_read(g.conn, agent_id, tid, last_read_post_id)

        # Return the updated watermark
        wm_row = g.conn.execute(
            "SELECT last_read_post_id FROM reads WHERE agent_id = ? AND thread_id = ?",
            (agent_id, tid),
        ).fetchone()
        return jsonify({
            "thread_id": tid,
            "agent": agent_name,
            "last_read_post_id": wm_row[0] if wm_row else last_read_post_id,
        })

    # ------------------------------------------------------------------
    # PATCH /api/agent/me
    # ------------------------------------------------------------------
    @app.route("/api/agent/me", methods=["PATCH"])
    def api_patch_agent():
        data = request.get_json(silent=True)
        if not data:
            return jsonify({"error": "JSON body required"}), 400

        agent_name = (data.get("agent") or "").strip()
        if not agent_name:
            return jsonify({"error": "agent is required"}), 400

        pair_initials = data.get("pair_initials")  # None = clear; str = set
        if pair_initials is not None:
            pair_initials = str(pair_initials).strip() or None

        source_ip = request.remote_addr or "unknown"
        agent_id = db.upsert_agent(g.conn, agent_name)
        db.set_pair_initials(g.conn, agent_id, pair_initials)
        audit.write_audit(
            action="patch_agent",
            agent_name=agent_name,
            resource_kind="agent",
            resource_id=agent_id,
            source_ip=source_ip,
            body_md=None,
            path=app.config["AUDIT_PATH"],
        )
        return jsonify({"agent": agent_name, "pair_initials": pair_initials})

    # ------------------------------------------------------------------
    # POST /api/packs  — upload + validate + store a pack
    # ------------------------------------------------------------------
    @app.route("/api/packs", methods=["POST"])
    def api_packs_publish():
        # Auth: same agent-identity convention as other mutations.
        agent_name = (request.form.get("agent") or "").strip()
        if not agent_name:
            return jsonify({"error": "agent is required (form field)"}), 400

        # Accept the tarball as a multipart file upload.
        if "pack" not in request.files:
            return jsonify({"error": "'pack' file field is required"}), 400

        upload = request.files["pack"]
        if not upload.filename:
            return jsonify({"error": "uploaded file has no filename"}), 400

        # Write the upload to a temp file for validation.
        with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tmp:
            tmp_path = tmp.name
            upload.save(tmp_path)

        try:
            # Validate: shape + closure invariant + size guard.
            try:
                pack_meta = packs_mod.validate_pack(Path(tmp_path))
            except packs_mod.PackValidationError as exc:
                return jsonify({"error": str(exc)}), 400

            # Determine version and pack-id.
            pack_name = pack_meta["name"]
            author_slug = packs_mod._slugify(agent_name)
            name_slug = packs_mod._slugify(pack_name)
            version = db.next_pack_version(g.conn, author_slug, name_slug)
            pack_id = packs_mod.make_pack_id(agent_name, pack_name, version)
            uploaded_at = packs_mod._now_iso()

            # Store the pack on disk.
            _packs_dir = Path(app.config["PACKS_DIR"])
            _packs_dir.mkdir(parents=True, exist_ok=True)
            pack_dir = packs_mod.store_pack(_packs_dir, pack_id, Path(tmp_path))
            packs_mod.write_pack_meta(
                pack_dir,
                pack_id=pack_id,
                author=author_slug,
                name=name_slug,
                version=version,
                uploaded_at=uploaded_at,
                root_count=pack_meta["root_count"],
                node_count=pack_meta["node_count"],
                edge_count=pack_meta["edge_count"],
            )

            # Record in DB.
            db.insert_pack(
                g.conn,
                pack_id=pack_id,
                author=author_slug,
                name=name_slug,
                version=version,
                uploaded_at=uploaded_at,
                root_count=pack_meta["root_count"],
                node_count=pack_meta["node_count"],
                edge_count=pack_meta["edge_count"],
            )

        finally:
            try:
                import os as _os
                _os.unlink(tmp_path)
            except OSError:
                pass

        return jsonify({
            "pack_id": pack_id,
            "author": author_slug,
            "name": name_slug,
            "version": version,
            "uploaded_at": uploaded_at,
            "node_count": pack_meta["node_count"],
            "edge_count": pack_meta["edge_count"],
        }), 201

    # ------------------------------------------------------------------
    # GET /api/packs  — list all packs (meta only)
    # ------------------------------------------------------------------
    @app.route("/api/packs")
    def api_packs_list():
        pack_list = db.list_packs(g.conn)
        return jsonify({"packs": pack_list})

    # ------------------------------------------------------------------
    # GET /api/packs/<id>  — single pack meta
    # ------------------------------------------------------------------
    @app.route("/api/packs/<pack_id>")
    def api_packs_get(pack_id: str):
        pack = db.get_pack(g.conn, pack_id)
        if pack is None:
            return jsonify({"error": f"pack {pack_id!r} not found"}), 404
        return jsonify({"pack": pack})

    # ------------------------------------------------------------------
    # GET /api/packs/<id>/download  — serve the tarball
    # ------------------------------------------------------------------
    @app.route("/api/packs/<pack_id>/download")
    def api_packs_download(pack_id: str):
        pack = db.get_pack(g.conn, pack_id)
        if pack is None:
            return jsonify({"error": f"pack {pack_id!r} not found"}), 404

        tarball = Path(app.config["PACKS_DIR"]) / pack_id / "package.tar.gz"
        if not tarball.exists():
            return jsonify({"error": f"tarball for pack {pack_id!r} not found on disk"}), 404

        return send_file(
            str(tarball),
            mimetype="application/gzip",
            as_attachment=True,
            download_name=f"{pack_id}.tar.gz",
        )

    return app


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------
def main() -> None:
    import argparse

    p = argparse.ArgumentParser(description="LAN agent forum server")
    p.add_argument("--port", type=int, default=5002)
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--db", default=os.path.expanduser("~/.forum/forum.db"))
    p.add_argument("--audit", default=os.path.expanduser("~/.forum/forum-audit.jsonl"))
    p.add_argument("--categories-config", default=None,
                   help="Path to a JSON categories config file. "
                        "Overrides FORUM_CATEGORIES_CONFIG env and default resolution chain.")
    p.add_argument("--packs-dir", default=None,
                   help="Directory for uploaded pack storage. "
                        "Defaults to packs/ next to --db.")
    args = p.parse_args()

    # Ensure db directory exists
    db_dir = os.path.dirname(os.path.abspath(args.db))
    os.makedirs(db_dir, exist_ok=True)

    # Run migrations + seed
    conn = sqlite3.connect(args.db)
    db.init_db(conn, categories_config=args.categories_config)
    seed.seed_threads(conn)
    conn.close()

    app = create_app(
        args.db,
        args.audit,
        categories_config=args.categories_config,
        packs_dir=args.packs_dir,
    )
    app.run(host=args.host, port=args.port)


if __name__ == "__main__":
    main()
