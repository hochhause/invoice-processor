"""
test_ollama.py — CLI utility to verify Ollama connectivity & available models.

Usage:
  python test_ollama.py                    # Test default LLM_URL
  python test_ollama.py --url <url>       # Test custom URL
  python test_ollama.py --list-ips         # Show host IPs visible to containers
"""
import os
import sys
import json
import urllib.request
import urllib.error
import socket
from pathlib import Path

def get_host_ips():
    """Get all non-localhost IPv4 addresses."""
    ips = {}
    try:
        import socket
        for name, aliases, addresses in socket.gethostbyname_ex(socket.gethostname()):
            for addr in addresses:
                if not addr.startswith("127."):
                    ips[addr] = "detected"
    except:
        pass
    # Add common Docker/Podman bridge IPs
    ips["host.docker.internal"] = "Docker Desktop (Windows/Mac)"
    ips["172.17.0.1"] = "Docker bridge (Linux)"
    ips["172.29.208.1"] = "WSL vEthernet (Windows)"
    return ips

def test_ollama(url: str, timeout: int = 5) -> dict:
    """Test Ollama connectivity."""
    endpoint = f"{url.rstrip('/')}/api/tags"
    result = {
        "url": url,
        "reachable": False,
        "models": [],
        "error": None,
    }

    try:
        req = urllib.request.Request(endpoint)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode())
            result["reachable"] = True
            result["models"] = [m["name"] for m in data.get("models", [])]
    except urllib.error.URLError as e:
        result["error"] = f"Connection error: {e.reason}"
    except socket.timeout:
        result["error"] = "Timeout (service not responding)"
    except json.JSONDecodeError:
        result["error"] = "Invalid response (not Ollama?)"
    except Exception as e:
        result["error"] = str(e)

    return result

def main():
    if "--list-ips" in sys.argv:
        print("Host IPs visible to containers:")
        for ip, desc in get_host_ips().items():
            print(f"  {ip:<30} ({desc})")
        return 0

    url = None
    if "--url" in sys.argv:
        idx = sys.argv.index("--url")
        if idx + 1 < len(sys.argv):
            url = sys.argv[idx + 1]

    if not url:
        url = os.environ.get("LLM_URL", "http://host.docker.internal:11434")

    print(f"Testing Ollama at: {url}")
    result = test_ollama(url)

    if result["reachable"]:
        print(f"[OK] REACHABLE")
        print(f"  Models: {', '.join(result['models']) if result['models'] else 'none'}")
        return 0
    else:
        print(f"[FAIL] UNREACHABLE")
        print(f"  Error: {result['error']}")
        print()
        print("Troubleshooting:")
        print(f"  1. Is Ollama running? (Check process)")
        print(f"  2. Try alternative URLs:")
        for ip, desc in get_host_ips().items():
            alt_url = f"http://{ip}:11434"
            print(f"     {alt_url} ({desc})")
            alt_result = test_ollama(alt_url, timeout=2)
            if alt_result["reachable"]:
                print(f"       [OK] THIS WORKS! Set: LLM_URL={alt_url}")
                return 0
        return 1

if __name__ == "__main__":
    sys.exit(main())
