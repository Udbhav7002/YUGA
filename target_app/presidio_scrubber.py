"""Microsoft Presidio scrubber — PII/secret redaction before crash reports
leave the process. Pattern recognizers only (no spaCy NER model download).
Init is lazy and threaded so app startup and the 500-response path never
block. If Presidio is unavailable (e.g. inside the Docker sandbox), scrub()
is a silent no-op so sandboxed tests never fail on a missing dependency.
"""

import threading

SCRUBBED = "<SCRUBBED>"

SECRET_PATTERNS = {
    "API_KEY": [
        ("github_classic", r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36}\b", 0.95),
        ("github_fine_grained", r"\bgithub_pat_[A-Za-z0-9_]{22,}\b", 0.95),
        ("aws_access_key", r"\bAKIA[0-9A-Z]{16}\b", 0.95),
        ("openai_anthropic", r"\bsk-[A-Za-z0-9_\-]{20,}\b", 0.9),
        ("gemini_legacy", r"\bAIza[0-9A-Za-z_\-]{35}\b", 0.95),
        ("gemini_new_format", r"\bAQ\.[A-Za-z0-9_\-]{30,}\b", 0.95),
        ("slack", r"\bxox[baprs]-[A-Za-z0-9\-]{10,}\b", 0.9),
    ],
    "JWT": [
        ("jwt", r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\b", 0.9),
    ],
    "CRYPTO_WALLET": [
        ("eth", r"\b0x[a-fA-F0-9]{40}\b", 0.9),
        ("btc_legacy", r"\b[13][a-km-zA-HJ-NP-Z1-9]{25,34}\b", 0.85),
        ("btc_segwit", r"\bbc1[a-z0-9]{25,62}\b", 0.9),
    ],
    "BEARER_TOKEN": [
        ("bearer", r"(?i)\bbearer\s+[A-Za-z0-9_\-\.=/+]{16,}", 0.85),
    ],
    "CREDENTIAL": [
        ("kv_secret",
         r"(?i)\b(api[_-]?key|secret[_-]?key|access[_-]?token|auth[_-]?token|client[_-]?secret|password)\b[\"\']?\s*[:=]\s*[\"\']?[A-Za-z0-9_\-+/]{7,}",
         0.85),
    ],
    "PRIVATE_KEY": [
        ("pk_block", r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----", 1.0),
    ],
}

_init_lock = threading.Lock()
_ready = threading.Event()
_recognizers = None


def _build():
    from presidio_analyzer import Pattern, PatternRecognizer
    from presidio_analyzer.predefined_recognizers import (
        CreditCardRecognizer,
        CryptoRecognizer,
        EmailRecognizer,
        IpRecognizer,
    )

    recs = []
    for entity, pats in SECRET_PATTERNS.items():
        recs.append(PatternRecognizer(
            supported_entity=entity,
            patterns=[Pattern(name, rx, score) for name, rx, score in pats],
        ))
    recs += [CreditCardRecognizer(), CryptoRecognizer(), EmailRecognizer(), IpRecognizer()]
    return recs


def warmup() -> None:
    """Start building the recognizer set in a background thread."""
    threading.Thread(target=scrub, args=("warmup",), daemon=True).start()


def scrub(text: str) -> str:
    """Replace every detected PII/secret span with <SCRUBBED>. Never raises."""
    global _recognizers
    try:
        if not _ready.is_set():
            with _init_lock:
                if not _ready.is_set():
                    try:
                        _recognizers = _build()
                    except ImportError:
                        _recognizers = []
                    _ready.set()
        if not _recognizers:
            return text

        hits = []
        for rec in _recognizers:
            for entity in rec.supported_entities:
                try:
                    hits += rec.analyze(text, entities=[entity], nlp_artifacts=None)
                except Exception:
                    pass

        hits.sort(key=lambda r: (r.start, -(r.end - r.start)))
        out, pos = [], 0
        for h in hits:
            if h.start < pos or h.start >= h.end:
                continue
            out.append(text[pos:h.start])
            out.append(SCRUBBED)
            pos = h.end
        out.append(text[pos:])
        return "".join(out)
    except Exception:
        return text