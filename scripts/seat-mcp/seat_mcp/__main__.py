"""python3 -m seat_mcp"""

from .auth import load_token
from .jobs import ensure_jobs_dir
from .server import serve


def main() -> None:
    load_token()  # fail fast, naming the Infisical key, when the token is missing
    ensure_jobs_dir()
    serve()


if __name__ == "__main__":
    main()
