import asyncio
import audioop
import hashlib
import logging
import os
import ssl
import struct
import threading
import time
from pathlib import Path

import aiohttp
import davey
import discord
import yt_dlp
from discord.ext import commands, voice_recv

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

UPLOADS_DIR = Path("uploads").resolve()
AUDIO_DIR = Path(
    os.getenv("AUDIO_DIR", str(Path(__file__).with_name("audio")))
).resolve()
WHISPER_URL = os.getenv("WHISPER_URL", "https://kasupc:8000").rstrip("/")
WHISPER_CERT_FILE = Path(os.getenv("WHISPER_CERT_FILE", "certs/cert.pem"))
WHISPER_RECONNECT_INTERVAL = 10.0
CLIP_COOLDOWN_SECONDS = 1.0
ALLOWED_EXTENSIONS = {"mp3", "m4a", "wav", "flac", "ogg", "aac", "opus", "wma"}


def is_allowed_audio_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def get_whisper_ws_url(base_url: str) -> str:
    return "wss://" + base_url.removeprefix("https://").rstrip("/") + "/ws"


def get_whisper_ssl_verifier(
    cert_path: Path = WHISPER_CERT_FILE,
) -> aiohttp.Fingerprint:
    raw_text = cert_path.read_text(encoding="ascii")
    der_bytes = ssl.PEM_cert_to_DER_cert(raw_text)
    fingerprint = hashlib.sha256(der_bytes).digest()
    return aiohttp.Fingerprint(fingerprint)


class InterruptiblePCMVolumeTransformer(discord.PCMVolumeTransformer):
    """PCMVolumeTransformer that can pause the main source to play a short interrupt clip."""

    def __init__(self, original: discord.AudioSource, volume: float = 1.0):
        super().__init__(original, volume=volume)
        self._interrupt_source: discord.AudioSource | None = None
        self._interrupt_lock = threading.Lock()

    def interrupt(self, source: discord.AudioSource) -> None:
        with self._interrupt_lock:
            if self._interrupt_source is not None:
                self._interrupt_source.cleanup()
            self._interrupt_source = source

    def read(self) -> bytes:
        with self._interrupt_lock:
            if self._interrupt_source is not None:
                data = self._interrupt_source.read()
                if data:
                    return audioop.mul(data, 2, min(self.volume, 2.0))
                self._interrupt_source.cleanup()
                self._interrupt_source = None
        return super().read()

    def cleanup(self) -> None:
        with self._interrupt_lock:
            if self._interrupt_source is not None:
                self._interrupt_source.cleanup()
                self._interrupt_source = None
        super().cleanup()


async def safe_voice_hook(ws: discord.gateway.DiscordVoiceWebSocket, msg: dict) -> None:
    op = msg.get("op")
    data = msg.get("d") or {}
    if op == 12 and "streams" not in data:
        vc = ws._connection.voice_client
        if "user_id" in data and "audio_ssrc" in data:
            vc._add_ssrc(int(data["user_id"]), data["audio_ssrc"])
        return
    try:
        await voice_recv.gateway.hook(ws, msg)
    except Exception:
        logger.debug("Ignoring error in voice_recv gateway hook", exc_info=True)


class RubyVoiceRecvClient(voice_recv.VoiceRecvClient):
    def create_connection_state(self) -> discord.voice_state.VoiceConnectionState:
        return discord.voice_state.VoiceConnectionState(self, hook=safe_voice_hook)


class RubyChanSink(voice_recv.AudioSink):
    CHUNK_FLUSH_BYTES = 3200  # 100ms of 16kHz 16-bit mono PCM (5 x 20ms frames)

    def __init__(self, cog: "Music", vc: RubyVoiceRecvClient):
        super().__init__()
        self.cog = cog
        self.vc = vc
        self._decoders: dict[int, discord.opus.Decoder] = {}
        self._rate_states: dict[int, tuple | None] = {}
        self._chunks: dict[int, bytearray] = {}
        self._lock = threading.Lock()

    def wants_opus(self) -> bool:
        return True

    def write(
        self, user: discord.User | discord.Member | None, data: voice_recv.VoiceData
    ) -> None:
        if not self.cog.whisper_online or user is None or user.bot:
            return

        opus_bytes = data.opus
        if not opus_bytes or opus_bytes == b"\xf8\xff\xfe":
            return

        conn = self.vc._connection
        if (
            conn.dave_protocol_version > 0
            and conn.dave_session is not None
            and conn.dave_session.ready
        ):
            try:
                opus_bytes = conn.dave_session.decrypt(
                    user.id, davey.MediaType.audio, opus_bytes
                )
            except (RuntimeError, ValueError):
                return

        payload_to_send: bytes | None = None
        with self._lock:
            decoder = self._decoders.get(user.id)
            if decoder is None:
                if not discord.opus.is_loaded():
                    discord.opus._load_default()
                decoder = self._decoders[user.id] = discord.opus.Decoder()

            try:
                pcm_48k_stereo = decoder.decode(opus_bytes, fec=False)
            except discord.opus.OpusError:
                return

            mono_48k = audioop.tomono(pcm_48k_stereo, 2, 0.5, 0.5)
            pcm_16k, state = audioop.ratecv(
                mono_48k, 2, 1, 48000, 16000, self._rate_states.get(user.id)
            )
            self._rate_states[user.id] = state

            buf = self._chunks.setdefault(user.id, bytearray())
            buf.extend(pcm_16k)
            if len(buf) >= self.CHUNK_FLUSH_BYTES:
                payload_to_send = struct.pack(">Q", user.id) + bytes(buf)
                buf.clear()

        if payload_to_send is not None:
            self.cog.enqueue_audio_chunk(payload_to_send)

    @voice_recv.AudioSink.listener()
    def on_voice_member_speaking_stop(self, member: discord.Member) -> None:
        payload_to_send: bytes | None = None
        with self._lock:
            buf = self._chunks.pop(member.id, None)
            self._rate_states.pop(member.id, None)
            if buf:
                payload_to_send = struct.pack(">Q", member.id) + bytes(buf)
        if payload_to_send is not None:
            self.cog.enqueue_audio_chunk(payload_to_send)

    @voice_recv.AudioSink.listener()
    def on_voice_member_disconnect(
        self, member: discord.Member | None, ssrc: int | None
    ) -> None:
        if member is not None:
            with self._lock:
                self._chunks.pop(member.id, None)
                self._rate_states.pop(member.id, None)
                self._decoders.pop(member.id, None)

    def cleanup(self) -> None:
        with self._lock:
            self._chunks.clear()
            self._rate_states.clear()
            self._decoders.clear()


class Music(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.youtube_cache_dir = UPLOADS_DIR
        os.makedirs(self.youtube_cache_dir, exist_ok=True)
        self.whisper_online: bool = False
        self._http_session: aiohttp.ClientSession | None = None
        self._ws_task: asyncio.Task | None = None
        self._ws_Wake_event = asyncio.Event()
        self._audio_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=200)
        self._last_clip_time: float = 0.0

    async def cog_load(self) -> None:
        self._http_session = aiohttp.ClientSession()
        self._ws_task = asyncio.create_task(self._whisper_ws_loop())

    async def cog_unload(self) -> None:
        if self._ws_task is not None:
            self._ws_task.cancel()
            self._ws_task = None
        if self._http_session is not None:
            await self._http_session.close()
            self._http_session = None

    def enqueue_audio_chunk(self, payload: bytes) -> None:
        if not self.whisper_online:
            return

        def _put() -> None:
            if self._audio_queue.full():
                try:
                    self._audio_queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                self._audio_queue.put_nowait(payload)
            except asyncio.QueueFull:
                pass

        self.bot.loop.call_soon_threadsafe(_put)

    def _set_whisper_online(self, online: bool) -> None:
        if self.whisper_online == online:
            return
        self.whisper_online = online
        if online:
            logger.info(
                "Connected to Whisper stream at %s; real-time voice recognition enabled.",
                WHISPER_URL,
            )
        else:
            logger.info(
                "Whisper server at %s offline; voice recognition disabled.",
                WHISPER_URL,
            )
            while not self._audio_queue.empty():
                try:
                    self._audio_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
        self._sync_voice_listeners()

    def _sync_voice_listeners(self) -> None:
        for vc in self.bot.voice_clients:
            if not isinstance(vc, RubyVoiceRecvClient) or not vc.is_connected():
                continue
            if self.whisper_online and not vc.is_listening():
                vc.listen(RubyChanSink(self, vc))
            elif not self.whisper_online and vc.is_listening():
                vc.stop_listening()

    async def _whisper_ws_loop(self) -> None:
        ws_url = get_whisper_ws_url(WHISPER_URL)
        while True:
            try:
                if not self.bot.voice_clients:
                    self._set_whisper_online(False)
                    self._ws_Wake_event.clear()
                    await self._ws_Wake_event.wait()
                    continue

                if self._http_session is None or self._http_session.closed:
                    self._http_session = aiohttp.ClientSession()

                timeout = aiohttp.ClientTimeout(total=None, sock_connect=2.0)
                ssl_verifier = get_whisper_ssl_verifier()
                async with self._http_session.ws_connect(
                    ws_url, timeout=timeout, heartbeat=15.0, ssl=ssl_verifier
                ) as ws:
                    self._set_whisper_online(True)

                    async def _sender() -> None:
                        while not ws.closed:
                            chunk = await self._audio_queue.get()
                            await ws.send_bytes(chunk)

                    sender_task = asyncio.create_task(_sender())
                    try:
                        async for msg in ws:
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                data = msg.json()
                                msg_type = data.get("type")
                                text = data.get("text", "")
                                user_id = data.get("user_id")
                                latency_ms = data.get("latency_ms")
                                if msg_type == "trigger":
                                    clip_name = data.get("clip") or "hai_ruby"
                                    state = data.get("state")
                                    logger.info(
                                        "AiScReam trigger '%s' (state=%s) from user %s (%sms): %r",
                                        clip_name,
                                        state,
                                        user_id,
                                        latency_ms,
                                        text,
                                    )
                                    for vc in self.bot.voice_clients:
                                        if vc.is_connected():
                                            await self.trigger_clip(vc, clip_name)
                            elif msg.type in (
                                aiohttp.WSMsgType.CLOSED,
                                aiohttp.WSMsgType.ERROR,
                            ):
                                break
                    finally:
                        sender_task.cancel()
            except aiohttp.ServerFingerprintMismatch as e:
                logger.critical(
                    "Whisper TLS security error: server certificate fingerprint (%s) does not match pinned certificate (%s) for %s:%d! Connection refused.",
                    e.got.hex(),
                    e.expected.hex(),
                    e.host,
                    e.port,
                )
                self._set_whisper_online(False)
            except asyncio.CancelledError:
                break
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
                self._set_whisper_online(False)
            except Exception:
                logger.exception("Unexpected error in Whisper WebSocket loop")
                self._set_whisper_online(False)

            if self.bot.voice_clients:
                await asyncio.sleep(WHISPER_RECONNECT_INTERVAL)

    async def _connect_and_listen(
        self, channel: discord.VoiceChannel
    ) -> RubyVoiceRecvClient:
        vc = await channel.connect(cls=RubyVoiceRecvClient, self_deaf=False)
        self._ws_Wake_event.set()
        if self.whisper_online and not vc.is_listening():
            vc.listen(RubyChanSink(self, vc))
        return vc

    def _get_audio_clip_path(self, clip_name: str) -> Path | None:
        safe_name = Path(clip_name).name
        candidate = AUDIO_DIR / f"{safe_name}.wav"
        if candidate.is_file():
            return candidate
        return None

    async def trigger_clip(self, vc: discord.VoiceClient, clip_name: str) -> None:
        now = time.monotonic()
        if now - self._last_clip_time < CLIP_COOLDOWN_SECONDS:
            return

        clip_path = self._get_audio_clip_path(clip_name)
        if clip_path is None:
            logger.warning("Audio clip '%s' not found in %s", clip_name, AUDIO_DIR)
            return

        self._last_clip_time = now
        clip_source = discord.FFmpegPCMAudio(str(clip_path))

        if vc.is_playing() and isinstance(vc.source, InterruptiblePCMVolumeTransformer):
            logger.info("Pausing current track to play '%s' response", clip_name)
            vc.source.interrupt(clip_source)
        elif not vc.is_playing():
            logger.info("Playing '%s' response", clip_name)
            vc.play(
                InterruptiblePCMVolumeTransformer(clip_source),
                after=lambda err: (
                    logger.error("Player error: %s", err) if err else None
                ),
            )
        else:
            clip_source.cleanup()

    @commands.hybrid_command()
    async def join(self, ctx: commands.Context, *, channel: discord.VoiceChannel):
        """Joins a voice channel"""

        if ctx.voice_client is not None:
            await ctx.voice_client.move_to(channel)
            self._ws_Wake_event.set()
            if (
                isinstance(ctx.voice_client, RubyVoiceRecvClient)
                and self.whisper_online
                and not ctx.voice_client.is_listening()
            ):
                ctx.voice_client.listen(RubyChanSink(self, ctx.voice_client))
        else:
            await self._connect_and_listen(channel)

        await ctx.send(f"Joined {channel.mention}")

    def is_youtube_url(self, url: str) -> bool:
        return url.startswith(("http://", "https://")) and (
            "youtube.com" in url or "youtu.be" in url
        )

    @staticmethod
    def _get_youtube_video_id(url: str) -> str:
        from urllib.parse import parse_qs, urlparse

        parsed = urlparse(url)
        hostname = parsed.hostname or ""

        if hostname in ("youtu.be", "www.youtu.be"):
            return parsed.path.lstrip("/")

        if hostname in ("youtube.com", "www.youtube.com", "m.youtube.com"):
            qs = parse_qs(parsed.query)
            if "v" in qs:
                return qs["v"][0]
            if parsed.path.startswith("/shorts/"):
                parts = parsed.path.strip("/").split("/")
                return parts[1] if len(parts) > 1 else ""

        return ""

    def _get_cached_youtube_audio(self, url: str) -> str | None:
        video_id = self._get_youtube_video_id(url)
        if not video_id:
            return None

        for candidate in os.listdir(self.youtube_cache_dir):
            if candidate.startswith(video_id + "."):
                path = os.path.join(self.youtube_cache_dir, candidate)
                if os.path.exists(path):
                    return path
        return None

    def download_youtube_audio(self, url: str) -> str:
        if yt_dlp is None:
            raise RuntimeError("yt-dlp is not installed, please pip install yt-dlp")

        cached = self._get_cached_youtube_audio(url)
        if cached:
            return cached

        ydl_opts = {
            "format": "bestaudio/best",
            "outtmpl": os.path.join(self.youtube_cache_dir, "%(id)s.%(ext)s"),
            "quiet": True,
            "noplaylist": True,
            "nocheckcertificate": True,
            "overwrites": False,
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        cached = self._get_cached_youtube_audio(url)
        if cached:
            return cached

        raise FileNotFoundError(f"Unable to locate downloaded YouTube audio for {url}")

    @commands.hybrid_command()
    async def play(self, ctx: commands.Context, *, query: str):
        """Plays a file from local filesystem, or downloads and plays YouTube when URL is passed."""

        if self.is_youtube_url(query):
            cached = self._get_cached_youtube_audio(query)
            if cached:
                path = cached
                await ctx.send("Playing cached audio from YouTube.")
            else:
                await ctx.send("Downloading audio from YouTube, please wait...")
                path = await asyncio.to_thread(self.download_youtube_audio, query)

            if not os.path.exists(path):
                raise FileNotFoundError(
                    f"Downloaded file not found at expected path: {path}"
                )

            source = InterruptiblePCMVolumeTransformer(
                discord.FFmpegPCMAudio(str(path))
            )

            def _after(err):
                if err:
                    logger.error("Player error: %s", err)

            ctx.voice_client.play(source, after=_after)
            await ctx.send(f"Now playing from YouTube: {query}")
            return

        file_path = (UPLOADS_DIR / query).resolve()
        if UPLOADS_DIR in file_path.parents and file_path.is_file():
            source = InterruptiblePCMVolumeTransformer(
                discord.FFmpegPCMAudio(str(file_path))
            )
            ctx.voice_client.play(
                source, after=lambda e: print(f"Player error: {e}") if e else None
            )
            await ctx.send(f"Now playing: `{query}`")
        else:
            await ctx.send(f"File does not exist: `{query}`")

    @commands.hybrid_command()
    async def playfile(self, ctx: commands.Context, file: discord.Attachment):
        """Uploads and plays an audio file directly."""

        filename = Path(file.filename).name
        if not is_allowed_audio_file(filename):
            await ctx.send(f"Unsupported file extension in file `{filename}`")
            return

        file_path = (UPLOADS_DIR / filename).resolve()
        try:
            await file.save(file_path)
        except (discord.HTTPException, OSError) as e:
            logger.error("Failed to save file %s: %s", filename, e)
            await ctx.send(f"Failed to save `{filename}`")
            return

        source = InterruptiblePCMVolumeTransformer(
            discord.FFmpegPCMAudio(str(file_path))
        )

        def _after(err):
            if err:
                logger.error("Player error: %s", err)

        ctx.voice_client.play(source, after=_after)
        await ctx.send(f"Now playing: `{filename}`")

    @commands.hybrid_command()
    async def volume(self, ctx: commands.Context, volume: int):
        """Changes the player's volume"""

        if ctx.voice_client is None:
            return await ctx.send("Not connected to a voice channel.")

        if ctx.voice_client.source is None:
            return await ctx.send("Nothing is currently playing.")

        ctx.voice_client.source.volume = volume / 100
        await ctx.send(f"Changed volume to {volume}%")

    @commands.hybrid_command()
    async def stop(self, ctx: commands.Context):
        """Stops and disconnects the bot from voice"""

        if ctx.voice_client is None:
            return await ctx.send("Not connected to a voice channel.")

        await ctx.voice_client.disconnect()
        await ctx.send("Disconnected from voice channel.")

    @play.before_invoke
    @playfile.before_invoke
    async def ensure_voice(self, ctx: commands.Context):
        await ctx.defer()
        if ctx.voice_client is None:
            if ctx.author.voice:
                await self._connect_and_listen(ctx.author.voice.channel)
            else:
                await ctx.send("You are not connected to a voice channel.")
                raise commands.CommandError("Author not connected to a voice channel.")
        elif ctx.voice_client.is_playing():
            if isinstance(ctx.voice_client, RubyVoiceRecvClient):
                ctx.voice_client.stop_playing()
            else:
                ctx.voice_client.stop()

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ):
        voice_client = member.guild.voice_client
        if voice_client is None or voice_client.channel is None:
            return

        if (
            before.channel == voice_client.channel
            and after.channel != voice_client.channel
            and not any(not m.bot for m in voice_client.channel.members)
        ):
            if voice_client.is_playing():
                voice_client.stop()
            await voice_client.disconnect()


intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(
    command_prefix=commands.when_mentioned_or("!"),
    description="Relatively simple music bot example",
    intents=intents,
)


@bot.event
async def on_ready():
    # Tell the type checker that User is filled up at this point
    assert bot.user is not None

    synced = await bot.tree.sync()
    print(f"Synced {len(synced)} slash command(s)")
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    print("------")


async def start_bot():
    token = os.getenv("DISCORD_BOT_TOKEN")
    if not token:
        raise ValueError("DISCORD_BOT_TOKEN environment variable is not set")

    async with bot:
        await bot.add_cog(Music(bot))
        await bot.start(token)


def main():
    asyncio.run(start_bot())


if __name__ == "__main__":
    main()
