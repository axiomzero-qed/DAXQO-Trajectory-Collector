"""Regression coverage for precise private-message app and resource filtering."""
import unittest
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

from trajectory.privacy import has_sensitive_ui, is_sensitive_app, redact_record, sanitize


PRIVATE_APP_CASES = (
    ("com.whatsapp", (".w4b", ".beta")),
    ("org.telegram.messenger", (".web",)),
    ("org.thunderdog.challegram", ()),
    ("org.thoughtcrime.securesms", ()),
    ("com.facebook.orca", ()),
    ("com.instagram.android", ()),
    ("com.snapchat.android", ()),
    ("com.viber.voip", ()),
    ("jp.naver.line.android", ()),
    ("com.tencent.wework", ()),
    ("com.alibaba.android.rimet", ()),
    ("com.ss.android.lark", ()),
    ("com.larksuite.suite", ()),
    ("com.microsoft.teams", ()),
    ("com.slack", ()),
    ("com.discord", ()),
    ("com.google.android.apps.messaging", ()),
    ("com.android.messaging", ()),
    ("com.google.android.apps.dynamite", ()),
    ("com.android.mms", ()),
    ("com.samsung.android.messaging", ()),
    ("com.miui.mms", ()),
)

SENSITIVE_RESOURCES = (
    "com.fixture:id/message_text",
    "com.fixture:id/message_body",
    "com.fixture:id/messageBody",
    "com.fixture:id/chatMessageContent",
    "com.fixture:id/incoming_message_text",
    "com.fixture:id/tv_message_body",
    "com.fixture:id/sms_body",
    "com.fixture:id/message_body_view",
    "com.fixture:id/message_text_view",
    "com.fixture:id/message_body_label",
)

ORDINARY_RESOURCES = (
    "com.fixture:id/dialog_message",
    "com.fixture:id/status_message",
    "com.fixture:id/notification_message",
    "com.fixture:id/message_count",
    "com.fixture:id/message_title",
)

JAVA_RECORDER_SOURCES = (
    Path(__file__).resolve().parents[1] / "integration" / "android" / "TrajectoryRecorder.java",
)
JAVA_BIN = Path(os.environ.get("JAVA_HOME", "/jdk-not-configured")) / "bin"


def page(package, node):
    return {"fingerprint": "a" * 64, "package_name": package, "nodes": [node],
            "width": 400, "height": 800, "training_allowed": True, "redacted": True}


def sensitive_record(package="com.example.fixture", resource="com.fixture:id/message_body"):
    marker = "fixture-private-body"
    return {
        "before": page(package, {"resource_id": resource, "text": marker}),
        "after": page(package, {"view_id": resource, "text": marker}),
        "action": {"type": "tap", "target": {"text": marker, "contentDescription": marker},
                   "parameters": {"nested": {"text": marker}}},
        "decision": {
            "selected_action": {"type": "type", "text": marker, "target": {"text": marker}},
            "candidate_actions": [{"type": "tap", "text": marker, "target": {"text": marker}}],
        },
        "privacy": {"redacted": True, "excluded": False, "reasons": []},
    }


class PrivateChatPrivacyTests(unittest.TestCase):
    def test_private_app_package_families_are_sensitive(self):
        for base, suffixes in PRIVATE_APP_CASES:
            with self.subTest(package=base):
                self.assertTrue(is_sensitive_app(base))
                for suffix in suffixes:
                    self.assertTrue(is_sensitive_app(base + suffix), base + suffix)
        self.assertTrue(is_sensitive_app("com.Slack"))

    def test_package_lookalikes_are_not_sensitive(self):
        for package in ("com.whatsappish", "org.telegram.messengerish", "com.slackboard",
                        "com.example.messagecatalog", "io.daxqo.trajectoryfixture"):
            with self.subTest(package=package):
                self.assertFalse(is_sensitive_app(package))

    def test_message_body_resource_semantics_match_exact_ids(self):
        for resource in SENSITIVE_RESOURCES:
            with self.subTest(resource=resource):
                self.assertTrue(has_sensitive_ui([{"resource_id": resource}]))
                self.assertTrue(has_sensitive_ui([{"resourceId": resource}]))
                self.assertTrue(has_sensitive_ui([{"view_id": resource}]))
                self.assertTrue(has_sensitive_ui([{"viewId": resource}]))
                self.assertTrue(has_sensitive_ui([{"id": resource}]))

    def test_ordinary_message_related_resources_do_not_trigger_semantic_filter(self):
        self.assertFalse(has_sensitive_ui([{"text": "消息"}]))
        for resource in ORDINARY_RESOURCES:
            with self.subTest(resource=resource):
                for key in ("resource_id", "resourceId", "view_id", "viewId", "id"):
                    self.assertFalse(has_sensitive_ui([{key: resource}]), (key, resource))

    def test_private_package_or_body_resource_excludes_and_clears_whole_record(self):
        records = (sensitive_record(package="com.whatsapp"),
                   sensitive_record(resource="com.fixture:id/chatMessageContent"))
        for record in records:
            with self.subTest(package=record["before"]["package_name"],
                              resource=record["before"]["nodes"][0].get("resource_id")):
                cleaned = redact_record(record)
                self.assertEqual(cleaned["before"]["nodes"], [])
                self.assertEqual(cleaned["after"]["nodes"], [])
                self.assertFalse(cleaned["before"]["training_allowed"])
                self.assertFalse(cleaned["after"]["training_allowed"])
                self.assertTrue(cleaned["privacy"]["excluded"])
                self.assertNotIn("fixture-private-body", repr(cleaned))

    def test_message_body_metadata_key_redacts_its_value(self):
        cleaned = sanitize({"message_body": "fixture-private-body"})
        self.assertNotIn("fixture-private-body", repr(cleaned))

    def test_shared_java_policy_matches_python_when_marked_block_is_available(self):
        javac = shutil.which("javac") or str(JAVA_BIN / "javac.exe")
        java = shutil.which("java") or str(JAVA_BIN / "java.exe")
        if not Path(javac).is_file() or not Path(java).is_file():
            self.skipTest("JDK 17 is unavailable")
        marker_pattern = re.compile(
            r"// BEGIN SHARED PRIVATE MESSAGE POLICY\s*(.*?)\s*// END SHARED PRIVATE MESSAGE POLICY",
            re.S,
        )
        sources = []
        for path in JAVA_RECORDER_SOURCES:
            if path.is_file():
                match = marker_pattern.search(path.read_text(encoding="utf-8"))
                if match:
                    sources.append((path, match.group(1)))
        if not sources:
            self.skipTest("Shared Java policy marker has not been added yet")

        app_cases = [base for base, _ in PRIVATE_APP_CASES]
        app_cases.extend(base + suffix for base, suffixes in PRIVATE_APP_CASES for suffix in suffixes)
        app_cases.extend(("com.Slack", "com.whatsappish", "org.telegram.messengerish", "com.slackboard",
                          "com.example.messagecatalog", "io.daxqo.trajectoryfixture"))
        resources = SENSITIVE_RESOURCES + ORDINARY_RESOURCES
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            for source_path, policy_block in sources:
                class_source = (
                    "import java.util.*;\nimport java.util.regex.Pattern;\n"
                    "public final class PrivatePolicyHarness {\n" + policy_block + "\n"
                    "public static void main(String[] args) {\n"
                    "for (String value : args) {\n"
                    "String[] parts=value.split(\"\\\\t\",2);\n"
                    "boolean hit=parts[0].equals(\"APP\") ? privateApp(parts[1]) : hasMessageBodySemantic(parts[1]);\n"
                    "System.out.println(parts[0]+\"\\t\"+parts[1]+\"\\t\"+hit);\n"
                    "}}}\n"
                )
                java_file = temp / "PrivatePolicyHarness.java"
                java_file.write_text(class_source, encoding="utf-8")
                compiled = subprocess.run([javac, "-encoding", "UTF-8", "-d", str(temp), str(java_file)],
                                          capture_output=True, text=True)
                self.assertEqual(compiled.returncode, 0, f"Java policy compile failed for {source_path}: {compiled.stderr}")
                args = [*("APP\t" + value for value in app_cases),
                        *("RESOURCE\t" + value for value in resources)]
                run = subprocess.run([java, "-cp", str(temp), "PrivatePolicyHarness", *args],
                                     capture_output=True, text=True)
                self.assertEqual(run.returncode, 0, f"Java policy run failed for {source_path}: {run.stderr}")
                actual = {}
                for line in run.stdout.splitlines():
                    kind, value, result = line.split("\t", 2)
                    actual[(kind, value)] = result == "true"
                for package in app_cases:
                    with self.subTest(source=str(source_path), package=package):
                        self.assertEqual(actual[("APP", package)], is_sensitive_app(package))
                for resource in resources:
                    with self.subTest(source=str(source_path), resource=resource):
                        self.assertEqual(actual[("RESOURCE", resource)], has_sensitive_ui([{"resource_id": resource}]))


if __name__ == "__main__":
    unittest.main()
