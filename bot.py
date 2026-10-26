import asyncio
import logging
import os
from pathlib import Path

import discord
import yt_dlp
from discord.ext import commands

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

UPLOADS_DIR = Path("uploads").resolve()


class Music(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.youtube_cache_dir = UPLOADS_DIR
        os.makedirs(self.youtube_cache_dir, exist_ok=True)

    @commands.hybrid_command()
    async def join(self, ctx: commands.Context, *, channel: discord.VoiceChannel):
        """Joins a voice channel"""

        if ctx.voice_client is not None:
            await ctx.voice_client.move_to(channel)
        else:
            await channel.connect()

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

            source = discord.PCMVolumeTransformer(discord.FFmpegPCMAudio(str(path)))

            def _after(err):
                if err:
                    logger.error("Player error: %s", err)

            ctx.voice_client.play(source, after=_after)
            await ctx.send(f"Now playing from YouTube: {query}")
            return

        file_path = (UPLOADS_DIR / query).resolve()
        if UPLOADS_DIR in file_path.parents and file_path.is_file():
            source = discord.PCMVolumeTransformer(
                discord.FFmpegPCMAudio(str(file_path))
            )
            ctx.voice_client.play(
                source, after=lambda e: print(f"Player error: {e}") if e else None
            )
            await ctx.send(f"Now playing: `{query}`")
        else:
            await ctx.send(f"File does not exist: `{query}`")

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
    async def ensure_voice(self, ctx: commands.Context):
        await ctx.defer()
        if ctx.voice_client is None:
            if ctx.author.voice:
                await ctx.author.voice.channel.connect()
                await ctx.guild.change_voice_state(
                    channel=ctx.author.voice.channel, self_mute=False, self_deaf=True
                )
            else:
                await ctx.send("You are not connected to a voice channel.")
                raise commands.CommandError("Author not connected to a voice channel.")
        elif ctx.voice_client.is_playing():
            ctx.voice_client.stop()


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


@bot.event
async def on_message(message):
    # Ignore messages from the bot itself to prevent infinite loops
    if message.author == bot.user:
        return

    # Check if the message was sent in a DM channel
    if isinstance(message.channel, discord.DMChannel):
        print(f"Received DM from {message.author}: {message.content}")
        await message.channel.send(f"I received your DM: {message.content}")

        # Check for attachments
        if message.attachments:
            print(f"Found {len(message.attachments)} attachment(s).")
            allowed_extensions = {
                "mp3",
                "m4a",
                "wav",
                "flac",
                "ogg",
                "aac",
                "opus",
                "wma",
            }

            def allowed_file(filename):
                return (
                    "." in filename
                    and filename.rsplit(".", 1)[1].lower() in allowed_extensions
                )

            for attachment in message.attachments:
                if allowed_file(attachment.filename):
                    # You can access properties like attachment.filename, attachment.url, attachment.size
                    print(f"Downloading file: {attachment.filename}")
                    try:
                        # Save the file to disk
                        await attachment.save(str(UPLOADS_DIR / attachment.filename))
                        print(f"Successfully saved {attachment.filename}")
                        await message.channel.send(f"Saved! {attachment.filename}")
                    except (discord.HTTPException, OSError) as e:
                        print(f"Failed to save file: {e}")
                        await message.channel.send(
                            f"Failed to save {attachment.filename}"
                        )
                else:
                    await message.channel.send(
                        f"Unsupported file extension in file {attachment.filename}"
                    )
        else:
            print("No file attachments in this DM.")

    # This line is crucial! It allows your bot to process other commands
    # you might have defined elsewhere in your code (e.g., those starting with '!')
    await bot.process_commands(message)


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
