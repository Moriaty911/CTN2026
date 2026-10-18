"""Domain vocabulary for retrieval, not fixed questions or prewritten answers.

Concepts add recall to lexical search; unfamiliar topics still use lexical ranking.
IDs locate existing, source-bound records and never supply new factual claims.
"""
import re

CONCEPTS = (
 (r'\b(age[ds]?|children|child|schooling|eligib|12\b)', ('F001',), ('U001','U004','U012')),
 (r'\b(free|cost|afford|payment|paying)', (), ('U002','U003')),
 (r'\b(spread|transmi|vaccinated passenger|sit next)', ('F005','F006'), ('U016','U017')),
 (r'\b(months|lasts?|protection last|duration|dose|booster|waiting|certificate valid)', ('F006','F007'), ('U007','U016')),
 (r'\b(exempt|cannot comply|medical alternative)', (), ('U004',)),
 (r'\b(nobody|today|current|outbreak|local h2d2)', ('F003','F008'), ('U020',)),
 (r'\b(anti.vacc|voluntary|personal medical|opposing access)', ('F012','F028'), ('U022',)),
 (r'\b(effectiv|percentage|trial|immune)', ('F004',), ('U015','U016')),
 (r'\b(safe|safety|rare harm|medical decision)', ('F005','F006'), ('U016','U017')),
 (r'\b(school open|keep every school)', ('F010',), ('U015',)),
 (r'\b(hospital|healthcare|capacity|funding|staffing)', ('F015','F019'), ('U015',)),
 (r'\b(severe|mild|fatality|moderate)', ('F016','F017'), ('U016',)),
 (r'\b(check|proof app|digital|certificate)', (), ('U005',)),
 (r'\b(data|privacy|retain|personal information|store)', (), ('U005','U006')),
 (r'\b(outer|island|ferr|travel|appointment|disab)', ('F015','F022'), ('U003','U013')),
 (r'\b(free choice|cannot work|rights|polio|precedent|individual rights)', ('F011',), ('U012','U018')),
 (r'\b(before|lockdown|restrictions hurt|mandate.*open|access.rule effectiveness)', (), ('U015',)),
 (r'\b(president|enacted|parliament)', ('F013','F025','F026','F027'), ('U009',)),
 (r'\b(emergency powers|abuse|reuse)', ('F032',), ('U008','U009','U018')),
)


def concept_ids(text):
    facts=set();unknowns=set()
    for pattern,fs,us in CONCEPTS:
        if re.search(pattern,text,re.I):facts.update(fs);unknowns.update(us)
    return facts,unknowns
