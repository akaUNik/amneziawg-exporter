# Repository Guidelines

## Project Structure & Module Organization

- `exporter.py` contains the full application: configuration loading, AWG command execution, Redis-backed activity tracking, and Prometheus metric output.
- `Dockerfile`, `docker-compose.yml`, and `redis.conf` define the containerized runtime and local Redis dependency.
- `Makefile` contains image build and publish targets used by CI.
- `.github/workflows/docker-build-and-push.yml` builds and pushes Docker images on `main`.
- `amneziawg-exporter.service` is the systemd unit example.

There is currently no committed `tests/` directory; add one when introducing automated tests.

## Build, Test, and Development Commands

- `make docker_build` builds the Docker image with the current `VERSION` from `Makefile`.
- `docker compose up -d` starts the exporter and Redis using the example compose file.
- `python3 -m py_compile exporter.py` performs a quick syntax check.
- `flake8 exporter.py` runs linting with the repository’s `.flake8` ignores.
- `make ci` builds and pushes versioned and `latest` Docker images; use it only with valid Docker Hub credentials.

For direct local runs, install the dependencies listed in `Dockerfile`, then run `./exporter.py --envfile path/to/.env` or configure environment variables directly.

## Coding Style & Naming Conventions

Use Python 3 style with 4-space indentation. Keep configuration names aligned with the documented `AWG_EXPORTER_*` environment variable pattern. Class names use `PascalCase`, functions and attributes use `snake_case`, and metric names retain the `awg_*` prefix. Preserve the single-file structure unless extraction clearly improves maintainability.

Lint with `flake8`; this repo ignores `E241`, `E402`, `E501`, and `W601`, so avoid introducing broader style churn.

## Testing Guidelines

When adding tests, prefer `pytest` and place them under `tests/` with names like `test_exporter.py`. Focus on deterministic units such as `AwgShowWrapper.parse`, configuration defaults, label handling, and Redis persistence using mocks or a disposable Redis container. Run `python3 -m py_compile exporter.py` and `flake8 exporter.py` before opening a PR.

## Commit & Pull Request Guidelines

Recent history uses short, imperative or release-oriented messages, for example `issue #29: replace user key with user ip`, `v3.2.0`, and `version 3.2.1`. Keep commits focused and reference issues when applicable.

Pull requests should describe the behavioral change, list validation performed, mention configuration or metric changes, and update `README.md` when user-facing environment variables, Docker behavior, or metrics change.

## Security & Configuration Tips

Do not commit secrets, Docker Hub tokens, Redis credentials, or production `.env` files. Treat `AWG_EXPORTER_AWG_SHOW_EXEC` as privileged configuration because it controls the command executed by the exporter.
