setup:
    uv sync

format:
    uv run ruff check --fix .
    uv run black .

run:
    mise exec -- uv run rubychan-bot

build:
    docker build --target bot -t rubychan:latest .

docker-run:
    mise exec -- docker run --rm -it -e DISCORD_BOT_TOKEN -v "{{justfile_directory()}}/uploads:/app/uploads" -v "{{justfile_directory()}}/audio:/app/audio:ro" rubychan:latest

audio-push:
    ssh rubychan@nas "mkdir -p /home/rubychan/audio"
    scp audio/*.wav rubychan@nas:/home/rubychan/audio/

audio-pull:
    mkdir -p audio
    scp "rubychan@nas:/home/rubychan/audio/*.wav" audio/

deploy: build whisper-up audio-push
    docker save rubychan:latest | ssh rubychan@nas docker load
    mise exec -- docker -H ssh://rubychan@nas compose up -d --force-recreate --no-build

status:
    docker -H ssh://rubychan@nas compose ps

logs:
    docker -H ssh://rubychan@nas compose logs -f

whisper-up:
    docker compose --profile whisper up -d --build whisper

whisper-down:
    docker compose --profile whisper stop whisper
    docker compose --profile whisper rm -f whisper

whisper-logs:
    docker compose --profile whisper logs -f whisper
