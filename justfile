setup:
    uv sync

format:
    uv run ruff check --fix .
    uv run black .

run:
    mise exec -- uv run rubychan-bot

build:
    docker build -t rubychan:latest .

docker-run:
    mise exec -- docker run --rm -it -e DISCORD_BOT_TOKEN -v "{{justfile_directory()}}/uploads:/app/uploads" rubychan:latest

deploy: build
    docker save rubychan:latest | ssh rubychan@nas docker load
    mise exec -- docker -H ssh://rubychan@nas compose up -d --force-recreate

status:
    docker -H ssh://rubychan@nas compose ps

logs:
    docker -H ssh://rubychan@nas compose logs -f
