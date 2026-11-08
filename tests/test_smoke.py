from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

if not os.environ.get("HWZ_BILI_DATA_DIR"):
    os.environ["HWZ_BILI_DATA_DIR"] = tempfile.mkdtemp(prefix="hwz-test-data-")

from app import WindowApi  # noqa: E402
from hwz import config  # noqa: E402
from hwz.bili import BiliClient, ResolvedInput  # noqa: E402
from hwz.downloader import DownloadManager, _safe_filename  # noqa: E402
from hwz.store import HistoryStore, SettingsStore  # noqa: E402


class SmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        config.ensure_runtime_dirs()

    def test_frozen_names_and_reserved_names(self) -> None:
        self.assertEqual(_safe_filename('a:b/c*?  "d"'), "a b c d")
        self.assertTrue(_safe_filename("CON").startswith("_"))
        self.assertLessEqual(len(_safe_filename("x" * 400)), 150)

    def test_url_input_recognition_without_network(self) -> None:
        client = BiliClient(SettingsStore())
        result = client.resolve_input("https://www.bilibili.com/video/BV1GJ411x7h7?p=2")
        self.assertIsInstance(result, ResolvedInput)
        self.assertEqual(result.bvid, "BV1GJ411x7h7")
        self.assertEqual(result.page, 2)
        result2 = client.resolve_input("BV1GJ411x7h7")
        self.assertEqual(result2.bvid, "BV1GJ411x7h7")
        with self.assertRaises(Exception):
            client.resolve_input("https://example.com/video/BV1GJ411x7h7")

    def test_window_api_has_no_public_state(self) -> None:
        # pywebview recursively serializes every public attribute of js_api.
        # A public Window reference would crawl into the native WebView and
        # eventually make the UI thread unresponsive.
        api = WindowApi()
        public_objects = [
            name
            for name in dir(api)
            if not name.startswith("_") and not callable(getattr(api, name))
        ]
        self.assertEqual(public_objects, [])

    def test_settings_roundtrip(self) -> None:
        store = SettingsStore()
        target = Path(tempfile.mkdtemp(prefix="hwz-downloads-"))
        updated = store.update({"connections": 8, "download_dir": str(target), "save_cover": True})
        self.assertEqual(updated["connections"], 8)
        self.assertTrue(updated["save_cover"])
        self.assertTrue(Path(updated["download_dir"]).is_dir())
        # Clamp invalid values instead of writing them.
        updated = store.update({"connections": 999})
        self.assertLessEqual(updated["connections"], 24)
        self.assertGreaterEqual(updated["connections"], 4)

    def test_history_persistence_and_delete(self) -> None:
        history = HistoryStore()
        history.clear()
        output = Path(tempfile.mkdtemp()) / "demo.mp4"
        output.write_bytes(b"demo")
        record = history.add({"title": "Demo", "output_path": str(output), "file_size": 4})
        self.assertTrue(history.get(record["id"])["exists"])
        self.assertEqual(history.count(), 1)
        self.assertTrue(history.delete(record["id"]))
        self.assertEqual(history.count(), 0)

    def test_chunk_bounds(self) -> None:
        manager = object.__new__(DownloadManager)
        bounds = manager._chunk_bounds(9 * 1024 * 1024)
        self.assertEqual(bounds[0][0], 0)
        self.assertEqual(bounds[-1][1], 9 * 1024 * 1024 - 1)
        for index in range(1, len(bounds)):
            self.assertEqual(bounds[index][0], bounds[index - 1][1] + 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
