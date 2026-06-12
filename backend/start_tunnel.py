"""Start FastAPI + ngrok tunnel. Usage: python start_tunnel.py [ngrok_token]"""
import sys
import os
import threading
import uvicorn
from pyngrok import ngrok, conf

# Authtoken: pass as argument or set NGROK_AUTHTOKEN env var
token = sys.argv[1] if len(sys.argv) > 1 else os.getenv("NGROK_AUTHTOKEN", "")
if token:
    conf.get_default().auth_token = token

def run_server():
    uvicorn.run("main:app", host="0.0.0.0", port=8000, log_level="warning")

thread = threading.Thread(target=run_server, daemon=True)
thread.start()

# Give server a moment to start
import time
time.sleep(1)

tunnel = ngrok.connect(8000, "http")
print("\n" + "=" * 60)
print(f"  Dashboard: {tunnel.public_url}")
print("=" * 60)
print("  Ctrl+C to stop\n")

try:
    ngrok.get_ngrok_process().proc.wait()
except KeyboardInterrupt:
    print("\nStopping...")
    ngrok.kill()
