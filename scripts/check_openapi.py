import json
from pathlib import Path

from pagw.api import create_app

expected = json.loads(Path("docs/openapi.json").read_text())
assert create_app().openapi() == expected, "OpenAPI changed; run make openapi and review the diff"
print("OpenAPI matches the committed contract")
