# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Optional startup seeding — enqueue index jobs from a sources.yml.

If the seed file exists, each entry under `sources:` is enqueued (the background
indexer does the crawling). Sources already in the catalog are skipped, so a
restart never re-crawls — use index(force=true) to refresh. Optional and
crash-safe: a missing/malformed file logs a warning and never raises, so it can't
break server startup.
"""
import logging
import os

import yaml

from . import db

log = logging.getLogger(__name__)


def seed_from_file(path: str):
    if not path or not os.path.isfile(path):
        return
    try:
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        entries = data.get("sources") or []
    except Exception as e:  # noqa: BLE001 - never let a bad seed file crash startup
        log.warning("seed file %s could not be parsed: %s", path, e)
        return

    seeded = skipped = 0
    for entry in entries:
        try:
            source = entry["source"]
            url = entry["url"]
        except (KeyError, TypeError):
            log.warning("seed entry missing source/url, skipped: %r", entry)
            continue
        version = entry.get("version")
        if db.get_source(source, version):  # already indexed → don't re-crawl on restart
            skipped += 1
            continue
        db.enqueue_job(url, source, version)
        seeded += 1
    log.info("seeded %d source(s), %d already present, from %s", seeded, skipped, path)
