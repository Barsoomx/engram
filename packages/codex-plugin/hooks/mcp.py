import os, sys

os.environ["ENGRAM_MCP_CODEX_SCOPE"] = "1"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from engram_cli.main import main

raise SystemExit(main(["mcp", "serve"]))
