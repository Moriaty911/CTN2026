"""Explicit synthetic reference for tests that exercise factual reply generation."""
from ctn.jev import BackgroundReference,JevPacker

def with_test_background(brain):
    ref=BackgroundReference(reference_id='test-policy',text='The synthetic test policy defines access conditions for the purposes of this offline fixture.',source_id='synthetic:test-policy-v1')
    brain.trusted_background=(ref,)
    brain.workflow.evaluator.packer=JevPacker(verified_references=(ref,))
    brain.allied_reply_every_n=1
    return brain
