"""Occupation routing with shared account eligibility and persisted-plan sampling."""
import random
import re
import time
import math
from typing import Any
from .models import AccountState, RoutingDecision
from .registry import PersonaRegistry, get_default_registry
from .policy import REPLY_POLICY
from .catalog import GROUP_ALLOCATION

SCENARIOS={
 'transport':('bus','ferry','transport','commut','driver','passenger','delivery'),
 'business':('shop','restaurant','cafe','business','cost','tourist','booking','retail'),
 'tourism':('touris','hotel','visitor','dive','guest','marina'),
 'education':('school','classroom','student','campus','teacher','university'),
 'sport':('sport','coach','team training','oval','lesson'),
 'outdoor':('park','outdoor','garden','beach','oval','market'),
 'healthcare':('hospital','clinic','care worker','patient','health system','nurse'),
 'vaccine_evidence':('trial','clinical','study','efficacy','side effect','transmission','immune','vaccine evidence'),
 'privacy':('privacy','tracking','personal data','visit history','database','retaining'),
 'technology':('app','phone','qr','digital','scanner','connection','software'),
 'rural':('farm','rural','outer island','orchard','grower','fishing'),
 'workplace':('workplace','shift','staff','employer','contractor','worksite'),
 'administration':('procedure','guidance','implementation','proof','certificate','requirement'),
 'law':('law','legal','rights','statute','proportional','exemption','clause','policy version','municipal','ordinance'),
 'access':('access','entrance','entry','public space'),
 'discussion':('argument','explain','clarify','evidence','meaning'),
}
TOPIC_TAGS={'vaccine_evidence':'vaccine_evidence','public_health':'healthcare',
    'public_access':'access','economic_effects':'business','voting_process':'administration'}


def _extract_jev_answers(data):
    if not isinstance(data,dict): return {}
    if isinstance(data.get('answers'),dict): return data['answers']
    assessment = ((data.get('flow') or {}).get('deep') or {}).get('assessment') or {}
    return (assessment.get('output') or {}).get('answers',{})


def _get_val(answer):
    return answer.get('value') if isinstance(answer,dict) else answer


def scenario_tags(data,text):
    lower=text.lower()
    tags={tag for tag,words in SCENARIOS.items() if any(re.search(r'\b'+re.escape(word),lower) for word in words)}
    topic=_get_val(_extract_jev_answers(data).get('topic'))
    if topic in TOPIC_TAGS:tags.add(TOPIC_TAGS[topic])
    return tags


def preferred_group(data):
    answer=_extract_jev_answers(data).get('preferred_occupation') or {}
    if not isinstance(answer,dict) or answer.get('status')!='valid':return None
    probabilities=answer.get('probabilities') or {}
    selected=answer.get('value')
    if selected not in GROUP_ALLOCATION or not isinstance(probabilities,dict) or selected not in probabilities:return None
    if any(type(v) not in (int,float) or not math.isfinite(v) or not 0<=v<=1 for v in probabilities.values()):return None
    p=probabilities[selected]
    margin=p-max((v for k,v in probabilities.items() if k!=selected),default=0)
    return selected if p>=REPLY_POLICY['occupation_min_probability'] and margin>=REPLY_POLICY['occupation_min_margin'] else None


class PersonaRouter:
    def __init__(self,registry=None,*,default_side='yes',store=None,db_path=':memory:',allow_unbound=False,rng=None):
        from ctn.storage import StateStore
        self.store=store if store is not None else StateStore(db_path)
        self.registry=(registry if registry is not None else get_default_registry()).attach(self.store)
        self.default_side=default_side
        self.allow_unbound=allow_unbound
        self.rng=rng or random.Random()
        self._states={}

    def get_state(self,account_id):
        return self.store.get_account_state(account_id)

    def mark_used(self,account_id,now=None,*,target_stance=''):
        self.store.mark_account_used(account_id,now=now,target_stance=target_stance)

    def set_account_frozen(self,account_id,frozen=True):return self.store.set_account_frozen(account_id,frozen=frozen)
    def set_account_banned(self,account_id,banned=True):return self.store.set_account_banned(account_id,banned=banned)
    def reset_states(self):self.store.reset_accounts()
    def dump_states(self):return {k:v.to_dict() for k,v in self.store.load_all_account_states().items()}
    def load_states(self,states_data):
        for account,data in states_data.items():
            self.store.save_account_state(AccountState(**{k:v for k,v in {**data,'account_id':account}.items() if k in AccountState.__dataclass_fields__}))

    def context_for(self,persona):
        return {'persona_id':persona.persona_id,'persona_version':persona.version,
            'occupation_group':persona.occupation_group,'catalog_hash':self.registry.catalog_hash,
            'voice_style':persona.profile.get('voice',{}).get('style',''),
            'cooldown_s':persona.cooldown_s}

    def _eligible(self,data,side,now):
        if self.registry.catalog_hash and self.store.policy('active_persona_catalog')!=self.registry.catalog_hash:
            raise ValueError('persona_catalog_mismatch')
        candidates=[p for p in self.registry.list_by_side(side) if p.account_id]
        states=self.store.account_states_for(p.account_id for p in candidates)
        if self.allow_unbound:
            for p in candidates:
                if p.account_id not in states:states[p.account_id]=self.get_state(p.account_id)
        requirements=data.get('action_decision') or {}
        high='high' in (requirements.get('required_account_level'),requirements.get('minimum_account_level'))
        bindings=self.store.persona_bindings()
        from ctn.pipeline.targeting import accounts_at_daily_cap
        capped=accounts_at_daily_cap(self.store,[p.account_id for p in candidates],now=now)
        available=[];cooling=[]
        for p in candidates:
            if p.account_id in capped:continue
            state=states.get(p.account_id)
            # capability_tier is a soft preference applied in route(), never an eligibility filter.
            if state is None or state.is_disabled():continue
            if not self.allow_unbound and not bindings.get(p.persona_id)==p.account_id:continue
            if self.store.policy('require_warmup',False):
                if not self.store.account_initialise_ready(p.account_id,now=now):continue
            if state.is_in_cooldown(now,p.cooldown_s):cooling.append((p,state))
            else:available.append((p,state))
        return available,cooling

    def route(self,data,post_text,side=None,prompt_type=None,now=None,*,complementary=None):
        now=time.time() if now is None else now
        side=(side or self.default_side).lower()
        available,cooling=self._eligible(data,side,now)
        tags=scenario_tags(data,post_text);preferred=preferred_group(data)
        requirements=data.get('action_decision') or {}
        high_note=''
        if available and 'high' in (requirements.get('required_account_level'),requirements.get('minimum_account_level')):
            available,high_note=self._prefer_high(available)
        if not available:
            if not cooling:raise ValueError('no_bound_active_capable_accounts')
            p,state=min(cooling,key=lambda pair:pair[1].remaining_cooldown(now,pair[0].cooldown_s))
            return RoutingDecision(p.account_id,p,p.occupation_group,0,'all_accounts_in_cooldown',True,True)
        preferred_available=[pair for pair in available if pair[0].occupation_group==preferred]
        if preferred_available:
            group=preferred;pool=preferred_available;score=1+len(set(pool[0][0].topic_tags)&tags);reason='jev_preferred_occupation'
        else:
            scores={p.occupation_group:len(set(p.topic_tags)&tags) for p,_ in available}
            best=max(scores.values(),default=0)
            groups=[g for g,s in scores.items() if s==best]
            if best==0:
                general=[g for g in groups if g in ('office_community','independent_commentary')]
                groups=general or groups
            group=self.rng.choice(sorted(groups));pool=[pair for pair in available if pair[0].occupation_group==group]
            score=best;reason='scenario_tag_match' if best else 'general_clarification_fallback'
        maintained=[pair for pair in pool if (self.store.lifecycle(pair[0].account_id) or {}).get('maintenance')]
        if maintained and (prompt_type in ('P02','P07') or self.store.effective_phase(now=now)=='climax'):pool=maintained
        least=min(s.last_posted_at for _,s in pool)
        p,state=self.rng.choice([pair for pair in pool if pair[1].last_posted_at==least])
        if high_note:reason=high_note+':'+reason
        return RoutingDecision(p.account_id,p,group,float(score),reason,preferred is not None and preferred!=group)

    def _prefer_high(self,available,*,history_pool=5):
        """High-value targets prefer high-tier, then maintained, then experienced standard accounts."""
        tier=[pair for pair in available if pair[1].capability_tier=='high']
        if tier:return tier,'high_tier'
        maintained=[pair for pair in available if (self.store.lifecycle(pair[0].account_id) or {}).get('maintenance')]
        if maintained:return maintained,'high_fallback_maintenance'
        counts=self.store.publication_counts([pair[0].account_id for pair in available])
        experienced=sorted((pair for pair in available if counts.get(pair[0].account_id,0)>0),
                           key=lambda pair:-counts[pair[0].account_id])
        if experienced:
            cutoff=counts[experienced[min(history_pool,len(experienced))-1][0].account_id]
            return [pair for pair in experienced if counts[pair[0].account_id]>=cutoff],'high_fallback_standard'
        return available,'high_fallback_standard'

    def additional_candidates(self,data,post_text,primary_account,count,*,rng=None,now=None,purpose=None,materials=None):
        rng=rng or self.rng;now=time.time() if now is None else now
        if materials is not None:
            if (purpose not in ('P01','P02','P03','P07','P08') or materials.get('prompt_type')!=purpose
                or (materials.get('action_decision') or {}).get('action')!='reply'):
                return []
            if purpose=='P08':
                if not materials.get('information_gap'):return []
            elif not materials.get('support'):return []
        available,_=self._eligible(data,self.default_side,now)
        tags=scenario_tags(data,post_text);preferred=preferred_group(data)
        pool=[pair for pair in available if pair[0].account_id!=primary_account and
              (set(pair[0].topic_tags)&tags or pair[0].occupation_group==preferred)]
        selected=[]
        for _ in range(count):
            groups=sorted({p.occupation_group for p,_ in pool})
            if not groups:break
            group=rng.choice(groups)
            choices=[pair for pair in pool if pair[0].occupation_group==group]
            least=min(s.last_posted_at for _,s in choices)
            p,_=rng.choice([pair for pair in choices if pair[1].last_posted_at==least])
            selected.append(p)
            pool=[pair for pair in pool if pair[0].account_id!=p.account_id]
        return selected
