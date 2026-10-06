"""Run the packaged HTTP demo and save local evidence."""

import json
from pathlib import Path

from pagw.demo import main

result = main()
Path("artifacts").mkdir(exist_ok=True)
Path("artifacts/local-demo.json").write_text(json.dumps(result, indent=2) + "\n")
