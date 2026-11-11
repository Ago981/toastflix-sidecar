import asyncio
import base64
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
from fastapi.testclient import TestClient

from app import app
from movy_extractor import (
    MOVY_SITE,
    MOVY_UA,
    _init_key,
    _next_word,
    _ushr,
    check_proxy_listening,
    decrypt,
    extract_movy_stream,
    get_warp_proxy,
    is_proxy_alive,
    is_warp_active,
    movy_headers,
    parse_media_key,
    quality_of,
)
from sync import SyncEngine, is_movy_request


class FakeAudio:
    def __init__(self):
        self.hid = "a" * 16

    def metadata(self, hid):
        return {
            "segs": ["https://cdn.example.test/0.ts"],
            "durs": [10.0],
            "starts": [0.0],
            "source_fingerprint": "fake-audio-fp",
        }


class FakeOffsets:
    def __init__(self):
        self.reported = None

    @staticmethod
    def key(media_key, resolution, video_fp, audio_fp):
        return f"{media_key}:{resolution}:{video_fp}:{audio_fp}"

    async def lookup(self, payload):
        return None

    async def report(self, payload, result):
        self.reported = result


class MovyExtractorTests(unittest.IsolatedAsyncioTestCase):
    def test_quality_of(self):
        self.assertEqual(quality_of("4k"), 2160)
        self.assertEqual(quality_of("2160p"), 2160)
        self.assertEqual(quality_of("1080p"), 1080)
        self.assertEqual(quality_of("720p"), 720)
        self.assertEqual(quality_of("480p"), 480)
        self.assertIsNone(quality_of("invalid"))

    def test_movy_headers(self):
        headers = movy_headers()
        self.assertEqual(headers["User-Agent"], MOVY_UA)
        self.assertEqual(headers["Origin"], MOVY_SITE)
        self.assertEqual(headers["Referer"], f"{MOVY_SITE}/")

    async def test_parse_media_key(self):
        # Movie key with tmdb prefix
        with patch("movy_extractor.fetch_tmdb_metadata", new=AsyncMock(return_value=("Interstellar", "2014", "tt0816692"))):
            is_movie, tmdb_id, imdb_id, s, ep, title, year = await parse_media_key("tmdb:movie:157336")
            self.assertTrue(is_movie)
            self.assertEqual(tmdb_id, 157336)
            self.assertEqual(title, "Interstellar")
            self.assertEqual(year, "2014")

        # Series key with IMDB id
        with patch("movy_extractor.resolve_tmdb_id", new=AsyncMock(return_value=113962)), \
             patch("movy_extractor.fetch_tmdb_metadata", new=AsyncMock(return_value=("Mayor of Kingstown", "2021", "tt13111078"))):
            is_movie, tmdb_id, imdb_id, s, ep, title, year = await parse_media_key("series:tt13111078:2:2")
            self.assertFalse(is_movie)
            self.assertEqual(tmdb_id, 113962)
            self.assertEqual(imdb_id, "tt13111078")
            self.assertEqual(s, 2)
            self.assertEqual(ep, 2)

    def test_cipher_decrypt(self):
        seed = "test-seed-xyz"
        media_id = 12345
        plain = b'mvm1{"sources":[{"quality":"1080p","url":"https://goldenkite.net/test.m3u8"}]}'

        # Encrypt with stream cipher
        state = _init_key(seed, media_id)
        cipher = bytearray(len(plain))
        i = 0
        round_num = 0
        while i < len(plain):
            word = _next_word(state, round_num)
            b = 0
            while b < 4 and i < len(plain):
                w_byte = (_ushr(word, 8 * b)) & 255
                cipher[i] = plain[i] ^ w_byte
                i += 1
                b += 1
            round_num += 1

        payload_b64 = base64.b64encode(cipher).decode("ascii")
        decrypted = decrypt(payload_b64, seed, media_id)
        self.assertIsNotNone(decrypted)
        self.assertIn("sources", decrypted)
        self.assertEqual(decrypted["sources"][0]["quality"], "1080p")
        self.assertEqual(decrypted["sources"][0]["url"], "https://goldenkite.net/test.m3u8")

    async def test_extract_movy_stream_mocked(self):
        seed = "test-seed-mock"
        media_id = 999
        plain = b'mvm1{"sources":[{"quality":"1080p","url":"https://goldenkite.net/master.m3u8"}]}'
        state = _init_key(seed, media_id)
        cipher = bytearray(len(plain))
        i, round_num = 0, 0
        while i < len(plain):
            w = _next_word(state, round_num)
            b = 0
            while b < 4 and i < len(plain):
                cipher[i] = plain[i] ^ ((_ushr(w, 8 * b)) & 255)
                i += 1
                b += 1
            round_num += 1
        encrypted_payload = base64.b64encode(cipher).decode("ascii")

        with patch("movy_extractor.parse_media_key", new=AsyncMock(return_value=(True, media_id, "tt000", 0, 0, "Test", "2024"))), \
             patch("movy_extractor.get_seed", new=AsyncMock(return_value=seed)), \
             patch("movy_extractor.fetch_sources", new=AsyncMock(return_value=encrypted_payload)), \
             patch("movy_extractor.resolve_video", new=AsyncMock(return_value=("https://goldenkite.net/variant_1080.m3u8", 1080))):

            streams = await extract_movy_stream("movie:tt000:0:0")
            self.assertEqual(len(streams), 1)
            self.assertEqual(streams[0]["resolution"], 1080)
            self.assertEqual(streams[0]["url"], "https://goldenkite.net/variant_1080.m3u8")
            self.assertEqual(streams[0]["headers"]["User-Agent"], MOVY_UA)
            self.assertEqual(streams[0]["headers"]["Origin"], MOVY_SITE)


class ProxyDetectionTests(unittest.TestCase):
    def test_get_warp_proxy_fallback_to_none_when_offline(self):
        with patch.dict(os.environ, {}, clear=True), \
             patch("movy_extractor.check_proxy_listening", return_value=False):
            proxy = get_warp_proxy()
            self.assertIsNone(proxy)
            self.assertFalse(is_warp_active())

    def test_get_warp_proxy_local_wireproxy_listening(self):
        def fake_listening(host, port, timeout=0.3):
            return host == "127.0.0.1" and port == 1080

        with patch.dict(os.environ, {}, clear=True), \
             patch("movy_extractor.check_proxy_listening", side_effect=fake_listening):
            proxy = get_warp_proxy()
            self.assertEqual(proxy, "socks5://127.0.0.1:1080")
            self.assertTrue(is_warp_active())

    def test_get_warp_proxy_env_variable(self):
        with patch.dict(os.environ, {"SIDECAR_WARP_PROXY": "socks5://10.0.0.5:1080"}), \
             patch("movy_extractor.is_proxy_alive", return_value=True):
            proxy = get_warp_proxy()
            self.assertEqual(proxy, "socks5://10.0.0.5:1080")
            self.assertTrue(is_warp_active())


class GoldenkiteSyncRoutingTests(unittest.IsolatedAsyncioTestCase):
    def test_is_movy_request_detection(self):
        self.assertTrue(is_movy_request("https://goldenkite.net/hls/stream.m3u8"))
        self.assertTrue(is_movy_request("https://vividstone.net/playlist.m3u8"))
        self.assertTrue(is_movy_request("https://www.movy.sx/watch/123"))
        self.assertTrue(is_movy_request("https://vidfast.vc/stream.m3u8"))
        self.assertTrue(is_movy_request("https://api.wecollege.net/sources"))

        # Referer / Origin header detection
        self.assertTrue(is_movy_request("https://cdn.example.com/stream.m3u8", {"Referer": "https://www.movy.sx/"}))
        self.assertTrue(is_movy_request("https://cdn.example.com/stream.m3u8", {"Origin": "https://vidfast.vc"}))

        # Standard non-movy URLs
        self.assertFalse(is_movy_request("https://vixsrc.to/playlist.m3u8"))
        self.assertFalse(is_movy_request("https://cdn.example.test/segment.ts"))

    async def test_get_routes_goldenkite_via_warp(self):
        engine = SyncEngine(FakeAudio(), FakeOffsets())

        captured_kwargs = {}
        captured_headers = {}

        class DummyClient:
            async def __aenter__(self):
                return self
            async def __aexit__(self, exc_type, exc_val, exc_tb):
                pass
            async def get(self, url, headers=None):
                captured_headers.update(headers or {})
                resp = MagicMock()
                resp.status_code = 200
                resp.headers = {}
                return resp

        def fake_httpx_init(**kwargs):
            captured_kwargs.update(kwargs)
            return DummyClient()

        target_url = "https://goldenkite.net/playlist.m3u8"
        with patch.dict(os.environ, {"SIDECAR_WARP_PROXY": "socks5://127.0.0.1:1080"}), \
             patch("sync.httpx.AsyncClient", side_effect=fake_httpx_init), \
             patch("sync.resolves_publicly", new=AsyncMock(return_value=True)):

            await engine._get(target_url, {})

            self.assertEqual(captured_kwargs.get("proxy"), "socks5://127.0.0.1:1080")
            self.assertEqual(captured_headers.get("User-Agent"), MOVY_UA)
            self.assertEqual(captured_headers.get("Referer"), "https://www.movy.sx/")

    async def test_get_routes_vidfast_with_vidfast_referer(self):
        engine = SyncEngine(FakeAudio(), FakeOffsets())
        captured_headers = {}

        class DummyClient:
            async def __aenter__(self):
                return self
            async def __aexit__(self, exc_type, exc_val, exc_tb):
                pass
            async def get(self, url, headers=None):
                captured_headers.update(headers or {})
                resp = MagicMock()
                resp.status_code = 200
                resp.headers = {}
                return resp

        target_url = "https://vidfast.vc/live/playlist.m3u8"
        with patch.dict(os.environ, {"SIDECAR_WARP_PROXY": "socks5://127.0.0.1:1080"}), \
             patch("sync.httpx.AsyncClient", side_effect=lambda **kw: DummyClient()), \
             patch("sync.resolves_publicly", new=AsyncMock(return_value=True)):

            await engine._get(target_url, {})
            self.assertEqual(captured_headers.get("Referer"), "https://vidfast.vc/")

    async def test_get_does_not_fallback_to_tor_on_403_for_goldenkite(self):
        engine = SyncEngine(FakeAudio(), FakeOffsets())

        class DummyClient403:
            async def __aenter__(self):
                return self
            async def __aexit__(self, exc_type, exc_val, exc_tb):
                pass
            async def get(self, url, headers=None):
                mock_resp = MagicMock()
                mock_resp.status_code = 403
                mock_resp.headers = {}
                mock_resp.raise_for_status = MagicMock(side_effect=httpx.HTTPStatusError("403 Forbidden", request=None, response=mock_resp))
                return mock_resp

        tor_proxy_used = []

        def fake_httpx_init(**kwargs):
            if kwargs.get("proxy") == "socks5://tor-node:9050":
                tor_proxy_used.append(True)
            return DummyClient403()

        with patch.dict(os.environ, {"TOR_PROXY_URLS": "socks5://tor-node:9050"}), \
             patch("sync.httpx.AsyncClient", side_effect=fake_httpx_init), \
             patch("sync.resolves_publicly", new=AsyncMock(return_value=True)):

            with self.assertRaises(RuntimeError):
                await engine._get("https://goldenkite.net/playlist.m3u8", {})

            # Must NOT attempt Tor fallback for Goldenkite
            self.assertEqual(len(tor_proxy_used), 0)

    async def test_measure_re_resolves_movy_stream(self):
        engine = SyncEngine(FakeAudio(), FakeOffsets())

        mock_sources = [
            {
                "resolution": 1080,
                "url": "https://goldenkite.net/stream_fresh_1080.m3u8",
                "headers": {
                    "User-Agent": MOVY_UA,
                    "Referer": "https://www.movy.sx/",
                    "Origin": "https://www.movy.sx",
                },
                "server": "miami",
            }
        ]

        # Mock video entries and media start time
        engine._video_entries = AsyncMock(return_value=(
            [{"url": "https://goldenkite.net/seg0.ts", "duration": 10.0, "start": 0.0}],
            None,
        ))
        engine._vidfast_sample_url = AsyncMock(return_value=None)
        engine._media_start_time = AsyncMock(return_value=0.0)
        engine._decode_audio = AsyncMock(return_value=("/tmp/dummy.m3u8", 0.0, 10.0))
        engine._pcm = AsyncMock(return_value=None)
        engine._envelope = MagicMock(return_value=[0.0] * 100)

        payload = {
            "media_key": "movie:tt0816692:0:0",
            "provider": "movy",
            "resolution": 1080,
            "video_url": "https://goldenkite.net/client_expired_or_ip_locked.m3u8",
            "audio_hid": "a" * 16,
        }

        with patch("sync.extract_movy_stream", new=AsyncMock(return_value=mock_sources)) as mock_extract:
            await engine.measure(payload)

            mock_extract.assert_called_once_with("movie:tt0816692:0:0", preferred_server=None)
            # Verify stream URL and headers were re-resolved to fresh values
            self.assertEqual(payload["video_url"], "https://goldenkite.net/stream_fresh_1080.m3u8")
            self.assertEqual(payload["video_headers"]["User-Agent"], MOVY_UA)
            self.assertEqual(payload["server"], "miami")


class AppEndpointsTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_warp_status_endpoint_direct(self):
        with patch("app.get_warp_proxy", return_value=None):
            resp = self.client.get("/api/warp-status")
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertFalse(data["active"])
            self.assertEqual(data["badge"], "Connessione Diretta")

    def test_warp_status_endpoint_active(self):
        with patch("app.get_warp_proxy", return_value="socks5://127.0.0.1:1080"):
            resp = self.client.get("/api/warp-status")
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertTrue(data["active"])
            self.assertEqual(data["proxy"], "socks5://127.0.0.1:1080")
            self.assertEqual(data["mode"], "wireproxy")
            self.assertEqual(data["badge"], "🛡️ WARP Integrato (WireProxy): Attivo")

    def test_landing_page_contains_warp_badge(self):
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("warpBadge", resp.text)
        self.assertIn("/api/warp-status", resp.text)
