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
just setup    # Install local Python dependencies with uv
just run      # Run the bot locally
just build    # Build Docker image locally
just deploy   # Build and deploy to rubychan@nas via Docker Compose over SSH
just status   # Check container status on rubychan@nas
just logs     # Tail container logs on rubychan@nas
```
