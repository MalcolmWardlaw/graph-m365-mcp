"""Fill launchd/org.wardlaw.graph-m365.plist from the Claude Desktop config.

Called by install.fish (fish has no heredoc, so this lives in its own file):
    python3 render_plist.py <template> <desktop-config.json> <dest> <repo> <uv>

Every key in mcpServers.graph-m365.env becomes an EnvironmentVariables entry,
except KEYCHAIN_BACKEND: the daemon runs on the file backend only.
"""
import json, os, plistlib, sys

tmpl, cfg, dest, repo, uv = sys.argv[1:6]
with open(cfg) as fh:
    env = json.load(fh)["mcpServers"]["graph-m365"].get("env", {})
missing = [k for k in ("GRAPH_CLIENT_ID", "GRAPH_TENANT_ID") if not env.get(k)]
if missing:
    sys.exit(f"Desktop config env block lacks {', '.join(missing)}")
with open(tmpl, "rb") as fh:
    pl = plistlib.load(fh)
home = os.path.expanduser("~")
pl["ProgramArguments"] = [uv, "run", f"{repo}/server.py", "--daemon"]
pl["EnvironmentVariables"] = {
    "PATH": pl["EnvironmentVariables"]["PATH"],
    **{k: v for k, v in env.items() if k != "KEYCHAIN_BACKEND"},
}
pl["StandardOutPath"] = pl["StandardErrorPath"] = f"{home}/Library/Logs/graph-m365.log"
os.makedirs(os.path.dirname(dest), exist_ok=True)
with open(dest, "wb") as fh:
    plistlib.dump(pl, fh)
os.chmod(dest, 0o600)                    # carries the tenant and client ids
print(f"wrote {dest}")
