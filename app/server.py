import os
import sqlite3
import time
import uuid
from pathlib import Path

from flask import Flask, g, jsonify, request, send_from_directory

DB_PATH = os.environ.get("DB_PATH", "/data/stacklog.db")
STATIC_DIR = Path(__file__).parent / "static"

app = Flask(__name__, static_folder=None)

DEFAULT_COLOR = "#5fb3a8"


def get_db():
    if "db" not in g:
        Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS projects (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            color TEXT NOT NULL DEFAULT '#5fb3a8',
            tag TEXT NOT NULL DEFAULT '',
            created_at INTEGER NOT NULL,
            order_index INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    # migration: add order_index to a projects table created before this column existed
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(projects)")}
    if "order_index" not in existing_cols:
        conn.execute("ALTER TABLE projects ADD COLUMN order_index INTEGER NOT NULL DEFAULT 0")
        for i, row in enumerate(conn.execute("SELECT id FROM projects ORDER BY created_at ASC")):
            conn.execute("UPDATE projects SET order_index = ? WHERE id = ?", (i, row[0]))
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            title TEXT NOT NULL,
            done INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL
        )
        """
    )
    conn.commit()

    count = conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
    if count == 0:
        now = int(time.time() * 1000)
        demo_project_id = "demo-project"
        conn.execute(
            "INSERT INTO projects (id, name, color, tag, created_at) VALUES (?, ?, ?, ?, ?)",
            (demo_project_id, "Projeto de exemplo", DEFAULT_COLOR, "exemplo", now),
        )
        demo_tasks = [
            "Clique em + Novo projeto pra criar o seu",
            "Marque esta tarefa como concluída pra testar",
            "Apague este projeto de exemplo quando terminar",
        ]
        for i, title in enumerate(demo_tasks):
            conn.execute(
                "INSERT INTO tasks (id, project_id, title, done, created_at) VALUES (?, ?, ?, 0, ?)",
                (f"demo-task-{i}", demo_project_id, title, now + i),
            )
        conn.commit()
    conn.close()


# ---------- projects ----------

@app.get("/api/projects")
def list_projects():
    rows = get_db().execute("SELECT * FROM projects ORDER BY order_index ASC, created_at ASC").fetchall()
    return jsonify([dict(r) for r in rows])


@app.post("/api/projects")
def create_project():
    body = request.get_json(force=True, silent=True) or {}
    name = (body.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name is required"}), 400
    color = (body.get("color") or DEFAULT_COLOR).strip()
    tag = (body.get("tag") or "").strip()
    project_id = uuid.uuid4().hex[:12]
    now = int(time.time() * 1000)
    db = get_db()
    next_order = db.execute("SELECT COALESCE(MAX(order_index), -1) + 1 FROM projects").fetchone()[0]
    db.execute(
        "INSERT INTO projects (id, name, color, tag, created_at, order_index) VALUES (?, ?, ?, ?, ?, ?)",
        (project_id, name, color, tag, now, next_order),
    )
    db.commit()
    return jsonify({"id": project_id, "name": name, "color": color, "tag": tag, "created_at": now, "order_index": next_order}), 201


@app.patch("/api/projects/<project_id>")
def update_project(project_id):
    body = request.get_json(force=True, silent=True) or {}
    fields, values = [], []
    for key in ("name", "color", "tag"):
        if key in body:
            fields.append(f"{key} = ?")
            values.append((body[key] or "").strip())
    if "order_index" in body:
        try:
            fields.append("order_index = ?")
            values.append(int(body["order_index"]))
        except (TypeError, ValueError):
            return jsonify({"error": "order_index must be an integer"}), 400
    if not fields:
        return jsonify({"error": "nothing to update"}), 400
    values.append(project_id)
    db = get_db()
    cur = db.execute(f"UPDATE projects SET {', '.join(fields)} WHERE id = ?", values)
    db.commit()
    if cur.rowcount == 0:
        return jsonify({"error": "not found"}), 404
    return jsonify({"ok": True})


@app.delete("/api/projects/<project_id>")
def delete_project(project_id):
    db = get_db()
    db.execute("DELETE FROM tasks WHERE project_id = ?", (project_id,))
    cur = db.execute("DELETE FROM projects WHERE id = ?", (project_id,))
    db.commit()
    if cur.rowcount == 0:
        return jsonify({"error": "not found"}), 404
    return jsonify({"ok": True})


# ---------- tasks ----------

@app.get("/api/tasks")
def list_tasks():
    rows = get_db().execute("SELECT * FROM tasks ORDER BY created_at ASC").fetchall()
    return jsonify([dict(r) | {"done": bool(r["done"])} for r in rows])


@app.post("/api/tasks")
def create_task():
    body = request.get_json(force=True, silent=True) or {}
    project_id = (body.get("project_id") or "").strip()
    title = (body.get("title") or "").strip()
    if not project_id or not title:
        return jsonify({"error": "project_id and title are required"}), 400
    exists = get_db().execute("SELECT 1 FROM projects WHERE id = ?", (project_id,)).fetchone()
    if not exists:
        return jsonify({"error": "project not found"}), 404
    task_id = uuid.uuid4().hex[:12]
    now = int(time.time() * 1000)
    get_db().execute(
        "INSERT INTO tasks (id, project_id, title, done, created_at) VALUES (?, ?, ?, 0, ?)",
        (task_id, project_id, title, now),
    )
    get_db().commit()
    return jsonify({"id": task_id, "project_id": project_id, "title": title, "done": False, "created_at": now}), 201


@app.patch("/api/tasks/<task_id>")
def update_task(task_id):
    body = request.get_json(force=True, silent=True) or {}
    if "done" not in body:
        return jsonify({"error": "done is required"}), 400
    db = get_db()
    cur = db.execute("UPDATE tasks SET done = ? WHERE id = ?", (1 if body["done"] else 0, task_id))
    db.commit()
    if cur.rowcount == 0:
        return jsonify({"error": "not found"}), 404
    return jsonify({"ok": True})


@app.delete("/api/tasks/<task_id>")
def delete_task(task_id):
    db = get_db()
    cur = db.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    db.commit()
    if cur.rowcount == 0:
        return jsonify({"error": "not found"}), 404
    return jsonify({"ok": True})


# ---------- static ----------

@app.get("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.get("/<path:filename>")
def static_files(filename):
    return send_from_directory(STATIC_DIR, filename)


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
