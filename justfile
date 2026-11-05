image := "registry.connormonahan.net/rubychan"
git_hash := `git rev-parse --short HEAD`

setup:
    uv sync

test:
    uv run python -m unittest discover -s tests

format:
    uv run ruff check --fix .
    uv run black .

run:
    mise exec -- uv run rubychan-bot

build:
    docker build --target bot -t {{ image }}:{{ git_hash }} -t {{ image }}:latest .

docker-run:
    mise exec -- docker run --rm -it -e DISCORD_BOT_TOKEN -v "{{ justfile_directory() }}/uploads:/app/uploads" -v "{{ justfile_directory() }}/certs:/app/certs:ro" {{ image }}:latest

generate-certs:
    uv run python scripts/generate_certs.py

cert-push: generate-certs
    ssh rubychan@nas "mkdir -p /home/rubychan/certs"
    scp certs/cert.pem rubychan@nas:/home/rubychan/certs/cert.pem

docker-push tag=git_hash: build
    docker push {{ image }}:{{ tag }}

docker-push-latest: (docker-push "latest")

deploy-init: cert-push
    ssh rubychan@nas "mkdir -p /opt/stacks/rubychan"
    scp compose.yaml rubychan@nas:/opt/stacks/rubychan/compose.yaml
    mise exec -- sh -c 'printf "DISCORD_BOT_TOKEN=%s\n" "$DISCORD_BOT_TOKEN" | ssh rubychan@nas "umask 007 && cat > /opt/stacks/rubychan/.env"'

deploy: docker-push docker-push-latest cert-push
    ssh rubychan@nas "cd /opt/stacks/rubychan && docker compose pull && docker compose up -d --force-recreate"

status:
    ssh rubychan@nas "cd /opt/stacks/rubychan && docker compose ps"

logs:
    ssh rubychan@nas "cd /opt/stacks/rubychan && docker compose logs -f"

whisper-up: generate-certs
    docker build --target whisper -t rubychan-whisper:latest .
    mkdir -p /c/opt/stacks/rubychan-whisper/certs
    cp certs/* /c/opt/stacks/rubychan-whisper/certs/
    cp compose.whisper.yaml /c/opt/stacks/rubychan-whisper/compose.yaml
    cd /c/opt/stacks/rubychan-whisper && docker compose up -d

whisper-down:
    cd /c/opt/stacks/rubychan-whisper && docker compose down

whisper-logs:
    cd /c/opt/stacks/rubychan-whisper && docker compose logs -f
