import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Mock GPU/inference dependencies if running on host without faster-whisper
for mod in ("numpy", "faster_whisper"):
    if mod not in sys.modules:
        sys.modules[mod] = MagicMock()

import aiohttp
from aiohttp import web

import bot
import scripts.generate_certs as gen_certs
import whisper_server


class WhisperSSLTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.certs_dir = Path(self.temp_dir.name) / "certs"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_cert_generation_and_loading(self):
        cert_path, key_path = gen_certs.generate_certs(
            out_dir=self.certs_dir,
            force=True,
        )
        self.assertTrue(cert_path.is_file())
        self.assertTrue(key_path.is_file())

        # Verify server SSLContext loads
        with (
            patch.object(whisper_server, "SSL_CERT_PATH", cert_path),
            patch.object(whisper_server, "SSL_KEY_PATH", key_path),
        ):
            server_ctx = whisper_server.create_ssl_context()
            self.assertIsNotNone(server_ctx)

        # Verify client verifier loads
        verifier = bot.get_whisper_ssl_verifier(cert_path)
        self.assertIsInstance(verifier, aiohttp.Fingerprint)
        self.assertEqual(len(verifier.fingerprint), 32)

    async def test_websocket_handshake_with_certificate_pinning(self):
        # 1. Generate legitimate certs
        cert_path, key_path = gen_certs.generate_certs(
            out_dir=self.certs_dir,
            force=True,
        )

        # 2. Setup mock server using create_ssl_context
        with (
            patch.object(whisper_server, "SSL_CERT_PATH", cert_path),
            patch.object(whisper_server, "SSL_KEY_PATH", key_path),
        ):
            server_ctx = whisper_server.create_ssl_context()

        app = web.Application()

        async def ws_handler(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            await ws.send_str("AUTHENTICATED_SECURE_CHANNEL")
            await ws.close()
            return ws

        app.router.add_get("/ws", ws_handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=server_ctx)
        await site.start()
        port = next(iter(site._server.sockets)).getsockname()[1]

        try:
            # 3. Client connects with the pinned certificate
            verifier = bot.get_whisper_ssl_verifier(cert_path)
            async with (
                aiohttp.ClientSession() as session,
                session.ws_connect(f"wss://127.0.0.1:{port}/ws", ssl=verifier) as ws,
            ):
                msg = await ws.receive_str()
                self.assertEqual(msg, "AUTHENTICATED_SECURE_CHANNEL")

            # 4. Client connects with an attacker / untrusted certificate
            bad_dir = Path(self.temp_dir.name) / "bad_certs"
            bad_cert_path, _ = gen_certs.generate_certs(
                out_dir=bad_dir,
                force=True,
            )
            bad_verifier = bot.get_whisper_ssl_verifier(bad_cert_path)

            async with aiohttp.ClientSession() as session:
                with self.assertRaises(aiohttp.ServerFingerprintMismatch):
                    async with session.ws_connect(
                        f"wss://127.0.0.1:{port}/ws", ssl=bad_verifier
                    ):
                        pass

        finally:
            await runner.cleanup()

    def test_missing_cert_raises_error(self):
        missing = self.certs_dir / "nonexistent.pem"
        with self.assertRaises(FileNotFoundError):
            bot.get_whisper_ssl_verifier(missing)

    def test_get_whisper_ws_url(self):
        self.assertEqual(
            bot.get_whisper_ws_url("https://kasupc:8000"),
            "wss://kasupc:8000/ws",
        )


if __name__ == "__main__":
    unittest.main()
