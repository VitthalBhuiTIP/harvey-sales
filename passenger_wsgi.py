import os
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
VENV_PYTHON = "/home/theinsightinc/venvs/harvey-sales/bin/python"

# Make Passenger use our virtualenv's Python
if sys.executable != VENV_PYTHON and os.path.exists(VENV_PYTHON):
    os.execl(VENV_PYTHON, VENV_PYTHON, *sys.argv)

sys.path.insert(0, BASE_DIR)
os.chdir(BASE_DIR)

from a2wsgi import ASGIMiddleware
from main import app  # change if your FastAPI app is elsewhere, e.g. from app.main import app

application = ASGIMiddleware(app)