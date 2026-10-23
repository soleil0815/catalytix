"""WSGI entry point: gunicorn -w 2 -b 0.0.0.0:9300 wsgi:app"""
import os
import sys

from catalytix import create_app

try:
    app = create_app()
except SystemExit as exc:
    # Catalytix refuses to start (e.g. the key doesn't match the database). Exit code 4 tells gunicorn
    # that restarting the workers is pointless – otherwise they would be restarted forever.
    if exc.code not in (None, 0):
        print(exc.code if isinstance(exc.code, str) else f"Catalytix: start aborted ({exc.code})",
              file=sys.stderr)
        sys.exit(4)
    raise

if __name__ == "__main__":
    # For development/testing only. Production: gunicorn (Linux) or waitress (Windows)
    app.run(host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "9300")),
            debug=os.environ.get("FLASK_DEBUG") == "1")
