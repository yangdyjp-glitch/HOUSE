from __future__ import annotations

import json
import os
import queue
import re
import sqlite3
import threading
from contextlib import closing
from datetime import datetime, timezone
from functools import partial
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse


ROOT = Path(__file__).resolve().parent
HOUSE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def resolve_database_path() -> Path:
    configured_path = os.environ.get("DATABASE_PATH")
    if configured_path:
        return Path(configured_path).expanduser().resolve()

    volume_path = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH")
    if volume_path:
        return Path(volume_path).resolve() / "house.sqlite3"

    return ROOT / "data" / "house.sqlite3"


class FavoriteStore:
    def __init__(self, database_path: Path):
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.Lock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection:
            with connection:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS favorites (
                        house_id TEXT PRIMARY KEY,
                        created_at TEXT NOT NULL
                    )
                    """
                )

    def list(self) -> list[dict[str, str]]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT house_id, created_at FROM favorites ORDER BY created_at DESC, house_id"
            ).fetchall()
        return [
            {"houseId": row["house_id"], "createdAt": row["created_at"]}
            for row in rows
        ]

    def add(self, house_id: str) -> bool:
        with self._write_lock, closing(self._connect()) as connection:
            with connection:
                cursor = connection.execute(
                    "INSERT OR IGNORE INTO favorites (house_id, created_at) VALUES (?, ?)",
                    (house_id, utc_now()),
                )
        return cursor.rowcount > 0

    def add_many(self, house_ids: list[str]) -> bool:
        changed = False
        with self._write_lock, closing(self._connect()) as connection:
            with connection:
                for house_id in house_ids:
                    cursor = connection.execute(
                        "INSERT OR IGNORE INTO favorites (house_id, created_at) VALUES (?, ?)",
                        (house_id, utc_now()),
                    )
                    changed = cursor.rowcount > 0 or changed
        return changed

    def remove(self, house_id: str) -> bool:
        with self._write_lock, closing(self._connect()) as connection:
            with connection:
                cursor = connection.execute(
                    "DELETE FROM favorites WHERE house_id = ?", (house_id,)
                )
        return cursor.rowcount > 0


class FavoriteEvents:
    def __init__(self) -> None:
        self._subscribers: set[queue.Queue[str]] = set()
        self._lock = threading.Lock()

    def subscribe(self) -> queue.Queue[str]:
        subscriber: queue.Queue[str] = queue.Queue(maxsize=2)
        with self._lock:
            self._subscribers.add(subscriber)
        return subscriber

    def unsubscribe(self, subscriber: queue.Queue[str]) -> None:
        with self._lock:
            self._subscribers.discard(subscriber)

    def publish(self, payload: str) -> None:
        with self._lock:
            subscribers = tuple(self._subscribers)
        for subscriber in subscribers:
            try:
                subscriber.put_nowait(payload)
            except queue.Full:
                try:
                    subscriber.get_nowait()
                except queue.Empty:
                    pass
                try:
                    subscriber.put_nowait(payload)
                except queue.Full:
                    pass


class HouseHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, server_address, handler_class, store: FavoriteStore):
        super().__init__(server_address, handler_class)
        self.store = store
        self.favorite_events = FavoriteEvents()

    def favorite_payload(self) -> dict[str, object]:
        return {"favorites": self.store.list(), "updatedAt": utc_now()}

    def publish_favorites(self) -> dict[str, object]:
        payload = self.favorite_payload()
        self.favorite_events.publish(json.dumps(payload, ensure_ascii=False))
        return payload


class HouseRequestHandler(SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    @property
    def house_server(self) -> HouseHTTPServer:
        return self.server  # type: ignore[return-value]

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/health":
            storage = "railway-volume" if os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") else "local-file"
            self._send_json({"status": "ok", "storage": storage})
            return
        if path == "/api/favorites":
            self._send_json(self.house_server.favorite_payload())
            return
        if path == "/api/favorites/events":
            self._stream_favorite_events()
            return
        super().do_GET()

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path != "/api/favorites/import":
            self._send_error_json(HTTPStatus.NOT_FOUND, "Unknown API endpoint")
            return

        body = self._read_json()
        if body is None:
            return
        house_ids = body.get("houseIds")
        if not isinstance(house_ids, list) or len(house_ids) > 100:
            self._send_error_json(HTTPStatus.BAD_REQUEST, "houseIds must be an array with at most 100 items")
            return
        if not all(isinstance(item, str) and HOUSE_ID_PATTERN.fullmatch(item) for item in house_ids):
            self._send_error_json(HTTPStatus.BAD_REQUEST, "One or more houseIds are invalid")
            return

        changed = self.house_server.store.add_many(list(dict.fromkeys(house_ids)))
        payload = self.house_server.publish_favorites() if changed else self.house_server.favorite_payload()
        self._send_json(payload)

    def do_PUT(self) -> None:
        house_id = self._favorite_id_from_path()
        if house_id is None:
            return
        changed = self.house_server.store.add(house_id)
        payload = self.house_server.publish_favorites() if changed else self.house_server.favorite_payload()
        self._send_json(payload)

    def do_DELETE(self) -> None:
        house_id = self._favorite_id_from_path()
        if house_id is None:
            return
        changed = self.house_server.store.remove(house_id)
        payload = self.house_server.publish_favorites() if changed else self.house_server.favorite_payload()
        self._send_json(payload)

    def end_headers(self) -> None:
        path = urlparse(self.path).path
        if path == "/" or path.endswith(".html"):
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def _favorite_id_from_path(self) -> str | None:
        path = urlparse(self.path).path
        prefix = "/api/favorites/"
        if not path.startswith(prefix) or path == "/api/favorites/events":
            self._send_error_json(HTTPStatus.NOT_FOUND, "Unknown API endpoint")
            return None
        house_id = unquote(path[len(prefix) :])
        if not HOUSE_ID_PATTERN.fullmatch(house_id):
            self._send_error_json(HTTPStatus.BAD_REQUEST, "Invalid house id")
            return None
        return house_id

    def _read_json(self) -> dict[str, object] | None:
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            content_length = 0
        if content_length <= 0 or content_length > 16_384:
            self._send_error_json(HTTPStatus.BAD_REQUEST, "Invalid request body")
            return None
        try:
            body = json.loads(self.rfile.read(content_length))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_error_json(HTTPStatus.BAD_REQUEST, "Request body must be valid JSON")
            return None
        if not isinstance(body, dict):
            self._send_error_json(HTTPStatus.BAD_REQUEST, "Request body must be a JSON object")
            return None
        return body

    def _send_json(self, payload: dict[str, object], status: HTTPStatus = HTTPStatus.OK) -> None:
        content = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(content)

    def _send_error_json(self, status: HTTPStatus, message: str) -> None:
        self._send_json({"error": message}, status)

    def _stream_favorite_events(self) -> None:
        subscriber = self.house_server.favorite_events.subscribe()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            self.wfile.write(b"retry: 3000\n")
            self._write_event(json.dumps(self.house_server.favorite_payload(), ensure_ascii=False))
            while True:
                try:
                    payload = subscriber.get(timeout=20)
                    self._write_event(payload)
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass
        finally:
            self.house_server.favorite_events.unsubscribe(subscriber)

    def _write_event(self, payload: str) -> None:
        self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
        self.wfile.flush()


def create_server(
    host: str = "0.0.0.0",
    port: int = 8000,
    root: Path = ROOT,
    database_path: Path | None = None,
) -> HouseHTTPServer:
    store = FavoriteStore(database_path or resolve_database_path())
    handler = partial(HouseRequestHandler, directory=str(root))
    return HouseHTTPServer((host, port), handler, store)


def main() -> None:
    port = int(os.environ.get("PORT", "8000"))
    server = create_server(port=port)
    print(f"HOUSE is serving on http://0.0.0.0:{port}")
    print(f"Favorites database: {server.store.database_path}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
