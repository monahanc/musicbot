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
    docker build --target bot -t rubychan:latest .

docker-run:
    mise exec -- docker run --rm -it -e DISCORD_BOT_TOKEN -v "{{justfile_directory()}}/uploads:/app/uploads" -v "{{justfile_directory()}}/audio:/app/audio:ro" -v "{{justfile_directory()}}/certs:/app/certs:ro" rubychan:latest

generate-certs:
    uv run python scripts/generate_certs.py

cert-push:
    ssh rubychan@nas "mkdir -p /home/rubychan/certs"
    scp certs/cert.pem rubychan@nas:/home/rubychan/certs/cert.pem

audio-push:
    ssh rubychan@nas "mkdir -p /home/rubychan/audio"
    scp audio/*.wav rubychan@nas:/home/rubychan/audio/

audio-pull:
    mkdir -p audio
    scp "rubychan@nas:/home/rubychan/audio/*.wav" audio/

deploy: generate-certs build cert-push whisper-up audio-push
    docker save rubychan:latest | ssh rubychan@nas docker load
    mise exec -- docker -H ssh://rubychan@nas compose up -d --force-recreate --no-build

status:
    docker -H ssh://rubychan@nas compose ps

logs:
    docker -H ssh://rubychan@nas compose logs -f

whisper-up: generate-certs
    docker compose --profile whisper up -d --build whisper

whisper-down:
    docker compose --profile whisper stop whisper
    docker compose --profile whisper rm -f whisper

whisper-logs:
    docker compose --profile whisper logs -f whisper
