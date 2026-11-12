from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path

if not os.environ.get("HWZ_BILI_DATA_DIR"):
    os.environ["HWZ_BILI_DATA_DIR"] = tempfile.mkdtemp(prefix="hwz-test-data-")

from app import WindowApi  # noqa: E402
from hwz import config  # noqa: E402
from hwz.bili import BiliClient, ResolvedInput  # noqa: E402
from hwz.downloader import DownloadManager, _safe_filename  # noqa: E402
from hwz.player import _playlist_stats_from_text, _scaled_size  # noqa: E402
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

    def test_player_output_size_keeps_aspect_and_caps_height(self) -> None:
        self.assertEqual(_scaled_size({"video_width": 3840, "video_height": 2160}, 1080), (1920, 1080))
        self.assertEqual(_scaled_size({"video_width": 1920, "video_height": 1080}, 720), (1280, 720))
        self.assertEqual(_scaled_size({"video_width": 1920, "video_height": 1080}, 0), (1920, 1080))
        # Odd dimensions are rounded down to even values for yuv420p.
        self.assertEqual(_scaled_size({"video_width": 1919, "video_height": 1079}, 0), (1918, 1078))

    def test_player_playlist_stats(self) -> None:
        playlist = (
            "#EXTM3U\n#EXT-X-PLAYLIST-TYPE:EVENT\n"
            "#EXTINF:1.000000,\nseg_00000.m4s\n"
            "#EXTINF:0.500000,\nseg_00001.m4s\n"
        )
        self.assertEqual(_playlist_stats_from_text(playlist), (2, 1.5, False))
        self.assertEqual(_playlist_stats_from_text(playlist + "#EXT-X-ENDLIST\n"), (2, 1.5, True))

    def test_download_plan_exposes_video_metadata_for_player(self) -> None:
        client = BiliClient(SettingsStore())
        client._parse_cache["test-plan"] = {
            "_created": time.time(),
            "title": "Demo",
            "bvid": "BV1GJ411x7h7",
            "page": 1,
            "page_count": 1,
            "duration": 60,
            "source_url": "https://www.bilibili.com/video/BV1GJ411x7h7",
            "_best_audio": {"url": "https://example.com/audio.m4a"},
            "_qualities": [
                {
                    "id": "q1080-24-avc",
                    "label": "1080P 高清",
                    "width": 1920,
                    "height": 1080,
                    "fps": 30,
                    "_video": {"url": "https://example.com/video.m4s", "backup_urls": []},
                    "_audio": {"url": "https://example.com/audio.m4a", "backup_urls": []},
                    "_combined": False,
                }
            ],
        }
        plan = client.build_download_plan("test-plan", "q1080-24-avc", "video", False)
        self.assertEqual(plan["video_width"], 1920)
        self.assertEqual(plan["video_height"], 1080)
        self.assertEqual(plan["video_fps"], 30)
        self.assertEqual(len(plan["streams"]), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
