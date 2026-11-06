"""Entry point for hosting Harvey's dashboard on cPanel (Passenger).

cPanel settings (Setup Python App):
    Application root         public_html/harvey-sales
    Application URL          harvey-sales
    Application startup file passenger_wsgi.py
    Application entry point  application

Environment variables (Setup Python App -> Environment variables, or .env):
    HARVEY_DASHBOARD_PUBLIC_HOSTS  required, e.g. theinsightpartnersinc.com,www.theinsightpartnersinc.com
    HARVEY_BASE_PATH               the Application URL as a path; default /harvey-sales

The venv is built by scripts/cpanel_deploy.sh (run from .cpanel.yml).
Passenger speaks WSGI and FastAPI is ASGI, so the app is wrapped with a2wsgi.
"""

import os
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
VENV = "/home/theinsightinc/venvs/harvey-sales"
VENV_PYTHON = os.path.join(VENV, "bin", "python")

# Re-run this loader under the deploy venv's Python (it has the packages).
# Compare prefixes, not executables: the venv's python is a symlink.
if (os.path.exists(VENV_PYTHON)
        and os.path.realpath(sys.prefix) != os.path.realpath(VENV)):
    os.execl(VENV_PYTHON, VENV_PYTHON, *sys.argv)

sys.path.insert(0, BASE_DIR)
os.chdir(BASE_DIR)

# Values set in cPanel win over .env.
from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(BASE_DIR, ".env"), override=False)

BASE_PATH = "/" + os.environ.setdefault("HARVEY_BASE_PATH", "/harvey-sales").strip().strip("/")
os.environ["HARVEY_BASE_PATH"] = BASE_PATH

if not os.environ.get("HARVEY_DASHBOARD_PUBLIC_HOSTS", "").strip():
    raise RuntimeError(
        "Set HARVEY_DASHBOARD_PUBLIC_HOSTS to your domain (e.g. theinsightpartnersinc.com) "
        "in Setup Python App -> Environment variables, then restart the app.")

from a2wsgi import ASGIMiddleware  # noqa: E402

from harvey.dashboard import app  # noqa: E402  (the FastAPI app is harvey/dashboard.py)

_asgi = ASGIMiddleware(app)


def application(environ, start_response):
    """Strip the /harvey-sales prefix so the dashboard sees "/", "/api/...",
    "/static/..." exactly as it does locally."""
    full = environ.get("SCRIPT_NAME", "") + environ.get("PATH_INFO", "")
    if full == BASE_PATH:
        query = environ.get("QUERY_STRING", "")
        start_response("301 Moved Permanently",
                       [("Location", BASE_PATH + "/" + ("?" + query if query else "")),
                        ("Content-Length", "0")])
        return [b""]
    if not full.startswith(BASE_PATH + "/"):
        start_response("404 Not Found", [("Content-Type", "text/plain"), ("Content-Length", "9")])
        return [b"not found"]
    environ["SCRIPT_NAME"] = ""
    environ["PATH_INFO"] = full[len(BASE_PATH):]
    return _asgi(environ, start_response)
