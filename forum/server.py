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

from flask import Flask, g, jsonify, render_template, request

from . import audit, db, seed
from .avatar import avatar_svg
from .render import render_post_body


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
        for post in posts:
            post["body_html"] = render_post_body(post["body_md"])

        return jsonify({"thread": thread_dict, "posts": posts})

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
