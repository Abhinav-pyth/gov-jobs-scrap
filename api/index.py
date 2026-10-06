"""api/index.py - Vercel Serverless Function entrypoint for the Flask app.

Vercel's `functions` build system automatically discovers Python files under
the /api directory and serves them as serverless functions. This module
exposes the Flask WSGI application so it can handle HTTP requests in a
serverless environment.

The wsgi.py module handles:
- Configuring writable database paths for serverless (/tmp)
- Importing the Flask app without starting background threads
- Exposing the app as a WSGI callable named 'app' or 'handler'
"""

import sys
from pathlib import Path

# Ensure the project root is in sys.path so we can import wsgi
project_root = Path(__file__).parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from wsgi import app

# Vercel looks for these names in order: handler, __handler__, app
handler = app
