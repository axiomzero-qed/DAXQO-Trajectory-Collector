import unittest
from trajectory.privacy import sanitize, has_sensitive_ui


class ProtocolPrivacyTests(unittest.TestCase):
    def test_microsecond_timestamp_and_state_links_survive_otp_filter(self):
        timestamp = '2026-10-24T10:41:20.148061Z'
        fingerprint = 'a123456b' * 8
        self.assertEqual(sanitize({'started_at': timestamp, 'from': fingerprint, 'history': [fingerprint]}),
                         {'started_at': timestamp, 'from': fingerprint, 'history': [fingerprint]})
        self.assertNotIn('654321', sanitize('verification value: 654321'))

    def test_real_native_node_with_password_false_is_eligible(self):
        node = {'id': '0.1', 'text': '商品 01', 'description': '', 'password': False,
                'editable': False, 'clickable': True, 'className': 'android.widget.TextView'}
        self.assertFalse(has_sensitive_ui([node]))
        self.assertTrue(has_sensitive_ui([{**node, 'password': True}]))
        self.assertTrue(has_sensitive_ui([{**node, 'text': '验证码'}]))


if __name__ == '__main__':
    unittest.main()
