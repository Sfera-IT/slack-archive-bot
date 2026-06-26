# Repository Instructions

- This project uses `uv` for Python dependency management, locking, virtualenvs, and command execution.
- Do not use `pip install -r requirements.txt`, manually manage a `.venv`, or add a new requirements file.
- Install dependencies with `uv sync`.
- Run tests with `uv run pytest`.
- Run the development app with `uv run python archivebot.py`.
- Run the WSGI app with `uv run gunicorn flask_app:flask_app -c gunicorn_conf.py`.
- When changing dependencies, edit `pyproject.toml` and refresh `uv.lock` with `uv lock`.
