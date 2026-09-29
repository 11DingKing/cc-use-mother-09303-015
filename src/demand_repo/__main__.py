"""模块入口：python -m demand_repo [db_path] [host] [port]"""
from __future__ import annotations

import sys

from .http_api import serve

if __name__ == "__main__":
    db_path = sys.argv[1] if len(sys.argv) > 1 else ":memory:"
    host = sys.argv[2] if len(sys.argv) > 2 else "127.0.0.1"
    port = int(sys.argv[3]) if len(sys.argv) > 3 else 8080
    serve(db_path, host, port)
