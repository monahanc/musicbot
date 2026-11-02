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
just setup          # Install local Python dependencies with uv
just test           # Run unit and integration tests
just format         # Lint and format code with ruff and black
just run            # Run the bot locally
just build          # Build Docker image locally
just generate-certs # Generate Whisper TLS keypair and self-signed cert in certs/
just cert-push      # Push whisper cert to rubychan@nas:/home/rubychan/certs/
just deploy-init    # Copy compose.yaml and .env to rubychan@nas:/opt/stacks/rubychan
just deploy         # Build, push to registry, and redeploy on rubychan@nas
just status         # Check container status on rubychan@nas
just logs           # Tail container logs on rubychan@nas
just whisper-up     # Build and start GPU faster-whisper server on kasupc with TLS
just whisper-down   # Stop GPU faster-whisper server on kasupc
just whisper-logs   # Tail GPU faster-whisper server logs on kasupc
```

## Security

Communications between `bot.py` and `whisper_server.py` are encrypted over TLS (`https://` / `wss://`) using custom certificate pinning:
- A private key and self-signed certificate are generated locally for `whisper_server` via `scripts/generate_certs.py`.
- The private key (`certs/key.pem`) stays exclusively on the server (`kasupc`).
- Only the public certificate (`certs/cert.pem`) is deployed to the client (`rubychan` on `nas`).
- The client strictly pins the certificate fingerprint and will refuse connections to any server not presenting this exact certificate (standard public CA / domain-validated certificates are explicitly rejected).
