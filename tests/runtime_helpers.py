"""Explicit synthetic reference for tests that exercise factual reply generation."""
from ctn.jev import BackgroundReference,JevPacker

def with_test_background(brain):
    ref=BackgroundReference(reference_id='test-policy',text='The synthetic test policy defines access conditions for the purposes of this offline fixture.',source_id='synthetic:test-policy-v1')
    brain.trusted_background=(ref,)
    brain.workflow.evaluator.packer=JevPacker(verified_references=(ref,))
    brain.allied_reply_every_n=1
    return brain


def approve_synthetic_reports(store):
    """Explicit test-only human approvals; no production policy bypass."""
    import time
    from ctn.moderation.policy import candidate_payload
    store.set_policy('report_mode','manual')
    store.set_policy('report_clearance',{'allowed':True,'source':'synthetic human review','expires_at':time.time()+300,'live_source_verified':False})
    for profile in store.list_bots_by_status('confirmed_bot'):
        author=profile['account_id']
        for post in store.get_author_posts(author,limit=100):
            payload=candidate_payload(store,'post',post['post_id'],author)
            if payload:store.approve_report(store.store_report_candidate(payload))
        if profile['upheld_strikes'] and (store.confirmed_activity(author) or {}).get('action_count',0)>=100:
            payload=candidate_payload(store,'account',author,author)
            if payload:store.approve_report(store.store_report_candidate(payload))
