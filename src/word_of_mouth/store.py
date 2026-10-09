"""SQLite store for recs and the hand-to-hand chains they travel.

A rec is started by one person. Every person a rec reaches gets one node in
that rec's tree; a node's parent is the node of whoever handed it to them.
Hand-offs are links: a "one" link is claimed by the first other person to
open it, a "group" link adds every opener as a branch under the passer.
People have no accounts: a person is a random id their phone keeps, plus the
name they typed.
"""

import secrets
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS people (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS recs (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    note TEXT NOT NULL,
    link TEXT NOT NULL,
    root_node_id TEXT,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS nodes (
    id TEXT PRIMARY KEY,
    rec_id TEXT NOT NULL REFERENCES recs(id),
    person_id TEXT NOT NULL REFERENCES people(id),
    parent_node_id TEXT REFERENCES nodes(id),
    via_link TEXT,
    note TEXT NOT NULL DEFAULT '',
    kept INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    UNIQUE (rec_id, person_id)
);
CREATE TABLE IF NOT EXISTS links (
    token TEXT PRIMARY KEY,
    rec_id TEXT NOT NULL REFERENCES recs(id),
    from_node_id TEXT NOT NULL REFERENCES nodes(id),
    mode TEXT NOT NULL CHECK (mode IN ('one', 'group')),
    to_name TEXT NOT NULL DEFAULT '',
    claimed_node_id TEXT REFERENCES nodes(id),
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS nodes_by_person ON nodes(person_id);
CREATE INDEX IF NOT EXISTS links_by_node ON links(from_node_id);
"""

KINDS = ("food", "song", "book", "other")
MAX_TITLE = 120
MAX_NOTE = 600
MAX_NAME = 40
MAX_LINK = 500


class StoreError(Exception):
    """A request the store refuses; the message is shown to the user."""


def new_id(n_bytes: int = 6) -> str:
    return secrets.token_urlsafe(n_bytes)


def clean(text: object, limit: int) -> str:
    return str(text or "").strip()[:limit]


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.write_lock = threading.Lock()
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA)

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        return db

    # -- people -------------------------------------------------------------

    def upsert_person(self, db: sqlite3.Connection, person_id: str, name: str) -> None:
        person_id = clean(person_id, 64)
        name = clean(name, MAX_NAME)
        if len(person_id) < 8:
            raise StoreError("Tell us your name first.")
        if not name:
            raise StoreError("Tell us your name first.")
        db.execute(
            "INSERT INTO people (id, name, created_at) VALUES (?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET name = excluded.name",
            (person_id, name, time.time()),
        )

    # -- writes -------------------------------------------------------------

    def start_rec(self, person_id: str, name: str, kind: str, title: str, note: str, link: str) -> dict:
        title = clean(title, MAX_TITLE)
        if not title:
            raise StoreError("Give it a title.")
        kind = kind if kind in KINDS else "other"
        link = clean(link, MAX_LINK)
        if link and not link.startswith(("http://", "https://")):
            link = "https://" + link
        with self.write_lock, self.connect() as db:
            self.upsert_person(db, person_id, name)
            rec_id, node_id = new_id(), new_id()
            now = time.time()
            db.execute(
                "INSERT INTO recs (id, kind, title, note, link, root_node_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (rec_id, kind, title, clean(note, MAX_NOTE), link, node_id, now),
            )
            db.execute(
                "INSERT INTO nodes (id, rec_id, person_id, parent_node_id, kept, created_at) VALUES (?, ?, ?, NULL, 1, ?)",
                (node_id, rec_id, person_id, now),
            )
        return {"rec_id": rec_id, "node_id": node_id}

    def make_link(self, node_id: str, person_id: str, mode: str, to_name: str) -> dict:
        if mode not in ("one", "group"):
            raise StoreError("Pick one person or the whole room.")
        to_name = clean(to_name, MAX_NAME)
        if mode == "one" and not to_name:
            raise StoreError("Who's it for? Type their name.")
        with self.write_lock, self.connect() as db:
            node = self.owned_node(db, node_id, person_id)
            if mode == "group":
                existing = db.execute(
                    "SELECT token FROM links WHERE from_node_id = ? AND mode = 'group'", (node_id,)
                ).fetchone()
                if existing:
                    return {"token": existing["token"]}
            token = new_id(5)
            db.execute(
                "INSERT INTO links (token, rec_id, from_node_id, mode, to_name, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (token, node["rec_id"], node_id, mode, to_name if mode == "one" else "", time.time()),
            )
        return {"token": token}

    def open_link(self, token: str, person_id: str, name: str) -> dict:
        """Join the chain through a link, if this person may; return the view."""
        with self.write_lock, self.connect() as db:
            link = db.execute("SELECT * FROM links WHERE token = ?", (token,)).fetchone()
            if link is None:
                raise StoreError("This link doesn't exist. Ask whoever sent it for a fresh one.")
            self.upsert_person(db, person_id, name)
            mine = db.execute(
                "SELECT id FROM nodes WHERE rec_id = ? AND person_id = ?", (link["rec_id"], person_id)
            ).fetchone()
            status = "joined"
            if mine is not None:
                node_id = mine["id"]
                status = "already"
            elif link["mode"] == "one" and link["claimed_node_id"] is not None:
                node_id = None
                status = "taken"
            else:
                node_id = new_id()
                db.execute(
                    "INSERT INTO nodes (id, rec_id, person_id, parent_node_id, via_link, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (node_id, link["rec_id"], person_id, link["from_node_id"], token, time.time()),
                )
                if link["mode"] == "one":
                    db.execute("UPDATE links SET claimed_node_id = ? WHERE token = ?", (node_id, token))
            view = self.node_view(db, node_id) if node_id else self.link_preview(db, link)
        view["status"] = status
        view["link_mode"] = link["mode"]
        view["to_name"] = link["to_name"]
        return view

    def keep(self, node_id: str, person_id: str, note: str) -> dict:
        with self.write_lock, self.connect() as db:
            node = self.owned_node(db, node_id, person_id)
            is_root = node["parent_node_id"] is None
            if is_root:
                db.execute("UPDATE nodes SET kept = 1 WHERE id = ?", (node_id,))
            else:
                db.execute("UPDATE nodes SET kept = 1, note = ? WHERE id = ?", (clean(note, MAX_NOTE), node_id))
            return self.node_view(db, node_id)

    def save_note(self, node_id: str, person_id: str, note: str) -> None:
        with self.write_lock, self.connect() as db:
            node = self.owned_node(db, node_id, person_id)
            if node["parent_node_id"] is not None:
                db.execute("UPDATE nodes SET note = ? WHERE id = ?", (clean(note, MAX_NOTE), node_id))

    # -- reads --------------------------------------------------------------

    def owned_node(self, db: sqlite3.Connection, node_id: str, person_id: str) -> sqlite3.Row:
        node = db.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()
        if node is None or node["person_id"] != person_id:
            raise StoreError("That isn't yours to pass on.")
        return node

    def rec_dict(self, db: sqlite3.Connection, rec_id: str) -> dict:
        rec = db.execute("SELECT * FROM recs WHERE id = ?", (rec_id,)).fetchone()
        return {k: rec[k] for k in ("id", "kind", "title", "note", "link", "created_at")}

    def path_to(self, db: sqlite3.Connection, node_id: str) -> list[dict]:
        rows = db.execute(
            """
            WITH RECURSIVE up(id, parent_node_id, person_id, note, kept, created_at, depth) AS (
                SELECT id, parent_node_id, person_id, note, kept, created_at, 0 FROM nodes WHERE id = ?
                UNION ALL
                SELECT n.id, n.parent_node_id, n.person_id, n.note, n.kept, n.created_at, up.depth + 1
                FROM nodes n JOIN up ON n.id = up.parent_node_id
            )
            SELECT up.*, p.name FROM up JOIN people p ON p.id = up.person_id ORDER BY depth DESC
            """,
            (node_id,),
        ).fetchall()
        return [
            {"node_id": r["id"], "name": r["name"], "note": r["note"], "kept": bool(r["kept"])} for r in rows
        ]

    def node_view(self, db: sqlite3.Connection, node_id: str) -> dict:
        node = db.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()
        reach = db.execute("SELECT COUNT(*) FROM nodes WHERE rec_id = ?", (node["rec_id"],)).fetchone()[0]
        return {
            "rec": self.rec_dict(db, node["rec_id"]),
            "chain": self.path_to(db, node_id),
            "node_id": node_id,
            "kept": bool(node["kept"]),
            "my_note": node["note"],
            "is_starter": node["parent_node_id"] is None,
            "reach": reach - 1,
        }

    def link_preview(self, db: sqlite3.Connection, link: sqlite3.Row) -> dict:
        reach = db.execute("SELECT COUNT(*) FROM nodes WHERE rec_id = ?", (link["rec_id"],)).fetchone()[0]
        return {
            "rec": self.rec_dict(db, link["rec_id"]),
            "chain": self.path_to(db, link["from_node_id"]),
            "node_id": None,
            "kept": False,
            "my_note": "",
            "is_starter": False,
            "reach": reach - 1,
        }

    def get_node(self, node_id: str, person_id: str) -> dict:
        with self.connect() as db:
            self.owned_node(db, node_id, person_id)
            return self.node_view(db, node_id)

    def get_link(self, token: str) -> dict:
        with self.connect() as db:
            link = db.execute("SELECT * FROM links WHERE token = ?", (token,)).fetchone()
            if link is None:
                raise StoreError("This link doesn't exist.")
            rec = self.rec_dict(db, link["rec_id"])
            claimed_by = None
            if link["claimed_node_id"]:
                claimed_by = db.execute(
                    "SELECT p.name FROM nodes n JOIN people p ON p.id = n.person_id WHERE n.id = ?",
                    (link["claimed_node_id"],),
                ).fetchone()["name"]
            joined = db.execute("SELECT COUNT(*) FROM nodes WHERE via_link = ?", (token,)).fetchone()[0]
            return {
                "token": token,
                "mode": link["mode"],
                "to_name": link["to_name"],
                "rec": rec,
                "from_node_id": link["from_node_id"],
                "claimed_by": claimed_by,
                "joined": joined,
            }

    def shelf(self, person_id: str) -> dict:
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT n.id AS node_id, n.rec_id, n.parent_node_id, n.kept, n.created_at,
                       (SELECT COUNT(*) FROM nodes m WHERE m.rec_id = n.rec_id) - 1 AS reach,
                       (SELECT COUNT(*) FROM links l WHERE l.from_node_id = n.id) AS passes
                FROM nodes n WHERE n.person_id = ? ORDER BY n.created_at DESC
                """,
                (person_id,),
            ).fetchall()
            items = []
            for r in rows:
                items.append({
                    "node_id": r["node_id"],
                    "rec": self.rec_dict(db, r["rec_id"]),
                    "chain": [c["name"] for c in self.path_to(db, r["node_id"])],
                    "started": r["parent_node_id"] is None,
                    "kept": bool(r["kept"]),
                    "passed": r["passes"] > 0,
                    "reach": r["reach"],
                })
        return {
            "got": [i for i in items if not i["started"]],
            "passed": [i for i in items if i["passed"]],
            "started": [i for i in items if i["started"]],
        }

    def tree(self, rec_id: str, person_id: str = "") -> dict:
        with self.connect() as db:
            rec = db.execute("SELECT id FROM recs WHERE id = ?", (rec_id,)).fetchone()
            if rec is None:
                raise StoreError("No such rec.")
            rows = db.execute(
                "SELECT n.id, n.parent_node_id, n.created_at, p.name FROM nodes n JOIN people p ON p.id = n.person_id "
                "WHERE n.rec_id = ? ORDER BY n.created_at",
                (rec_id,),
            ).fetchall()
            mine = db.execute(
                "SELECT id FROM nodes WHERE rec_id = ? AND person_id = ?", (rec_id, person_id)
            ).fetchone()
            return {
                "rec": self.rec_dict(db, rec_id),
                "my_node_id": mine["id"] if mine else None,
                "nodes": [
                    {"id": r["id"], "parent": r["parent_node_id"], "name": r["name"], "at": r["created_at"]}
                    for r in rows
                ],
            }
