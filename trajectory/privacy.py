"""Conservative privacy helpers for trajectory records and UI snapshots."""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any

_SECRET_KEY = re.compile(
    r"(?:password|passwd|passcode|otp|one.?time.?code|verification.?code|验证码|密碼|密码|"
    r"card.?number|credit.?card|银行卡|卡号|chat|聊天|conversation|message.?text|"
    r"secret|token|api.?key|access.?key|private.?key|authorization|bearer|cookie)", re.I
)
_EDITABLE_KEY = re.compile(r"(?:editable|password|input|text|description|content.?description)", re.I)
_EMAIL = re.compile(r"(?<![\w.+-])[\w.+-]{1,64}@[\w.-]+\.[A-Za-z]{2,}(?![\w.-])")
_PHONE = re.compile(r"(?<!\w)(?:\+?\d[\d ()-]{7,}\d)(?!\w)")
_CARD = re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)")
_CREDENTIAL = re.compile(r"(?i)\b(?:bearer|api[_ -]?key|secret|token)\s*[:=]?\s*[A-Za-z0-9._~+/=-]{8,}")
_OTP = re.compile(r"(?<!\d)\d{6}(?!\d)")
_SK_KEY = re.compile(r"(?i)\bsk-[A-Za-z0-9_-]{8,}\b")
_SENSITIVE_UI = re.compile(r"(?:验证码|驗證碼|密码|密碼|支付|付款|聊天|会话|通话|password|passcode|one.?time.?code|\botp\b|payment|checkout|wallet|banking|\bchat\b|conversation)", re.I)
_CLEAR_NODE_FIELDS = re.compile(
    r"(?:text|description|hint|value|label|tooltip|placeholder|accessible.?name|accessible.?text)", re.I
)

# Keep this policy aligned with TrajectoryRecorder.java's marked pure-Java block.
# Namespace boundaries include declared variants, not similarly named packages.
PRIVATE_APP_PREFIXES = (
    "com.whatsapp", "org.telegram.messenger", "org.thunderdog.challegram",
    "org.thoughtcrime.securesms", "com.facebook.orca", "com.instagram.android",
    "com.snapchat.android", "com.viber.voip", "jp.naver.line.android",
    "com.tencent.wework", "com.alibaba.android.rimet", "com.ss.android.lark",
    "com.larksuite.suite", "com.microsoft.teams", "com.slack", "com.discord",
    "com.google.android.apps.messaging", "com.android.mms",
    "com.samsung.android.messaging", "com.miui.mms", "com.android.messaging",
    "com.google.android.apps.dynamite", "com.android.systemui",
)
LEGACY_PRIVATE_APP_TERMS = (
    "wechat", "weixin", "com.tencent.mm", "qq", "mail", "email", "gmail",
    "outlook", "pay", "wallet", "bank", "alipay", "payment", "password", ".gm",
    "authenticator", "contacts", "dialer",
)
_PRIVATE_MESSAGE_RESOURCE = re.compile(
    r"^(?:(?:tv|text|view|item|bubble|chat|conversation|sms|im|incoming|outgoing|received|sent)_)*"
    r"(?:message_(?:text|body|content)|(?:chat|sms)_(?:text|body|content)|chat_message)"
    r"(?:_view|_text_view|_label)?$"
)
_RESOURCE_ID_KEYS = {"id", "resourceid", "viewid", "resourcename"}


def has_message_body_semantic(value: Any) -> bool:
    """Match explicit body identifiers; ordinary message/title/count UI is safe."""
    if not isinstance(value, str):
        return False
    name = value.strip().rsplit("/", 1)[-1]
    name = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", name)
    name = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name)
    name = re.sub(r"[^a-z0-9]+", "_", name.casefold()).strip("_")
    return _PRIVATE_MESSAGE_RESOURCE.fullmatch(name) is not None


def _clean_string(value: str) -> str:
    # UUIDs and opaque IDs must remain stable for idempotency and linkage.
    if re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-8][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}", value):
        return value
    if re.fullmatch(r"[0-9a-fA-F]{64}", value):
        return value  # SHA-256 state links are protocol identifiers, not OTPs.
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|\+00:00)", value):
        try:
            datetime.fromisoformat(value.replace('Z', '+00:00'))
            return value
        except ValueError:
            pass
    value = _CREDENTIAL.sub("[redacted]", value)
    value = _SK_KEY.sub("[redacted]", value)
    value = _OTP.sub("[redacted]", value)
    value = _EMAIL.sub("[redacted]", value)
    value = _PHONE.sub("[redacted]", value)
    return _CARD.sub("[redacted]", value)


def sanitize(value: Any) -> Any:
    """Recursively remove sensitive-key values and scrub common identifiers."""
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            name = str(key)
            canonical = re.sub(r'[^a-z0-9]', '', name.casefold())
            if canonical in {'reasoning', 'reasoningcontent', 'hiddenreasoning', 'chainofthought', 'thinking', 'thoughts'}:
                continue
            if _SECRET_KEY.search(name) or has_message_body_semantic(name):
                cleaned[name] = item if name.casefold() == "password" and isinstance(item, bool) else "[redacted]"
            elif name.casefold() in {"id", "record_id", "action_id", "session_id", "stream_id", "task_id", "device_id",
                                     "fingerprint", "schema_version", "record_type", "sequence"}:
                cleaned[name] = item
            else:
                cleaned[name] = sanitize(item)
        return cleaned
    if isinstance(value, list):
        return [sanitize(item) for item in value]
    if isinstance(value, tuple):
        return [sanitize(item) for item in value]
    if isinstance(value, str):
        return _clean_string(value)
    return value


def sanitize_nodes(nodes: Any) -> list[dict[str, Any]]:
    """Copy UI nodes while clearing values from editable/password controls."""
    if not isinstance(nodes, list):
        return []
    result: list[dict[str, Any]] = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        sensitive = node.get("password") is True or node.get("editable") is True or any(
            node.get(key) is True for key in ("isPassword", "isEditable", "inputField")
        )
        sensitive = sensitive or any(
            has_message_body_semantic(key)
            or (re.sub(r"[^a-z0-9]", "", str(key).casefold()) in _RESOURCE_ID_KEYS
                and has_message_body_semantic(value))
            for key, value in node.items()
        )
        item = sanitize(node)
        if not isinstance(item, dict):
            continue
        if sensitive:
            for key in tuple(item):
                if _CLEAR_NODE_FIELDS.search(key):
                    item[key] = ""
        result.append(item)
    return result


def has_sensitive_ui(nodes: Any) -> bool:
    """Detect privacy-sensitive UI text before any sanitization hides its marker."""
    if not isinstance(nodes, list):
        return False
    for node in nodes:
        if not isinstance(node, dict):
            continue
        if node.get('password') is True or node.get('isPassword') is True:
            return True
        for key, value in node.items():
            if has_message_body_semantic(key) and value not in (False, None, ''):
                return True
            if (re.sub(r"[^a-z0-9]", "", str(key).casefold()) in _RESOURCE_ID_KEYS
                    and has_message_body_semantic(value)):
                return True
            if isinstance(key, str) and _SENSITIVE_UI.search(key) and value not in (False, None, ''):
                return True
            if isinstance(value, str) and _SENSITIVE_UI.search(value):
                return True
    return False


def is_sensitive_app(package_name: Any) -> bool:
    """Return true for common messaging, mail, payment, password, or banking apps."""
    if not isinstance(package_name, str) or not package_name.strip():
        return True
    name = package_name.strip().casefold()
    return (any(name == prefix or name.startswith(prefix + ".") for prefix in PRIVATE_APP_PREFIXES)
            or any(term in name for term in LEGACY_PRIVATE_APP_TERMS))


def redact_record(record: dict[str, Any]) -> dict[str, Any]:
    """Sanitize a record and additionally clean UI accessibility nodes."""
    sensitive_sides: set[str] = set()
    for side in ("before", "after"):
        raw_state = record.get(side)
        if isinstance(raw_state, dict):
            private_app = "package_name" in raw_state and is_sensitive_app(raw_state["package_name"])
            if private_app or has_sensitive_ui(raw_state.get("nodes")):
                sensitive_sides.add(side)
    cleaned = sanitize(record)
    if not isinstance(cleaned, dict):
        return {}
    for side in ("before", "after"):
        state = cleaned.get(side)
        if isinstance(state, dict) and "nodes" in state:
            state["nodes"] = [] if side in sensitive_sides else sanitize_nodes(state["nodes"])
            if side in sensitive_sides:
                state["training_allowed"] = False
    action = cleaned.get("action")
    if isinstance(action, dict):
        parameters = action.get("parameters")
        if isinstance(parameters, dict):
            action["parameters"] = _clear_text_fields(parameters)
        target = action.get("target")
        if isinstance(target, dict):
            sanitized_target = sanitize_nodes([target])
            action["target"] = sanitized_target[0] if sanitized_target else {}
    decision = cleaned.get("decision")
    if isinstance(decision, dict):
        for key in ("selected_action", "candidate_actions"):
            if key in decision:
                decision[key] = _clear_text_fields(decision[key])
    if sensitive_sides:
        privacy = cleaned.get("privacy")
        if not isinstance(privacy, dict):
            privacy = {}; cleaned["privacy"] = privacy
        privacy.update(redacted=True, excluded=True)
        reasons = privacy.setdefault("reasons", [])
        if isinstance(reasons, list) and "sensitive_app_or_ui" not in reasons:
            reasons.append("sensitive_app_or_ui")
        if isinstance(action, dict):
            action["target"] = {}
            action["parameters"] = {}
        if isinstance(decision, dict):
            for key in tuple(decision):
                if key not in {"source", "model", "tool_call_id", "tool", "confidence"}:
                    decision.pop(key)
            decision["candidate_actions"] = []
            decision["selected_action"] = {}
        task = cleaned.get("task")
        if isinstance(task, dict):
            for key in ("user_task", "subgoal"):
                if key in task: task[key] = "[redacted]"
        cleaned.pop("screenshot", None)
        cleaned.pop("image", None)
    return cleaned


def _clear_text_fields(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: ("" if isinstance(key, str) and key.casefold() == "text" else _clear_text_fields(item))
                for key, item in value.items()}
    if isinstance(value, list):
        return [_clear_text_fields(item) for item in value]
    return value
