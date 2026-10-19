"""One news step against a database, for a rehearsal on the server:

    docker compose exec fleet python -m ctn.news            # fetch + extract once, print the report
    docker compose exec fleet python -m ctn.news --no-extract
"""
import argparse
import asyncio
import json
from contextlib import closing
from dataclasses import replace

from ctn.config import Settings
from ctn.storage import StateStore
from .extract import ClaimExtractor
from .ingest import NewsIngestor


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-file')
    parser.add_argument('--no-extract', action='store_true')
    args = parser.parse_args(argv)
    settings = Settings.load()
    with closing(StateStore(settings.database_path(args.state_file), migrate=False)) as store:
        extractor = None
        if not args.no_extract and settings.llm.api_key:
            from ctn.llm import DeepSeekClient
            extractor = ClaimExtractor(DeepSeekClient(replace(settings.llm, max_tokens=4096, temperature=0.1)))
        report = asyncio.run(NewsIngestor(store, extractor=extractor, notify=False).step())
        print(json.dumps({'report': report, 'summary': store.news_summary(), 'latest': store.news_list(limit=10)}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
