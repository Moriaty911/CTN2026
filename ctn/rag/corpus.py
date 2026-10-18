"""Versioned, source-bound static retrieval. Source archives never enter prompts."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CORPUS = ROOT / 'config/rag/yes.v1.1.json'
RETRIEVAL_VERSION = 'rag-lexical.v1'
STOP = set('the a an is are was were to of in on and or for with does do it this that my we our what which how can will would be as have has'.split())
SYNONYMS = {
 'free': 'cost payment affordability', 'cost': 'free payment affordability',
 'months': 'duration doses booster interval', 'spread': 'transmission infectious',
 'exemption': 'exemptions medical alternatives', 'exemptions': 'exemption medical alternatives',
 'aged': 'age children eligibility', '12': 'age threshold children',
 'app': 'digital privacy proof data', 'retain': 'retention data privacy',
 'stores': 'retention data privacy', 'fine': 'enforcement penalties responsibility',
 'shopkeeper': 'retail enforcement responsibility',
 'hospitals': 'hospital healthcare care capacity', 'outbreak': 'transmission preparedness',
 'tourism': 'tourist tourists visitors economy', 'islands': 'island rural geography access',
 'expires': 'sunset review duration', 'mrna': 'platform vaccine',
 'emergency': 'emergencies governance powers treatment', 'deaths': 'covid mortality severe',
 'paper': 'newspaper media', 'newspaper': 'media leaning', 'president': 'government parliament referendum',
 'poll': 'polling majority support', 'trending': 'polling majority hashtag',
}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def tokens(text):
    return {w for w in re.findall(r'[a-z0-9]+', text.lower()) if w not in STOP}


@dataclass(frozen=True)
class RagSnapshot:
    """Detached immutable JSON; the identifier binds every selected byte."""
    body_json: str

    @property
    def snapshot_id(self):
        return digest(self.to_dict())

    def to_dict(self):
        return json.loads(self.body_json)

    def campaign_items(self):
        data = self.to_dict()
        items = []
        for row in data['facts']:
            kind = 'attributed_claim' if row['claim_mode'] == 'reported_position' else 'official_fact'
            items.append(dict(reference_id=row['id'], kind=kind, text=row['text'], source_id=row['source_id']))
        for row in data['arguments']:
            text = '\n'.join(row[k] for k in ('claim','reasoning','fair_objection','response_boundary'))
            items.append(dict(reference_id=row['id'], kind='argument', text=text, source_id='editorial'))
        for row in data['unknowns']:
            text = '\n'.join(row[k] for k in ('question','safe_response','do_not_infer'))
            items.append(dict(reference_id=row['id'], kind='unknown', text=text, source_id=row['source_id']))
        return items


class Corpus:
    def __init__(self, path=None):
        self.path = Path(path or DEFAULT_CORPUS)
        self.data = json.loads(self.path.read_text())
        # Reuse the offline validator, including provenance and qualifier checks.
        from scripts.check_rag_corpus import validate
        schema = json.loads((ROOT / 'config/rag/corpus.schema.json').read_text())
        cases_path=self.path.with_name(self.path.name.replace('yes.','review-cases.'))
        cases=json.loads(cases_path.read_text())
        validate(self.data, schema, cases, root=ROOT)
        self.hash = digest(self.data)
        self.facts = {r['id']: r for r in self.data['facts']}
        self.arguments = {r['id']: r for r in self.data['argument_cards']}
        self.unknowns = {r['id']: r for r in self.data['unknowns']}

    def install_sources(self, store):
        for source in self.data['sources']:
            if source.get('path'):
                path=(ROOT/source['path']).resolve()
                if not path.is_relative_to(ROOT):raise ValueError('source path must remain inside the project')
                body=path.read_bytes().decode('utf-8')
                if hashlib.sha256(path.read_bytes()).hexdigest()!=source['sha256']:
                    raise ValueError('original source hash changed')
                store.archive_source(source['id'],source['sha256'],body,source)

    def retrieve(self, text, *, purpose=None, target_id='', target_version=1, style=True):
        query = tokens(text)
        expanded = query | tokens(' '.join(SYNONYMS.get(t, '') for t in query))
        def score(row):
            # Topics and concise claims outrank long editorial explanations.
            topics = tokens(' '.join(row.get('topics', [])))
            body = tokens(' '.join(str(row.get(k, '')) for k in ('text','claim','question','safe_response')))
            return 4*len(query & topics) + 2*len(query & body) + len(expanded & (topics | body))
        def ranked(rows, limit):
            eligible = [r for r in rows if not purpose or 'purposes' not in r or purpose in r['purposes']]
            return [r for r in sorted(eligible, key=lambda r: (-score(r), r['id'])) if score(r)>0][:limit]
        from .relevance import concept_ids
        concept_facts,concept_unknowns=concept_ids(text)
        facts = ranked(self.facts.values(), 6)
        arguments = ranked(self.arguments.values(), 2)
        unknowns = ranked(self.unknowns.values(), 3)
        # Retain full dependencies; do not cut a qualification to hit a top-k limit.
        fs = {r['id']: r for r in facts}; us = {r['id']: r for r in unknowns}
        fs.update((i,self.facts[i]) for i in concept_facts)
        us.update((i,self.unknowns[i]) for i in concept_unknowns)
        if any(t in query for t in ('yes','no','referendum')):fs.update((i,self.facts[i]) for i in ('F001','F002'))
        for a in arguments:
            fs.update((i, self.facts[i]) for i in a['fact_ids'])
            us.update((i, self.unknowns[i]) for i in a['unknown_ids'])
        styles = ranked(self.data['style_examples'], 1) if style else []
        return RagSnapshot(json.dumps(dict(schema_version='ctn-rag-snapshot.v1',
            corpus_version=self.data['corpus_version'], corpus_hash=self.hash,
            retrieval_version=RETRIEVAL_VERSION, target_id=target_id, target_version=target_version,
            query_hash=digest(text), purpose=purpose, facts=list(fs.values()), arguments=arguments,
            unknowns=list(us.values()), styles=styles,
            constraints=['Preserve attribution, dates, uncertainty and all required qualifiers.',
                'Arguments are judgments; unknown is not evidence that a provision does not exist.',
                'Style reference only: do not copy. Persona voice takes priority.']), ensure_ascii=False))


def too_close(text, references, threshold=.82):
    """Local wording check, never a ban on reusing a fact."""
    from ctn.moderation.detector import word_shingles, shingle_jaccard
    normalized = lambda s: ' '.join(re.findall(r'[a-z0-9]+', s.lower()))
    return any(normalized(text)==normalized(r) or
        shingle_jaccard(word_shingles(text),word_shingles(r))>=threshold for r in references if r)
