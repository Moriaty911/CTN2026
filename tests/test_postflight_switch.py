"""Operator switch for JEV review of our own drafts (CTN_POSTFLIGHT_JEV)."""
import os
import unittest
from unittest.mock import Mock, patch

from ctn.jev.postflight import JevPostflightChecker, postflight_jev_enabled


class PostflightSwitchTests(unittest.TestCase):
    def test_off_skips_jev_but_keeps_the_local_checks(self):
        client = Mock()
        checker = JevPostflightChecker(client)
        with patch.dict(os.environ, {'CTN_POSTFLIGHT_JEV': 'false'}):
            self.assertFalse(postflight_jev_enabled())
            ok = checker.check(post_id='p', target_text='Is the rule final?', reply_text='My bus is packed at seven, so a quick check at the door seems fair.',
                               assigned_side='yes', intended_purpose='P01', strict_alignment=True)
            self.assertEqual((ok.can_send, ok.reason), (True, 'postflight_jev_disabled'))
            long = checker.check(post_id='p', target_text='t', reply_text='x' * 501, assigned_side='yes')
            self.assertFalse(long.can_send)
            self.assertIn('exceeds_max_chars', long.reason)
            foreign = checker.check(post_id='p', target_text='t', reply_text='这个政策很好', assigned_side='yes')
            self.assertFalse(foreign.can_send)
        client.assert_not_called()
        for attr in ('evaluate', 'ask', 'request', 'post'):
            self.assertFalse(getattr(client, attr).called)

    def test_default_without_the_variable_is_off(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('CTN_POSTFLIGHT_JEV', None)
            self.assertFalse(postflight_jev_enabled())
        with patch.dict(os.environ, {'CTN_POSTFLIGHT_JEV': 'true'}):
            self.assertTrue(postflight_jev_enabled())


if __name__ == '__main__':
    unittest.main()
