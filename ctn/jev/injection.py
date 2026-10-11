"""Bounded, offline review signals for untrusted observations.

This is a heuristic preflight, not a malicious-intent classifier or proof that
an input is safe. Normal quotations can require review. Inspection views never
replace original text; findings refer to Unicode offsets in the original field.
No network, code execution, recursive decoding, or automatic sanitization.
"""

import base64
import binascii
from dataclasses import dataclass
import hashlib
import json
import re
import unicodedata


INJECTION_SCHEMA_VERSION = "jev-injection-preflight.v1"
# Fixed rules are internal configuration, never supplied by a platform record.
_RULES = tuple((name, re.compile(pattern, re.IGNORECASE)) for name, pattern in (
    ("instruction_override", r"\b(?:ignore|disregard|forget|bypass|override)\s+(?:(?:all|the|your|any|previous|prior|above|earlier|existing|system|developer|evaluation|safety|scoring)\s+){0,7}(?:instructions?|rules?|prompts?|rubrics?|safeguards?)\b"),
    ("role_spoofing", r"<\|(?:im_start|start_header_id)\|>\s*(?:system|developer)|\[\s*(?:system|developer)\s*\]|<\s*/?\s*(?:system|developer)\s*>|\bsystem\s+override\b"),
    ("secret_request", r"\b(?:reveal|print|expose|leak|return|show|send)\b[^\n.!?]{0,64}\b(?:(?:your|internal|hidden|system)\s+(?:prompt|instructions)|api[ _-]?key|access[ _-]?token|credentials|secrets)\b"),
    ("assessment_override", r"\b(?:set|assign|force|mark|classify|label|rate)\b[^\n.!?]{0,64}\b(?:reply_value|response_specificity|policy_stance|prompt_injection|injection\s+risk|this\s+post|this\s+content)\b[^\n.!?]{0,32}(?:\b[0-4]\b|\b(?:aligned|opposing|neutral|safe|yes|no)\b)"),
    ("forced_output", r"\b(?:output|return|respond\s+with)\s+(?:only\s+)?(?:the\s+)?(?:following\s+)?json\s*[:{]|\b(?:do\s+not|never)\s+(?:report|flag|detect|mention)\b[^\n.!?]{0,40}\b(?:injection|this\s+instruction|risk)\b"),
    ("instruction_override_zh", r"(?:忽略|无视|绕过|覆盖|忘记).{0,16}(?:指令|提示词|评分规则|评估规则|安全规则)|(?:输出|泄露|显示|发送).{0,16}(?:密钥|系统提示词|内部指令)|(?:把|将).{0,24}(?:本帖|帖子|注入风险|回复价值).{0,16}(?:设为|设置为|判为|评为).{0,12}(?:[0-4]|我方|安全)|不要.{0,12}(?:报告|标记|检测).{0,12}(?:注入|风险)"),
))
_BIDI = re.compile("[\u202a-\u202e\u2066-\u2069]")
_IGNORABLE = frozenset("\u200b\u200c\u200d\u2060\ufeff\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069")
_ENCODED = re.compile(r"\b(?P<kind>base64|b64|hex)\s*:\s*(?P<token>[A-Za-z0-9+/=_-]{24,})", re.IGNORECASE)
_WHOLE_TOKEN = re.compile(r"[A-Za-z0-9+/=_-]{32,}")


@dataclass(frozen=True)
class InjectionLimits:
    max_total_chars: int = 262_144
    max_string_chars: int = 131_072
    max_strings: int = 4096
    max_nodes: int = 16_384
    max_depth: int = 16
    max_findings: int = 32
    max_encoded_candidates: int = 256
    max_decoded_chars: int = 4096

    def __post_init__(self):
        if any(type(v) is not int or v <= 0 for v in vars(self).values()):
            raise ValueError("injection limits: expected positive integers")


@dataclass(frozen=True)
class InjectionFinding:
    path: str
    rule: str
    start: int
    end: int
    view: str

    def to_dict(self):
        return vars(self).copy()


@dataclass(frozen=True)
class InjectionCheck:
    policy_fingerprint: str
    findings: tuple[InjectionFinding, ...]
    complete: bool
    limits_reached: tuple[str, ...] = ()

    @property
    def requires_review(self):
        return bool(self.findings) or not self.complete

    def to_dict(self):
        return {"schema_version": INJECTION_SCHEMA_VERSION,
                "policy_fingerprint": self.policy_fingerprint,
                "status": "needs_review" if self.requires_review else "no_local_signal",
                "complete": self.complete, "limits_reached": list(self.limits_reached),
                "findings": [f.to_dict() for f in self.findings],
                "scope": "local_heuristics_not_a_safety_guarantee"}


def _view(text):
    """Normalize for detection only, retaining offsets into unchanged input."""
    chars, offsets = [], []
    for index, char in enumerate(text):
        if char not in _IGNORABLE:
            expanded = unicodedata.normalize("NFKC", char).casefold()
            chars.extend(expanded)
            offsets.extend([index] * len(expanded))
    return "".join(chars), offsets


class InjectionPreflight:
    def __init__(self, *, limits: InjectionLimits | None = None):
        self.limits = limits if limits is not None else InjectionLimits()
        if type(self.limits) is not InjectionLimits:
            raise ValueError("injection limits: expected InjectionLimits")

    @property
    def fingerprint(self):
        data = {"version": INJECTION_SCHEMA_VERSION, "limits": vars(self.limits),
                "rules": [(name, rule.pattern) for name, rule in _RULES],
                "normalization": "per_character_NFKC_casefold_with_original_offsets",
                "ignored_characters": sorted(_IGNORABLE), "bidi_pattern": _BIDI.pattern,
                "encoding": "labelled_or_whole_field_base64_hex_one_level"}
        return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()

    def check(self, observations, *, path="content"):
        """Scan all supplied string values, including optional/omitted fields.

        Callers validate contracts first. Dictionary keys in those contracts are
        fixed field names. A budget overrun requests review, never a clean result.
        """
        findings, reached = [], []
        total = strings = encoded_count = nodes = 0
        halted = False

        def limit(name):
            nonlocal halted
            if name not in reached:
                reached.append(name)
            halted = True

        def add(where, rule, start, end, view):
            if len(findings) >= self.limits.max_findings:
                limit("max_findings")
                return
            findings.append(InjectionFinding(where, rule, start, end, view))

        def match_rules(text, where, *, encoded_span=None, encoding=None):
            normalized, offsets = _view(text)
            for name, rule in _RULES:
                for match in rule.finditer(normalized):
                    if halted:
                        return
                    start, end = encoded_span if encoded_span else (offsets[match.start()], offsets[match.end() - 1] + 1)
                    add(where, name, start, end, encoding or "normalized")

        def inspect(text, where):
            nonlocal total, strings, encoded_count
            strings += 1
            total += len(text)
            if strings > self.limits.max_strings:
                return limit("max_strings")
            if len(text) > self.limits.max_string_chars:
                return limit("max_string_chars")
            if total > self.limits.max_total_chars:
                return limit("max_total_chars")
            for match in _BIDI.finditer(text):
                add(where, "bidi_control", match.start(), match.end(), "original")
                if halted:
                    return
            match_rules(text, where)
            if halted:
                return
            candidates = [(m.group("kind").lower(), m.group("token"), m.span("token")) for m in _ENCODED.finditer(text)]
            stripped = text.strip()
            if _WHOLE_TOKEN.fullmatch(stripped):
                start = len(text) - len(text.lstrip())
                candidates.append(("auto", stripped, (start, start + len(stripped))))
            for kind, token, span in candidates:
                encoded_count += 1
                if encoded_count > self.limits.max_encoded_candidates:
                    return limit("max_encoded_candidates")
                if len(token) > self.limits.max_decoded_chars * 2:
                    return limit("max_decoded_chars")
                codecs = ("base64", "hex") if kind == "auto" else ("base64" if kind == "b64" else kind,)
                for codec in codecs:
                    try:
                        raw = bytes.fromhex(token) if codec == "hex" else base64.b64decode(
                            token + "=" * (-len(token) % 4), altchars=b"-_", validate=True)
                        decoded = raw.decode("utf-8")
                    except (ValueError, UnicodeError, binascii.Error):
                        continue
                    if len(decoded) > self.limits.max_decoded_chars:
                        return limit("max_decoded_chars")
                    match_rules(decoded, where, encoded_span=span, encoding=codec)
                    if halted:
                        return

        def walk(value, where, depth=0):
            nonlocal nodes
            if halted:
                return
            nodes += 1
            if nodes > self.limits.max_nodes:
                return limit("max_nodes")
            if depth > self.limits.max_depth:
                return limit("max_depth")
            if type(value) is str:
                inspect(value, where)
            elif type(value) is dict:
                for key, child in value.items():
                    walk(child, f"{where}.{key}" if where else key, depth + 1)
                    if halted:
                        return
            elif type(value) in {list, tuple}:
                for index, child in enumerate(value):
                    walk(child, f"{where}[{index}]", depth + 1)
                    if halted:
                        return

        walk(observations, path)
        return InjectionCheck(self.fingerprint, tuple(findings), not reached, tuple(reached))
