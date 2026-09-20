import subprocess
import re
import json
import os
import time
import sys
import urllib.request

LOG_FILE = "cloudflare.log"
VERCEL_JSON = "vercel.json"

def is_backend_alive():
    try:
        req = urllib.request.urlopen("http://127.0.0.1:8000/api/preflight", timeout=2)
        return req.status == 200
    except Exception:
        return False

def ensure_backend_running():
    if not is_backend_alive():
        print("[HYPRINT Auto-Manager] FastAPI Backend is NOT running on port 8000. Launching Uvicorn...")
        venv_python = os.path.join(".venv", "Scripts", "python.exe")
        python_exe = venv_python if os.path.exists(venv_python) else sys.executable
        
        # Launch uvicorn as separate background process
        subprocess.Popen(
            [python_exe, "-m", "uvicorn", "main:app", "--port", "8000", "--host", "0.0.0.0"],
            creationflags=subprocess.CREATE_NEW_CONSOLE if os.name == 'nt' else 0
        )
        
        # Wait up to 10 seconds for backend to start
        for _ in range(10):
            time.sleep(1)
            if is_backend_alive():
                print("[HYPRINT Auto-Manager] FastAPI Backend started successfully on http://127.0.0.1:8000")
                return True
        print("[HYPRINT Auto-Manager] Warning: Backend took long to start, proceeding anyway...")
    else:
        print("[HYPRINT Auto-Manager] FastAPI Backend is already running on http://127.0.0.1:8000")

def run_tunnel_and_deploy():
    ensure_backend_running()

    print("[HYPRINT Auto-Manager] Starting Cloudflare Tunnel...")
    if os.path.exists(LOG_FILE):
        try:
            os.remove(LOG_FILE)
        except Exception:
            pass

    cloudflared_bin = os.path.join("bin", "cloudflared.exe")
    if not os.path.exists(cloudflared_bin):
        cloudflared_bin = "cloudflared"

    process = subprocess.Popen(
        [cloudflared_bin, "tunnel", "--url", "http://127.0.0.1:8000", "--logfile", LOG_FILE],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )

    tunnel_url = None
    url_pattern = re.compile(r'https://[a-zA-Z0-9-]+\.trycloudflare\.com')

    print("[HYPRINT Auto-Manager] Waiting for Cloudflare Tunnel URL...")
    for _ in range(25):
        time.sleep(1)
        if os.path.exists(LOG_FILE):
            try:
                with open(LOG_FILE, 'r', encoding='utf-8', errors='ignore') as f:
                    content = f.read()
                match = url_pattern.search(content)
                if match:
                    tunnel_url = match.group(0)
                    break
            except Exception as e:
                print(f"[HYPRINT Auto-Manager] Error reading log: {e}")

    if not tunnel_url:
        print("[HYPRINT Auto-Manager] Failed to get Cloudflare Tunnel URL. Retrying...")
        process.terminate()
        return

    print(f"\n=======================================================")
    print(f"[HYPRINT Auto-Manager] ACTIVE CLOUDFLARE TUNNEL URL:")
    print(f"  {tunnel_url}")
    print(f"=======================================================\n")

    # Update vercel.json
    try:
        with open(VERCEL_JSON, 'r') as f:
            v_config = json.load(f)

        current_dest = None
        for rewrite in v_config.get('rewrites', []):
            if rewrite.get('source') == '/api/:path*':
                current_dest = rewrite.get('destination')
                rewrite['destination'] = f"{tunnel_url}/api/:path*"

        new_dest = f"{tunnel_url}/api/:path*"
        if current_dest != new_dest:
            with open(VERCEL_JSON, 'w') as f:
                json.dump(v_config, f, indent=2)
            print("[HYPRINT Auto-Manager] vercel.json updated. Committing and pushing to GitHub...")

            subprocess.run(['git', 'add', VERCEL_JSON], check=True)
            subprocess.run(['git', 'commit', '-m', f"Auto Cloudflare Tunnel: {tunnel_url}"], check=True)
            subprocess.run(['git', 'push', 'origin', 'HEAD'], check=True)
            print("\n>>> SUCCESS: GitHub push completed! Vercel is now deploying the new tunnel URL. <<<\n")
        else:
            print("[HYPRINT Auto-Manager] vercel.json already points to current tunnel URL.")

    except Exception as e:
        print(f"[HYPRINT Auto-Manager] Error updating vercel.json / git push: {e}")

    # Keep process running & monitor
    try:
        while process.poll() is None:
            time.sleep(5)
            # Ensure backend stays alive
            if not is_backend_alive():
                print("[HYPRINT Auto-Manager] Backend fell offline! Restarting backend...")
                ensure_backend_running()
    except KeyboardInterrupt:
        process.terminate()
        sys.exit(0)

if __name__ == "__main__":
    while True:
        try:
            run_tunnel_and_deploy()
        except KeyboardInterrupt:
            print("Exiting...")
            break
        except Exception as e:
            print(f"[HYPRINT Auto-Manager] Loop error: {e}")
        time.sleep(5)
