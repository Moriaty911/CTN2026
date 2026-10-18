"""Pi and Python share the same versioned corpus and durable snapshot IDs."""
import argparse
import json
from .corpus import Corpus
from ctn.config import Settings
from ctn.storage import StateStore


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('command',choices=['query'])
    p.add_argument('--text',required=True);p.add_argument('--purpose',default='P03')
    p.add_argument('--topic',default='');p.add_argument('--state-file');p.add_argument('--corpus')
    p.add_argument('--target-id',default='');p.add_argument('--target-version',type=int,default=1)
    p.add_argument('--json',action='store_true')
    args=p.parse_args(argv)
    corpus=Corpus(args.corpus)
    snapshot=corpus.retrieve(args.text+' '+args.topic,purpose=args.purpose,target_id=args.target_id,target_version=args.target_version)
    store=StateStore(Settings.load().database_path(args.state_file))
    try:
        corpus.install_sources(store)
        store.save_rag_snapshot(snapshot)
        print(json.dumps({'snapshot_id':snapshot.snapshot_id,**snapshot.to_dict()},ensure_ascii=False,indent=2))
    finally:store.close()


if __name__=='__main__':main()
