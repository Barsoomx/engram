import os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from engram_cli.main import main

raise SystemExit(main())
