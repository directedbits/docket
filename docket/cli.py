# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Manual seeding CLI — index a source synchronously, no server required.

    python -m docket.cli ingest <source> <url|repo-path>
"""
import argparse

from . import config
from . import db
from . import ingest as ingest_mod


def main():
    p = argparse.ArgumentParser(prog="docket")
    sub = p.add_subparsers(dest="cmd", required=True)
    ing = sub.add_parser("ingest", help="index a source synchronously")
    ing.add_argument("source", help="logical corpus label, e.g. 'fastapi'")
    ing.add_argument("target", help="URL or local repo path")
    ing.add_argument("--version", default=config.DEFAULT_VERSION,
                     help="version label (default: latest)")
    args = p.parse_args()

    config.setup_logging()
    db.init_db()
    if args.cmd == "ingest":
        if args.version == "*":
            print("error: version '*' is reserved (wildcard)")
            return
        chunks = ingest_mod.ingest(args.target)
        n = db.replace_source(args.source, chunks, url=args.target, version=args.version)
        if n:
            db.optimize()
        print(f"indexed {n} chunks from {args.target} into "
              f"source '{args.source}' (version={args.version})")


if __name__ == "__main__":
    main()
