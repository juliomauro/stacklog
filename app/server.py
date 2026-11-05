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
        CREATE TABLE IF NOT EXISTS categories (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            order_index INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS projects (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            color TEXT NOT NULL DEFAULT '#5fb3a8',
            tag TEXT NOT NULL DEFAULT '',
            created_at INTEGER NOT NULL,
            order_index INTEGER NOT NULL DEFAULT 0,
            category_id TEXT
        )
        """
    )
    # migrations: columns added to a projects table created before they existed
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(projects)")}
    if "order_index" not in existing_cols:
        conn.execute("ALTER TABLE projects ADD COLUMN order_index INTEGER NOT NULL DEFAULT 0")
        for i, row in enumerate(conn.execute("SELECT id FROM projects ORDER BY created_at ASC")):
            conn.execute("UPDATE projects SET order_index = ? WHERE id = ?", (i, row[0]))
    if "category_id" not in existing_cols:
        conn.execute("ALTER TABLE projects ADD COLUMN category_id TEXT")
    if "archived_at" not in existing_cols:
        conn.execute("ALTER TABLE projects ADD COLUMN archived_at INTEGER")
    if "link" not in existing_cols:
        conn.execute("ALTER TABLE projects ADD COLUMN link TEXT NOT NULL DEFAULT ''")
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
    # migrations: done_at (when a task was checked) and note (free-text observation;
    # first shipped as waiting_on, renamed in place so existing values are kept)
    task_cols = {row[1] for row in conn.execute("PRAGMA table_info(tasks)")}
    if "done_at" not in task_cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN done_at INTEGER")
    if "note" not in task_cols:
        if "waiting_on" in task_cols:
            conn.execute("ALTER TABLE tasks RENAME COLUMN waiting_on TO note")
        else:
            conn.execute("ALTER TABLE tasks ADD COLUMN note TEXT NOT NULL DEFAULT ''")
    if "order_index" not in task_cols:
        # manual order inside a project; starts as the creation order
        conn.execute("ALTER TABLE tasks ADD COLUMN order_index INTEGER NOT NULL DEFAULT 0")
        rows = conn.execute("SELECT id, project_id FROM tasks ORDER BY project_id, created_at ASC").fetchall()
        pos = {}
        for task_id, project_id in rows:
            pos[project_id] = pos.get(project_id, -1) + 1
            conn.execute("UPDATE tasks SET order_index = ? WHERE id = ?", (pos[project_id], task_id))
    conn.commit()

    count =conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
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
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tags (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            created_at INTEGER NOT NULL
        )
        """
    )
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS tags_name_nocase ON tags (name COLLATE NOCASE)")
    # first run with the registry: seed it from the free-text tags projects already use
    if conn.execute("SELECT COUNT(*) FROM tags").fetchone()[0] == 0:
        now = int(time.time() * 1000)
        for (name,) in conn.execute("SELECT DISTINCT TRIM(tag) FROM projects WHERE TRIM(tag) != ''").fetchall():
            conn.execute("INSERT OR IGNORE INTO tags (id, name, created_at) VALUES (?, ?, ?)",
                         (uuid.uuid4().hex[:12], name, now))
    # keep project tags trimmed and spelled like the registry (Ex: " vmp " -> "VMP")
    conn.execute("UPDATE projects SET tag = TRIM(tag) WHERE tag != TRIM(tag)")
    conn.execute(
        "UPDATE projects SET tag = (SELECT t.name FROM tags t WHERE t.name = projects.tag COLLATE NOCASE) "
        "WHERE tag != '' AND EXISTS (SELECT 1 FROM tags t WHERE t.name = projects.tag COLLATE NOCASE AND t.name != projects.tag)"
    )
    conn.commit()
    conn.close()


def normalize_link(value):
    """Empty stays empty; a bare host/path gets https:// so it opens as a link."""
    v = (value or "").strip()
    if v and "://" not in v:
        v = "https://" + v
    return v


# ---------- tags ----------

def ensure_tag(db, name):
    """Returns the registry spelling of a tag, adding it to the registry when new."""
    name = (name or "").strip()
    if not name:
        return ""
    row = db.execute("SELECT name FROM tags WHERE name = ? COLLATE NOCASE", (name,)).fetchone()
    if row:
        return row["name"]
    db.execute("INSERT INTO tags (id, name, created_at) VALUES (?, ?, ?)",
               (uuid.uuid4().hex[:12], name, int(time.time() * 1000)))
    return name


def tag_json(db, r):
    n = db.execute("SELECT COUNT(*) FROM projects WHERE tag = ? COLLATE NOCASE", (r["name"],)).fetchone()[0]
    return dict(r) | {"count": n}


@app.get("/api/tags")
def list_tags():
    db = get_db()
    rows = db.execute("SELECT * FROM tags ORDER BY name COLLATE NOCASE ASC").fetchall()
    return jsonify([tag_json(db, r) for r in rows])


@app.post("/api/tags")
def create_tag():
    body = request.get_json(force=True, silent=True) or {}
    name = (body.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name is required"}), 400
    db = get_db()
    existing = db.execute("SELECT * FROM tags WHERE name = ? COLLATE NOCASE", (name,)).fetchone()
    if existing:
        return jsonify(tag_json(db, existing) | {"error": "tag already exists"}), 409
    tag_id = uuid.uuid4().hex[:12]
    db.execute("INSERT INTO tags (id, name, created_at) VALUES (?, ?, ?)", (tag_id, name, int(time.time() * 1000)))
    db.commit()
    return jsonify(tag_json(db, db.execute("SELECT * FROM tags WHERE id = ?", (tag_id,)).fetchone())), 201


@app.patch("/api/tags/<tag_id>")
def rename_tag(tag_id):
    """Renames a tag and every project using it. Renaming onto another existing
    tag merges the two (Ex: VPM -> VMP)."""
    body = request.get_json(force=True, silent=True) or {}
    name = (body.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name is required"}), 400
    db = get_db()
    tag = db.execute("SELECT * FROM tags WHERE id = ?", (tag_id,)).fetchone()
    if not tag:
        return jsonify({"error": "not found"}), 404
    other = db.execute("SELECT * FROM tags WHERE name = ? COLLATE NOCASE AND id != ?", (name, tag_id)).fetchone()
    if other:
        db.execute("UPDATE projects SET tag = ? WHERE tag = ? COLLATE NOCASE", (other["name"], tag["name"]))
        db.execute("DELETE FROM tags WHERE id = ?", (tag_id,))
        db.commit()
        return jsonify({"ok": True, "merged_into": tag_json(db, other)})
    db.execute("UPDATE projects SET tag = ? WHERE tag = ? COLLATE NOCASE", (name, tag["name"]))
    db.execute("UPDATE tags SET name = ? WHERE id = ?", (name, tag_id))
    db.commit()
    return jsonify(tag_json(db, db.execute("SELECT * FROM tags WHERE id = ?", (tag_id,)).fetchone()))


@app.delete("/api/tags/<tag_id>")
def delete_tag(tag_id):
    """Deletes a tag; projects that used it are left without a tag."""
    db = get_db()
    tag = db.execute("SELECT * FROM tags WHERE id = ?", (tag_id,)).fetchone()
    if not tag:
        return jsonify({"error": "not found"}), 404
    db.execute("UPDATE projects SET tag = '' WHERE tag = ? COLLATE NOCASE", (tag["name"],))
    db.execute("DELETE FROM tags WHERE id = ?", (tag_id,))
    db.commit()
    return jsonify({"ok": True})


# ---------- categories ----------

@app.get("/api/categories")
def list_categories():
    rows = get_db().execute("SELECT * FROM categories ORDER BY order_index ASC, created_at ASC").fetchall()
    return jsonify([dict(r) for r in rows])


@app.post("/api/categories")
def create_category():
    body = request.get_json(force=True, silent=True) or {}
    name = (body.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name is required"}), 400
    category_id = uuid.uuid4().hex[:12]
    now = int(time.time() * 1000)
    db = get_db()
    next_order = db.execute("SELECT COALESCE(MAX(order_index), -1) + 1 FROM categories").fetchone()[0]
    db.execute(
        "INSERT INTO categories (id, name, order_index, created_at) VALUES (?, ?, ?, ?)",
        (category_id, name, next_order, now),
    )
    db.commit()
    return jsonify({"id": category_id, "name": name, "order_index": next_order, "created_at": now}), 201


@app.patch("/api/categories/<category_id>")
def update_category(category_id):
    body = request.get_json(force=True, silent=True) or {}
    fields, values = [], []
    if "name" in body:
        fields.append("name = ?")
        values.append((body["name"] or "").strip())
    if "order_index" in body:
        try:
            fields.append("order_index = ?")
            values.append(int(body["order_index"]))
        except (TypeError, ValueError):
            return jsonify({"error": "order_index must be an integer"}), 400
    if not fields:
        return jsonify({"error": "nothing to update"}), 400
    values.append(category_id)
    db = get_db()
    cur = db.execute(f"UPDATE categories SET {', '.join(fields)} WHERE id = ?", values)
    db.commit()
    if cur.rowcount == 0:
        return jsonify({"error": "not found"}), 404
    return jsonify({"ok": True})


@app.delete("/api/categories/<category_id>")
def delete_category(category_id):
    db = get_db()
    # projects in a deleted category become uncategorized, never deleted
    db.execute("UPDATE projects SET category_id = NULL WHERE category_id = ?", (category_id,))
    cur = db.execute("DELETE FROM categories WHERE id = ?", (category_id,))
    db.commit()
    if cur.rowcount == 0:
        return jsonify({"error": "not found"}), 404
    return jsonify({"ok": True})


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
    category_id = body.get("category_id") or None
    project_id = uuid.uuid4().hex[:12]
    now = int(time.time() * 1000)
    raw_tasks = body.get("tasks") or []
    if not isinstance(raw_tasks, list):
        return jsonify({"error": "tasks must be a list"}), 400
    db = get_db()
    tag = ensure_tag(db, body.get("tag"))
    link = normalize_link(body.get("link"))
    next_order = db.execute("SELECT COALESCE(MAX(order_index), -1) + 1 FROM projects").fetchone()[0]
    db.execute(
        "INSERT INTO projects (id, name, color, tag, created_at, order_index, category_id, link) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (project_id, name, color, tag, now, next_order, category_id, link),
    )
    # optional tasks created in the same transaction: strings or {title, done, note}
    created_tasks = []
    for i, item in enumerate(raw_tasks):
        spec = {"title": item} if isinstance(item, str) else (item if isinstance(item, dict) else {})
        task = insert_task(db, project_id, spec, now + i)
        if task:
            created_tasks.append(task)
    db.commit()
    return jsonify({
        "id": project_id, "name": name, "color": color, "tag": tag,
        "created_at": now, "order_index": next_order, "category_id": category_id,
        "link": link, "archived_at": None, "tasks": created_tasks,
    }), 201


@app.patch("/api/projects/<project_id>")
def update_project(project_id):
    body = request.get_json(force=True, silent=True) or {}
    fields, values = [], []
    db = get_db()
    for key in ("name", "color"):
        if key in body:
            fields.append(f"{key} = ?")
            values.append((body[key] or "").strip())
    if "tag" in body:
        fields.append("tag = ?")
        values.append(ensure_tag(db, body["tag"]))
    if "link" in body:
        fields.append("link = ?")
        values.append(normalize_link(body["link"]))
    if "archived" in body:
        # archived projects leave the board but stay in search and in the report
        fields.append("archived_at = ?")
        values.append(int(time.time() * 1000) if body["archived"] else None)
    if "order_index" in body:
        try:
            fields.append("order_index = ?")
            values.append(int(body["order_index"]))
        except (TypeError, ValueError):
            return jsonify({"error": "order_index must be an integer"}), 400
    if "category_id" in body:
        # explicit null/empty moves the project back to "uncategorized"
        fields.append("category_id = ?")
        values.append(body["category_id"] or None)
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

def task_json(r):
    d = dict(r)
    d["done"] = bool(d["done"])
    d["note"] = d.get("note") or ""
    return d


def insert_task(db, project_id, spec, created_at):
    """Insert one task from {title, done, note}; returns its JSON or None if no title."""
    title = (spec.get("title") or "").strip()
    if not title:
        return None
    done = bool(spec.get("done"))
    done_at = created_at if done else None
    note = (spec.get("note") or "").strip()
    task_id = uuid.uuid4().hex[:12]
    order = db.execute("SELECT COALESCE(MAX(order_index), -1) + 1 FROM tasks WHERE project_id = ?",
                       (project_id,)).fetchone()[0]
    db.execute(
        "INSERT INTO tasks (id, project_id, title, done, created_at, done_at, note, order_index) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (task_id, project_id, title, 1 if done else 0, created_at, done_at, note, order),
    )
    return {"id": task_id, "project_id": project_id, "title": title, "done": done,
            "created_at": created_at, "done_at": done_at, "note": note, "order_index": order}


@app.get("/api/tasks")
def list_tasks():
    project_id = request.args.get("project_id")
    db = get_db()
    if project_id:
        rows = db.execute("SELECT * FROM tasks WHERE project_id = ? ORDER BY order_index ASC, created_at ASC",
                          (project_id,)).fetchall()
    else:
        rows = db.execute("SELECT * FROM tasks ORDER BY project_id, order_index ASC, created_at ASC").fetchall()
    return jsonify([task_json(r) for r in rows])


@app.post("/api/tasks")
def create_task():
    body = request.get_json(force=True, silent=True) or {}
    project_id = (body.get("project_id") or "").strip()
    if not project_id or not (body.get("title") or "").strip():
        return jsonify({"error": "project_id and title are required"}), 400
    db = get_db()
    if not db.execute("SELECT 1 FROM projects WHERE id = ?", (project_id,)).fetchone():
        return jsonify({"error": "project not found"}), 404
    task = insert_task(db, project_id, body, int(time.time() * 1000))
    db.commit()
    return jsonify(task), 201


@app.post("/api/tasks/reorder")
def reorder_tasks():
    """Body {ids: [...]}: sets order_index to each id's position in the list."""
    body = request.get_json(force=True, silent=True) or {}
    ids = body.get("ids")
    if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
        return jsonify({"error": "ids must be a list of task ids"}), 400
    db = get_db()
    for i, task_id in enumerate(ids):
        db.execute("UPDATE tasks SET order_index = ? WHERE id = ?", (i, task_id))
    db.commit()
    return jsonify({"ok": True})


@app.patch("/api/tasks/<task_id>")
def update_task(task_id):
    body = request.get_json(force=True, silent=True) or {}
    fields, values = [], []
    if "done" in body:
        done = bool(body["done"])
        fields += ["done = ?", "done_at = ?"]
        values += [1 if done else 0, int(time.time() * 1000) if done else None]
    if "note" in body:
        fields.append("note = ?")
        values.append((body["note"] or "").strip())
    if "title" in body:
        title = (body["title"] or "").strip()
        if not title:
            return jsonify({"error": "title cannot be empty"}), 400
        fields.append("title = ?")
        values.append(title)
    if not fields:
        return jsonify({"error": "nothing to update"}), 400
    values.append(task_id)
    db = get_db()
    cur = db.execute(f"UPDATE tasks SET {', '.join(fields)} WHERE id = ?", values)
    db.commit()
    if cur.rowcount == 0:
        return jsonify({"error": "not found"}), 404
    row = db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return jsonify(task_json(row))


@app.delete("/api/tasks/<task_id>")
def delete_task(task_id):
    db = get_db()
    cur = db.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    db.commit()
    if cur.rowcount == 0:
        return jsonify({"error": "not found"}), 404
    return jsonify({"ok": True})


# ---------- report ----------

def parse_day(value, default):
    """YYYY-MM-DD in server local time -> epoch ms at 00:00; default when empty."""
    if not value:
        return default
    return int(time.mktime(time.strptime(value, "%Y-%m-%d")) * 1000)


@app.get("/api/report")
def report():
    """Tasks finished in [since, until] (default: last 7 days) grouped by
    category and project, plus every open task that has an observation."""
    now = int(time.time() * 1000)
    day = 24 * 3600 * 1000
    try:
        until = parse_day(request.args.get("until"), None)
        until = until + day if until is not None else now  # until is inclusive
        since = parse_day(request.args.get("since"), until - 7 * day)
    except ValueError:
        return jsonify({"error": "dates must be YYYY-MM-DD"}), 400
    db = get_db()
    q = """
        SELECT t.*, p.name AS project_name, p.color AS project_color, p.tag AS project_tag,
               p.category_id AS category_id, c.name AS category_name
        FROM tasks t JOIN projects p ON p.id = t.project_id
        LEFT JOIN categories c ON c.id = p.category_id
    """
    done_rows = db.execute(q + " WHERE t.done = 1 AND t.done_at >= ? AND t.done_at < ? ORDER BY t.done_at ASC",
                           (since, until)).fetchall()
    note_rows = db.execute(q + " WHERE t.done = 0 AND t.note != '' ORDER BY t.created_at ASC").fetchall()

    groups = {}
    for r in done_rows:
        cat = groups.setdefault(r["category_id"], {
            "category_id": r["category_id"], "category_name": r["category_name"], "projects": {}})
        proj = cat["projects"].setdefault(r["project_id"], {
            "project_id": r["project_id"], "name": r["project_name"], "color": r["project_color"],
            "tag": r["project_tag"], "tasks": []})
        proj["tasks"].append({"id": r["id"], "title": r["title"], "done_at": r["done_at"], "note": r["note"]})
    done = [dict(c, projects=list(c["projects"].values())) for c in groups.values()]
    notes = [{"id": r["id"], "title": r["title"], "note": r["note"], "created_at": r["created_at"],
                "project_id": r["project_id"], "project_name": r["project_name"],
                "project_color": r["project_color"], "category_name": r["category_name"]} for r in note_rows]
    return jsonify({"since": since, "until": until, "done_count": len(done_rows),
                    "done": done, "notes": notes})


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
