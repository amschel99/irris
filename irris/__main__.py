"""python -m irris  ->  serves on $PORT (default 8000)."""
import os

import uvicorn

if __name__ == "__main__":
    uvicorn.run("irris.app:app", host=os.environ.get("HOST", "0.0.0.0"), port=int(os.environ.get("PORT", "8000")),
                proxy_headers=True, forwarded_allow_ips="*")
