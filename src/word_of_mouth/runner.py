"""Word of Mouth: recs that only spread hand to hand between friends.

A small Flask app. All state lives in one SQLite file under DATA_DIR
(``data/.apps/word-of-mouth/`` by default, or ``WORD_OF_MOUTH_DATA_DIR``).
It listens on 127.0.0.1:PORT (8080 by default, or ``WORD_OF_MOUTH_PORT``).
"""

import os
from pathlib import Path

import qrcode
import qrcode.image.svg
from flask import Flask, Response, abort, jsonify, request, send_file
from werkzeug.serving import run_simple

from word_of_mouth.store import Store, StoreError

# Persistent state for this app lives under DATA_DIR. It defaults to
# ``data/.apps/word-of-mouth/`` but is overridable via the ``WORD_OF_MOUTH_DATA_DIR`` env var
# so a throwaway instance can run against a *copy* of the data while editing --
# Always read/write state through DATA_DIR;
# never hardcode ``data/.apps/word-of-mouth/`` at a call site, or the override is
# bypassed. A writing call site should ``DATA_DIR.mkdir(parents=True,
# exist_ok=True)`` before writing.
DATA_DIR = Path(os.environ.get("WORD_OF_MOUTH_DATA_DIR", "data/.apps/word-of-mouth"))

# Listen port. Defaults to this app's assigned port but is overridable via
# the ``WORD_OF_MOUTH_PORT`` env var so an editing agent can boot a throwaway
# instance on a spare port next to the live one.
# Never hardcode the port at the ``run_simple`` call, or the override is bypassed.
PORT = int(os.environ.get("WORD_OF_MOUTH_PORT", "8080"))

# The browser-side modules the workspace shell builds and every app serves from
# its own origin: the app contract (how a page talks to the shell framing it) and
# the element context menu (the right-click menu whose last rows hand the
# clicked element to a chat). A module import is a fetch without cookies, which
# the forwarder refuses across origins, so they are served here rather than from
# the shell. Relative to the repo root the service runs from, like DATA_DIR.
SHELL_STATIC_MODULES_DIR = Path("system/apps/system_interface/imbue/system_interface/static/_static")
SHELL_STATIC_MODULE_NAMES = ('app_contract.js', 'context_menu.js')

# The script every page serves (keep it on every page): it connects the page to
# the shell, reports where the page is on the handshake so the shell can reopen
# this app's window at the same place, and installs the element context menu.
# A page visited outside the shell runs it harmlessly: nothing arrives, and the
# menu's Explain and Modify rows grey out.
SHELL_PAGE_SCRIPT = """<script type="module">
  import { connectToShell } from "/_static/app_contract.js";
  import { installElementContextMenu } from "/_static/context_menu.js";
  let handshake = null;
  const connection = connectToShell({
    onHandshake: (received) => {
      handshake = received;
      connection.location(location.pathname + location.search, document.title);
    },
  });
  installElementContextMenu({ connection, handshake: () => handshake });
</script>"""

app = Flask("word_of_mouth", static_folder=None)


ASSETS_DIR = Path(__file__).parent / "assets"
PUBLIC_URL_FILE = DATA_DIR / "public_url.txt"

store = Store(DATA_DIR / "word_of_mouth.sqlite3")


def qr_svg(data: str) -> str:
    image = qrcode.make(data, image_factory=qrcode.image.svg.SvgPathImage, box_size=10, border=1)
    markup = image.to_string(encoding="unicode")
    return markup[markup.index("<svg"):]


def public_base() -> str:
    """The address phones outside the workspace reach this app at.

    Written by the public-address program when its tunnel comes up; until then
    links use whatever address the request arrived on.
    """
    if PUBLIC_URL_FILE.is_file():
        url = PUBLIC_URL_FILE.read_text().strip().rstrip("/")
        if url:
            return url
    return request.host_url.rstrip("/")


def link_url(token: str) -> str:
    return f"{public_base()}/r/{token}"


def page() -> Response:
    html = (ASSETS_DIR / "app.html").read_text()
    # Inside an Imbue Studio workspace, connect the page to the shell; run
    # standalone, the shell's modules are absent and the page stays plain.
    if SHELL_STATIC_MODULES_DIR.is_dir():
        html = html.replace("</body>", SHELL_PAGE_SCRIPT + "</body>")
    response = Response(html, mimetype="text/html")
    response.headers["Cache-Control"] = "no-store"
    return response


@app.errorhandler(StoreError)
def store_error(error: StoreError) -> tuple[Response, int]:
    return jsonify({"error": str(error)}), 400


def body() -> dict:
    return request.get_json(silent=True) or {}


@app.route("/")
@app.route("/start")
@app.route("/shelf")
@app.route("/r/<token>")
@app.route("/h/<token>")
@app.route("/pass/<node_id>")
@app.route("/rec/<node_id>")
@app.route("/t/<rec_id>")
def spa(**_: str) -> Response:
    return page()


@app.post("/api/recs")
def api_start() -> Response:
    b = body()
    return jsonify(store.start_rec(b.get("person_id", ""), b.get("name", ""), b.get("kind", ""),
                                   b.get("title", ""), b.get("note", ""), b.get("link", "")))


@app.post("/api/nodes/<node_id>/links")
def api_make_link(node_id: str) -> Response:
    b = body()
    result = store.make_link(node_id, b.get("person_id", ""), b.get("mode", ""), b.get("to_name", ""))
    return jsonify({**result, "url": link_url(result["token"])})


@app.post("/api/links/<token>/open")
def api_open_link(token: str) -> Response:
    b = body()
    return jsonify(store.open_link(token, b.get("person_id", ""), b.get("name", "")))


@app.get("/api/links/<token>")
def api_link(token: str) -> Response:
    return jsonify({**store.get_link(token), "url": link_url(token)})


@app.get("/api/links/<token>/qr.svg")
def api_link_qr(token: str) -> Response:
    store.get_link(token)
    return Response(qr_svg(link_url(token)), mimetype="image/svg+xml")


@app.get("/api/nodes/<node_id>")
def api_node(node_id: str) -> Response:
    return jsonify(store.get_node(node_id, request.args.get("me", "")))


@app.post("/api/nodes/<node_id>/keep")
def api_keep(node_id: str) -> Response:
    b = body()
    return jsonify(store.keep(node_id, b.get("person_id", ""), b.get("note", "")))


@app.post("/api/nodes/<node_id>/note")
def api_note(node_id: str) -> Response:
    b = body()
    store.save_note(node_id, b.get("person_id", ""), b.get("note", ""))
    return jsonify({"ok": True})


@app.get("/api/shelf")
def api_shelf() -> Response:
    return jsonify(store.shelf(request.args.get("me", "")))


@app.get("/api/recs/<rec_id>/tree")
def api_tree(rec_id: str) -> Response:
    return jsonify(store.tree(rec_id, request.args.get("me", "")))


@app.route("/_static/<basename>")
def shell_module(basename: str) -> Response:
    # The two shell-built modules and nothing else: a name that is not one of
    # them is a 404, so this route can never read outside that directory.
    if basename not in SHELL_STATIC_MODULE_NAMES:
        abort(404)
    module_path = SHELL_STATIC_MODULES_DIR / basename
    if not module_path.is_file():
        abort(404)
    # Flask resolves a relative path against the app's own directory, not the cwd.
    return send_file(module_path.absolute(), mimetype="text/javascript")


@app.route("/health")
def health() -> Response:
    return Response('{"status": "ok"}', mimetype="application/json")


def main() -> None:
    run_simple(
        "127.0.0.1", PORT, app, threaded=True, use_reloader=False, use_debugger=False
    )


if __name__ == "__main__":
    main()
