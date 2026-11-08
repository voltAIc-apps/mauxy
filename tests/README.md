# mauxy tests

Unit tests for `/api/subscribe` and `/api/confirm`. Mautic is mocked via `respx`.

## Run

```bash
cd mauxy
python -m venv tests/venv
source tests/venv/bin/activate
pip install -r requirements.txt -r tests/requirements.txt
PYTHONPATH=. pytest tests/ -v
```

Env vars are set by `tests/conftest.py` before `main` is imported, so you do
not need a real `.env`. Tests use an in-memory SQLite action log and point
Mautic at `https://mautic.test` which `respx` intercepts.
