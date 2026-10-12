"""Paper trader for pons (ponsfamily.com) launches."""
import os


def _load_dotenv():
    """Local secrets (e.g. SOLANA_GRPC_TOKEN) from a .env file at the repo root, which git ignores. Only sets
    variables that aren't set already, so the real environment (Render's settings) always wins."""
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    try:
        with open(path, encoding="utf8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip().removeprefix("set ").removeprefix("export ").strip()
                os.environ.setdefault(key, value.strip().strip('"').strip("'"))
    except OSError:
        pass


_load_dotenv()
