"""Optional expansion after the ordinary reply and material gates have passed."""
import math
from ctn.persona.router import _extract_jev_answers
from ctn.persona.policy import REPLY_POLICY,REPLY_POLICY_HASH

EXPANSION_VERSION = REPLY_POLICY['version']
MULTI_THRESHOLD = REPLY_POLICY['multi_reply_min_probability']


def expansion_decision(data, *, material_ready):
    action=data.get('action_decision') or {}
    answer=_extract_jev_answers(data).get('multi_reply_space') or {}
    probability=answer.get('value') if isinstance(answer,dict) else None
    valid=(isinstance(answer,dict) and answer.get('status')=='valid'
           and answer.get('type')=='noul' and type(probability) in (float,int)
           and math.isfinite(probability) and 0<=probability<=1)
    allowed=(material_ready and action.get('status')=='decided' and action.get('action')=='reply'
             and valid and probability>=MULTI_THRESHOLD)
    return {'version':EXPANSION_VERSION,'policy_hash':REPLY_POLICY_HASH,'allowed':bool(allowed),'threshold':MULTI_THRESHOLD,
            'probability':probability if valid else None,
            'reason':'complementary_space' if allowed else 'single_reply_fallback'}
