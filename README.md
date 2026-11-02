# rubychan

Discord music bot.

## Configuration

Set `DISCORD_BOT_TOKEN` in `mise.local.toml` (gitignored):

```toml
[env]
DISCORD_BOT_TOKEN = "your-token-here"
```

## Commands

```sh
just setup         # Install local Python dependencies with uv
just format        # Lint and format code with ruff and black
just run           # Run the bot locally
just build         # Build Docker image locally
just deploy        # Build, start whisper on kasupc, sync audio, and deploy to rubychan@nas
just audio-push    # Upload local audio/*.wav clips to rubychan@nas:/home/rubychan/audio
just audio-pull    # Download audio/*.wav clips from rubychan@nas:/home/rubychan/audio
just status        # Check container status on rubychan@nas
just logs          # Tail container logs on rubychan@nas
just whisper-up    # Build and start GPU faster-whisper server on kasupc
just whisper-down  # Stop GPU faster-whisper server on kasupc
just whisper-logs  # Tail GPU faster-whisper server logs on kasupc
```
