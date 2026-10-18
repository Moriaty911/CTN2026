#!/usr/bin/env python3
"""Validate the offline curated corpus and render its review document.

Only the JSON Schema keywords used by corpus.schema.json are implemented here;
unsupported schema keywords fail closed. No extra dependency, runtime import,
network call, database write or assertion of independent factual truth is made.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import date
import hashlib
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
COLLECTIONS = ('facts', 'argument_cards', 'style_examples', 'unknowns')
SCHEMA_KEYS = {
    '$schema', '$id', '$defs', '$ref', 'type', 'const', 'enum', 'properties',
    'required', 'additionalProperties', 'items', 'minItems', 'uniqueItems',
    'minLength', 'maxLength', 'pattern', 'minimum',
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def check_schema(value, spec, document, path='$'):
    """Check this corpus schema's explicit keyword subset, including extra keys."""
    require(not set(spec) - SCHEMA_KEYS, f'{path}: unsupported schema keyword')
    if '$ref' in spec:
        reference = spec['$ref']
        require(reference.startswith('#/$defs/'), f'{path}: unsupported reference')
        return check_schema(value, document['$defs'][reference.removeprefix('#/$defs/')], document, path)
    if 'const' in spec:
        require(value == spec['const'], f'{path}: wrong constant')
    if 'enum' in spec:
        require(value in spec['enum'], f'{path}: invalid choice')
    if 'type' in spec:
        expected = {'object': dict, 'array': list, 'string': str, 'integer': int}[spec['type']]
        require(type(value) is expected, f'{path}: expected {spec["type"]}')
    if isinstance(value, dict):
        properties = spec.get('properties', {})
        require(set(spec.get('required', ())) <= set(value), f'{path}: missing required fields')
        if spec.get('additionalProperties') is False:
            require(not set(value) - set(properties), f'{path}: unexpected fields')
        for key in set(value) & set(properties):
            check_schema(value[key], properties[key], document, f'{path}.{key}')
    elif isinstance(value, list):
        require(len(value) >= spec.get('minItems', 0), f'{path}: too few items')
        if spec.get('uniqueItems'):
            serialised = [json.dumps(v, sort_keys=True, ensure_ascii=False) for v in value]
            require(len(serialised) == len(set(serialised)), f'{path}: duplicate items')
        for i, item in enumerate(value):
            check_schema(item, spec.get('items', {}), document, f'{path}[{i}]')
    elif isinstance(value, str):
        require(len(value) >= spec.get('minLength', 0), f'{path}: empty/short string')
        require(len(value) <= spec.get('maxLength', len(value)), f'{path}: string too long')
        if 'pattern' in spec:
            require(re.search(spec['pattern'], value) is not None, f'{path}: pattern mismatch')
    elif type(value) is int:
        require(value >= spec.get('minimum', value), f'{path}: below minimum')


def validate(corpus, schema, cases, root=ROOT):
    check_schema(corpus, schema, schema)
    curated = date.fromisoformat(corpus['curated_on'])
    sources = {s['id']: s for s in corpus['sources']}
    require(len(sources) == len(corpus['sources']), 'duplicate source ID')
    records = {r['id']: r for key in COLLECTIONS for r in corpus[key]}
    require(len(records) == sum(len(corpus[key]) for key in COLLECTIONS), 'duplicate material ID')
    for key in COLLECTIONS:
        for record in corpus[key]:
            for field, prefix in (('fact_ids', 'F'), ('unknown_ids', 'U')):
                for target in record.get(field, []):
                    require(target in records and target.startswith(prefix), f'{record["id"]}: invalid {field}')
            for field in ('text', 'claim', 'reasoning', 'question', 'safe_response'):
                text = record.get(field, '')
                require(not re.search(r'[\u3400-\u9fff]', text), f'{record["id"]}: non-English {field}')
    for fact in corpus['facts']:
        source = sources.get(fact['source_id'], {})
        require(source.get('kind') == 'official_setting', f'{fact["id"]}: raw/editorial source cannot establish a fact')
        require(source.get('url', '').startswith('https://capturethenarrative.com/'), f'{fact["id"]}: missing official URL')
        require(date.fromisoformat(source['retrieved_on']) <= curated, f'{fact["id"]}: future source date')
        for qualifier in fact['required_qualifiers']:
            require(qualifier.casefold() in fact['text'].casefold(), f'{fact["id"]}: lost qualifier {qualifier!r}')
        if fact['claim_mode'] in ('reported_position', 'evidence_limit'):
            require(fact['required_qualifiers'], f'{fact["id"]}: attribution/limit must be preserved')
    purposes = {'P01', 'P02', 'P03', 'P07', 'P08'}
    for argument in corpus['argument_cards']:
        require(set(argument['purposes']) <= purposes, f'{argument["id"]}: unsupported purpose')
        require(argument['origin'] == 'new' or argument['raw_passage_ids'], f'{argument["id"]}: missing raw lineage')
    normalised = set()
    for style in corpus['style_examples']:
        require(style['argument_id'] in records and style['argument_id'].startswith('A'), f'{style["id"]}: missing argument')
        require(set(style['purposes']) <= purposes, f'{style["id"]}: unsupported purpose')
        key = re.sub(r'\W+', ' ', style['text'].casefold()).strip()
        require(key not in normalised, f'{style["id"]}: duplicate example')
        normalised.add(key)
    for item in corpus['unknowns']:
        require(item['source_id'] in sources, f'{item["id"]}: unknown source')

    raw_sources = [s for s in corpus['sources'] if s['kind'] == 'user_raw']
    require(len(raw_sources) == 1, 'expected one raw source for this corpus version')
    source = raw_sources[0]
    raw_path = (root / source['path']).resolve()
    require(raw_path.is_relative_to(root.resolve()), 'raw source path escapes project')
    raw_bytes = raw_path.read_bytes()
    require(hashlib.sha256(raw_bytes).hexdigest() == source['sha256'], 'raw source changed; review it before updating provenance')
    raw_text = raw_bytes.decode()
    paragraphs = list(re.finditer(r'\S[^\n]*(?:\n(?!\s*\n)[^\n]+)*', raw_text))
    review = corpus['raw_review']
    require(len(review) == len(paragraphs) == source['paragraphs'], 'raw review is incomplete')
    raw_by_id = {r['id']: r for r in review}
    require(len(raw_by_id) == len(review), 'duplicate raw-review ID')
    for i, (row, match) in enumerate(zip(review, paragraphs), 1):
        text = match.group().strip()
        require(row['id'] == f'R{i:03}' and row['paragraph'] == i, 'raw paragraph numbering mismatch')
        require(row['source_id'] == source['id'], f'{row["id"]}: wrong raw source')
        require(row['line'] == raw_text.count('\n', 0, match.start()) + 1, f'{row["id"]}: wrong source line')
        require(row['original_characters'] == len(text), f'{row["id"]}: wrong original length')
        require(row['text_sha256'] == hashlib.sha256(text.encode()).hexdigest(), f'{row["id"]}: wrong excerpt hash')
        require(set(row['output_ids']) <= records.keys(), f'{row["id"]}: broken output link')
        require(not any(target.startswith('F') for target in row['output_ids']), f'{row["id"]}: raw text cannot be promoted to fact provenance')
        if row['disposition'] == 'retained_as_argument':
            require(row.get('retained_excerpt') == text, f'{row["id"]}: altered retained excerpt')
        else:
            require('retained_excerpt' not in row, f'{row["id"]}: unsafe excerpt outside retention decision')
        if row['disposition'] == 'excluded':
            require(all(target.startswith('U') for target in row['output_ids']), f'{row["id"]}: excluded argument reused')
        for target in row['output_ids']:
            record = records[target]
            if 'raw_passage_ids' in record:
                require(row['id'] in record['raw_passage_ids'], f'{target}: missing reverse raw link')
    for key in ('argument_cards', 'style_examples'):
        for record in corpus[key]:
            for rid in record['raw_passage_ids']:
                require(rid in raw_by_id and record['id'] in raw_by_id[rid]['output_ids'], f'{record["id"]}: broken raw lineage')

    require(cases['schema_version'] == 'ctn-rag-review-cases.v1', 'wrong review-case version')
    require(cases['corpus_version'] == corpus['corpus_version'], 'review cases refer to another corpus')
    require(cases['status'] == 'acceptance_questions_only_no_retriever_implemented', 'review cases must not claim retrieval validation')
    case_ids = set()
    coverage = set()
    for case in cases['cases']:
        require(case['id'] not in case_ids, 'duplicate review-case ID')
        require(case['query'].strip() and case['constraint'].strip(), 'empty review case')
        case_ids.add(case['id'])
        for field, prefix in (('expected_fact_ids', 'F'), ('expected_argument_ids', 'A'), ('expected_unknown_ids', 'U')):
            for target in case[field]:
                require(target in records and target.startswith(prefix), f'{case["id"]}: invalid expected record')
                coverage.add(target)
    require({r['id'] for r in corpus['unknowns']} <= coverage, 'an unknown category has no acceptance question')
    return {
        'corpus_version': corpus['corpus_version'],
        'counts': {key: len(corpus[key]) for key in COLLECTIONS},
        'raw_paragraphs_reviewed': len(review),
        'raw_dispositions': dict(Counter(r['disposition'] for r in review)),
        'acceptance_questions': len(cases['cases']),
        'max_example_characters': max(len(s['text']) for s in corpus['style_examples']),
        'retrieval_tested': False,
        'runtime_connected': False,
    }


def render(corpus, cases):
    lines = ['# YES RAG v1 审阅预览', '',
        '> 本文由 `config/rag/yes.v1.json` 生成；正式来源是 JSON。语料尚未接入运行链路。', '',
        f'版本：`{corpus["corpus_version"]}`；整理日期：{corpus["curated_on"]}。', '',
        '以下内容仅属于虚构比赛设定。来源中的阵营预测保留归属，事实保留证据限定；示例与提案不能充当事实。', '',
        '## 官方事实', '',
        '来源：[官方设定](https://capturethenarrative.com/info/competitions/2026/setting/)。以下为来源快照，不是实时疫情或独立临床核验。', '']
    for f in corpus['facts']:
        lines += [f'### {f["id"]} · {f["title_zh"]}', '', f['text'], '',
            f'类型：`{f["claim_mode"]}`；原章节：{f["source_section"]}。',
            '必须保留的限定：' + ('；'.join(f['required_qualifiers']) or '来源日期与虚构设定范围') + '。', '']
    lines += ['## YES 论点卡', '', '这些是政策判断、推理或实施提议；依赖事实见 ID，不代表效果已证实。', '']
    for a in corpus['argument_cards']:
        lines += [f'### {a["id"]} · {a["title_zh"]}', '', a['claim'], '', a['reasoning'], '',
            '**应正面回应的质疑：** ' + a['fair_objection'], '',
            '**边界：** ' + a['response_boundary'], '',
            f'依据：{", ".join(a["fact_ids"])}；未知：{", ".join(a["unknown_ids"])}。',
            f'用途：{", ".join(a["purposes"])}；原文线索：{", ".join(a["raw_passage_ids"]) or "新增补充"}。', '']
    lines += ['## 表达示例', '', '只参考表达；实际回复须适合人物、目的、材料和当前问题，不直接复制发布。', '']
    for s in corpus['style_examples']:
        lines += [f'### {s["id"]} · {s["title_zh"]}', '', s['text'], '',
            f'关联论点：{s["argument_id"]}；用途：{", ".join(s["purposes"])}；长度：{len(s["text"])}。', '']
    lines += ['## 未知事项', '', '未在材料中说明，不等于现实中不存在。以下处理方式供生成时参考，不是新公布的政策。', '']
    for u in corpus['unknowns']:
        lines += [f'### {u["id"]} · {u["title_zh"]}', '', u['question'], '',
            '**不可推断：** ' + u['do_not_infer'], '', '**可用答法：** ' + u['safe_response'], '',
            f'状态：`{u["knowledge_status"]}`；相关事实：{", ".join(u["fact_ids"]) or "不属于共享事实"}。', '']
    labels = {'retained_as_argument':'保留为论点','adapted_and_merged':'改写并合并','theme_only':'仅保留主题','excluded':'排除'}
    lines += ['## 原始 50 段处理记录', '',
        '原文摘录与排除理由仅用于审阅，不应进入检索正文。', '',
        '| 原段落 | 处理 | 输出关联 | 理由 |', '|---|---|---|---|']
    for r in corpus['raw_review']:
        lines.append(f'| [{r["id"]}](../../vote_for_raw_rag.md#L{r["line"]}) | {labels[r["disposition"]]} | {", ".join(r["output_ids"])} | {r["reason_zh"]} |')
    lines += ['', '## 后续检索验收题', '',
        '这里只列出预期材料与回答边界，尚未执行检索或模型效果评估。', '']
    for c in cases['cases']:
        expected = c['expected_fact_ids'] + c['expected_argument_ids'] + c['expected_unknown_ids']
        lines += [f'### {c["id"]}', '', c['query'], '',
            f'相关材料：{", ".join(expected)}。', '', c['constraint'], '']
    return '\n'.join(lines).rstrip() + '\n'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--write-preview', action='store_true', help='Regenerate docs/rag-yes-v1/preview.md after successful checks')
    args = parser.parse_args()
    directory = ROOT / 'config/rag'
    corpus = json.loads((directory / 'yes.v1.json').read_text())
    schema = json.loads((directory / 'corpus.schema.json').read_text())
    cases = json.loads((directory / 'review-cases.v1.json').read_text())
    result = validate(corpus, schema, cases)
    preview = ROOT / 'docs/rag-yes-v1/preview.md'
    expected = render(corpus, cases)
    if args.write_preview:
        preview.parent.mkdir(parents=True, exist_ok=True)
        preview.write_text(expected)
    else:
        require(preview.exists() and preview.read_text() == expected, 'preview is stale; run with --write-preview')
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
