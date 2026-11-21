import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import globalping


class PublicationValidationTests(unittest.TestCase):
    def test_empty_candidate_set_is_not_published(self):
        with tempfile.TemporaryDirectory() as directory:
            result = globalping._publish([], Path(directory), [], {}, [])
            self.assertEqual(result["status"], "blocked")
            self.assertFalse((Path(directory) / "iptv.m3u").exists())

    def test_channel_cap_is_enforced_in_output_validation(self):
        accepted = [
            {"channel": "CCTV-1", "normalized_url": "https://a.example/1"},
            {"channel": "CCTV-1", "normalized_url": "https://a.example/2"},
            {"channel": "CCTV-1", "normalized_url": "https://a.example/3"},
        ]
        m3u = "#EXTM3U\n" + "".join(f"#EXTINF:-1,{x['channel']}\n{x['normalized_url']}\n" for x in accepted)
        txt = "".join(f"{x['channel']},{x['normalized_url']}\n" for x in accepted)
        valid, reason = globalping._validate_new(m3u, txt, accepted, ["CCTV-1"])
        self.assertFalse(valid)
        self.assertIn("最多两条", reason)
