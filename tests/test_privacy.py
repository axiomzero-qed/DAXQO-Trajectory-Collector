import unittest

from trajectory.privacy import is_sensitive_app, redact_record, sanitize, sanitize_nodes


class PrivacyTests(unittest.TestCase):
    def test_recursive_sensitive_key_and_identifier_redaction(self):
        source = {"password": "secret", "nested": {"验证码": "246810", "chat": "hello"},
                  "note": "mail me at person@example.com or call +1 202 555 0198", "credential": "Bearer abcdEFGH1234"}
        cleaned = sanitize(source)
        self.assertEqual(cleaned["password"], "[redacted]")
        self.assertEqual(cleaned["nested"]["验证码"], "[redacted]")
        self.assertEqual(cleaned["nested"]["chat"], "[redacted]")
        self.assertNotIn("person@example.com", cleaned["note"])
        self.assertNotIn("202 555 0198", cleaned["note"])
        self.assertNotIn("abcdEFGH1234", cleaned["credential"])

    def test_nodes_clear_password_and_editable_content(self):
        nodes = [{"text": "Visible button", "editable": False},
                 {"text": "654321", "description": "验证码", "editable": True, "class": "EditText"},
                 {"text": "private", "password": True}]
        cleaned = sanitize_nodes(nodes)
        self.assertEqual(cleaned[0]["text"], "Visible button")
        self.assertEqual(cleaned[1]["text"], "")
        self.assertEqual(cleaned[1]["description"], "")
        self.assertEqual(cleaned[2]["text"], "")

    def test_sensitive_ui_blocks_whole_page_and_clears_action_and_candidate_text(self):
        record = {"before": {"training_allowed": True, "nodes": [{"text": "商品列表"}]},
                  "after": {"training_allowed": True, "nodes": [{"text": "验证码"}]},
                  "action": {"parameters": {"text": "654321"}},
                  "decision": {"candidate_actions": [{"text": "tap a secret value", "type": "tap"}]}}
        cleaned = redact_record(record)
        self.assertTrue(cleaned["before"]["training_allowed"])
        self.assertFalse(cleaned["after"]["training_allowed"])
        self.assertEqual(cleaned["after"]["nodes"], [])
        self.assertEqual(cleaned["action"]["parameters"], {})
        self.assertEqual(cleaned["decision"]["candidate_actions"], [])
        self.assertTrue(cleaned["privacy"]["excluded"])

    def test_metadata_six_digit_otp_and_openai_style_key_are_scrubbed(self):
        cleaned = sanitize({"metadata": {"candidate": "654321", "token_hint": "sk-abcdefghijklmno"},
                            "fingerprint": "screen-654321", "schema_version": "daxqo.trajectory/0.1"})
        self.assertEqual(cleaned["metadata"]["candidate"], "[redacted]")
        self.assertEqual(cleaned["metadata"]["token_hint"], "[redacted]")
        self.assertEqual(cleaned["fingerprint"], "screen-654321")
        self.assertEqual(cleaned["schema_version"], "daxqo.trajectory/0.1")

    def test_editable_description_variants_are_cleared(self):
        cleaned = sanitize_nodes([{"editable": True, "contentDescription": "private", "hint": "secret", "value": "123"}])
        self.assertEqual(cleaned[0]["contentDescription"], "")
        self.assertEqual(cleaned[0]["hint"], "")
        self.assertEqual(cleaned[0]["value"], "")

    def test_action_target_and_nested_text_fields_are_cleared(self):
        record = {
            "before": {"training_allowed": True, "nodes": []},
            "after": {"training_allowed": True, "nodes": []},
            "action": {
                "target": {"editable": True, "text": "typed", "contentDescription": "private note",
                           "hint": "hint text", "label": "label text", "id": "node-1"},
                "parameters": {"nested": {"text": "typed secret", "other": "kept"}},
            },
            "decision": {
                "selected_action": {"type": "type", "parameters": {"text": "candidate secret"}},
                "candidate_actions": [{"type": "type", "args": {"text": "candidate secret"}}],
            },
        }
        cleaned = redact_record(record)
        target = cleaned["action"]["target"]
        self.assertEqual(target["contentDescription"], "")
        self.assertEqual(target["hint"], "")
        self.assertEqual(target["label"], "")
        self.assertEqual(target["text"], "")
        self.assertEqual(target["id"], "node-1")
        self.assertEqual(cleaned["action"]["parameters"]["nested"]["text"], "")
        self.assertEqual(cleaned["action"]["parameters"]["nested"]["other"], "kept")
        self.assertEqual(cleaned["decision"]["selected_action"]["parameters"]["text"], "")
        self.assertEqual(cleaned["decision"]["candidate_actions"][0]["args"]["text"], "")

    def test_sensitive_apps_fail_closed(self):
        for package in ("com.tencent.mm", "com.tencent.mobileqq", "com.google.android.gm",
                        "com.example.bank", "com.example.wallet", "com.example.passwordmanager"):
            self.assertTrue(is_sensitive_app(package), package)
        self.assertFalse(is_sensitive_app("io.daxqo.trajectoryfixture"))
        self.assertTrue(is_sensitive_app(None))


if __name__ == "__main__":
    unittest.main()
