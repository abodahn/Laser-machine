"""Start the T&C Laser Intelligence & Command Center."""
import argparse

import uvicorn

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8800)
    p.add_argument("--reload", action="store_true")
    a = p.parse_args()
    uvicorn.run("app.main:app", host=a.host, port=a.port, reload=a.reload, log_level="info")
