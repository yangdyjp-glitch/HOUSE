import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from server import create_server


class FavoriteApiTests(unittest.TestCase):
    def setUp(self):
        self.temp_directory = tempfile.TemporaryDirectory()
        root = Path(self.temp_directory.name)
        (root / "index.html").write_text("HOUSE", encoding="utf-8")
        self.server = create_server(
            host="127.0.0.1",
            port=0,
            root=root,
            database_path=root / "test.sqlite3",
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp_directory.cleanup()

    def request(self, path, method="GET", body=None):
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = Request(
            self.base_url + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        with urlopen(request, timeout=2) as response:
            return response.status, json.loads(response.read())

    def test_add_list_and_remove_favorite(self):
        status, payload = self.request("/api/favorites")
        self.assertEqual(status, 200)
        self.assertEqual(payload["favorites"], [])

        status, payload = self.request("/api/favorites/project-3", method="PUT")
        self.assertEqual(status, 200)
        self.assertEqual([item["houseId"] for item in payload["favorites"]], ["project-3"])

        status, payload = self.request("/api/favorites/project-3", method="DELETE")
        self.assertEqual(status, 200)
        self.assertEqual(payload["favorites"], [])

    def test_legacy_import_is_idempotent(self):
        body = {"houseIds": ["project-1", "project-1", "project-2"]}
        self.request("/api/favorites/import", method="POST", body=body)
        _, payload = self.request("/api/favorites/import", method="POST", body=body)
        self.assertEqual(
            {item["houseId"] for item in payload["favorites"]},
            {"project-1", "project-2"},
        )

    def test_rejects_invalid_house_id(self):
        with self.assertRaises(HTTPError) as error:
            self.request("/api/favorites/not%20valid", method="PUT")
        self.assertEqual(error.exception.code, 400)
        error.exception.close()


if __name__ == "__main__":
    unittest.main()
