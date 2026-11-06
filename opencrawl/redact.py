"""Keep secrets and personal details out of logs, state files and exports.

API keys travel in request headers, never in URLs, but an operator's own URL (a feed with ?token=, a signed link)
or a library's error message can still carry one. Everything the crawler writes about a request (progress status,
the block list, log lines, exception text) goes through these helpers first.

  url(u)          the URL with the values of secret-looking query parameters replaced, and any user:password@ removed
  text(s)         the same for every URL or query fragment inside free text, plus bearer tokens and keys in headers
  subject_id(s)   with OPENCRAWL_REDACT_LOGS=1, a short stable hash instead of a subject id in log lines
  clean_output(s) control characters removed and workflow-command lines (`::set-output` and friends) defused, for
                  text taken from the web and printed in a CI log
"""
from __future__ import annotations

import hashlib
import os
import re

SECRET_PARAM = (r"(?:api[_-]?key|apikey|key|access[_-]?key|token|access[_-]?token|api[_-]?token|auth|auth[_-]?token|"
                r"password|passwd|pwd|secret|client[_-]?secret|signature|sig|x-amz-signature|x-amz-credential|"
                r"x-amz-security-token|x-goog-signature|x-goog-credential|session[_-]?id|jwt|bearer)")
_PARAM_RX = re.compile(rf"([?&;]{SECRET_PARAM}=)[^&#\s'\"<>]*", re.I)
_USERINFO_RX = re.compile(r"(\b[a-z][a-z0-9+.-]{0,30}://)[^/@\s'\"<>]{1,256}@", re.I)
_BEARER_RX = re.compile(r"\b(Bearer|Basic|Token)\s+[A-Za-z0-9._~+/=-]{8,}", re.I)
_HEADER_RX = re.compile(r"((?:x-api-key|x-goog-api-key|authorization|api-key|x-auth-token)['\"]?\s*[:=]\s*['\"]?)"
                        r"[^'\"\s,}]+", re.I)
_CTRL_RX = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u202a-\u202e\u2066-\u2069]")
REDACTED = "REDACTED"


def url(u: str | None) -> str:
    if not u:
        return u or ""
    return _PARAM_RX.sub(lambda m: m.group(1) + REDACTED, _USERINFO_RX.sub(r"\1", str(u)))


def text(s) -> str:
    s = "" if s is None else str(s)
    s = _USERINFO_RX.sub(r"\1", s)
    s = _PARAM_RX.sub(lambda m: m.group(1) + REDACTED, s)
    s = _BEARER_RX.sub(lambda m: m.group(1) + " " + REDACTED, s)
    return _HEADER_RX.sub(lambda m: m.group(1) + REDACTED, s)


def exc(e: BaseException, limit: int = 300) -> str:
    """'TypeName: message' with secrets removed, cut to `limit` characters."""
    return text(f"{type(e).__name__}: {e}")[:limit]


def logs_redacted() -> bool:
    return os.environ.get("OPENCRAWL_REDACT_LOGS", "").strip().lower() in ("1", "true", "yes")


def subject_id(sid: str) -> str:
    if not logs_redacted() or not sid:
        return sid
    return "subject-" + hashlib.sha256(sid.encode()).hexdigest()[:10]


def clean_output(s) -> str:
    """Text from the web, made safe to print in a CI log: no control/bidi characters, and no line that a runner
    would read as a workflow command (GitHub Actions treats lines starting with :: as commands)."""
    s = _CTRL_RX.sub("", "" if s is None else str(s))
    return "\n".join(("\u200b" + ln) if ln.lstrip().startswith("::") else ln for ln in s.split("\n"))
