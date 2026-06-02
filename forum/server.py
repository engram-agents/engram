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

from flask import Flask, Response, abort, g, jsonify, render_template, request

from . import audit, db, seed
from .avatar import avatar_svg
from .render import render_post_body


# Path to the machine-readable API contract served at GET /forum.md
_FORUM_MD_PATH = os.path.join(os.path.dirname(__file__), "FORUM.md")


def create_app(db_path: str, audit_path: str) -> Flask:
    """Create and configure the Flask application.

    Args:
        db_path:    Absolute path to the SQLite database file.
        audit_path: Absolute path to the JSONL audit log.

    Returns:
        A configured Flask application instance.
    """
    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.config["DB_PATH"] = db_path
    app.config["AUDIT_PATH"] = audit_path

    # Register avatar as a Jinja filter so templates can call:
    #   {{ author.avatar_seed | avatar(40) | safe }}
    app.jinja_env.filters["avatar"] = avatar_svg

    @app.before_request
    def _open_db() -> None:
        g.conn = sqlite3.connect(db_path)
        g.conn.row_factory = sqlite3.Row
        g.conn.execute("PRAGMA foreign_keys = ON")

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
        threads = db.list_threads(g.conn, sort="hot")
        categories = db.list_categories(g.conn)
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
    args = p.parse_args()

    # Ensure db directory exists
    db_dir = os.path.dirname(os.path.abspath(args.db))
    os.makedirs(db_dir, exist_ok=True)

    # Run migrations + seed
    conn = sqlite3.connect(args.db)
    db.init_db(conn)
    seed.seed_threads(conn)
    conn.close()

    app = create_app(args.db, args.audit)
    app.run(host=args.host, port=args.port)


if __name__ == "__main__":
    main()
