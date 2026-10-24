"""Export API contracts without modifying the development runtime database."""

import argparse
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from virtual_life.api import create_app
from virtual_life.settings import Settings


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="docs/openapi.json")
    args = parser.parse_args()
    target = Path(args.output)
    with TemporaryDirectory(prefix="virtual-life-openapi-") as scratch:
        app = create_app(Settings(database_path=str(Path(scratch) / "schema.sqlite3")))
        document = app.openapi()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Exported {len(document['paths'])} paths to {target}")


if __name__ == "__main__":
    main()
