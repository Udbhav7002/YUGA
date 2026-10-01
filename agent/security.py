"""
Security scanner for generated code.
"""

import re

DANGEROUS_PATTERNS = [
    r'os\.system\s*\(',
    r'os\.popen\s*\(',
    r'subprocess\.',
    r'eval\s*\(',
    r'exec\s*\(',
    r'__import__\s*\(',
    r'socket\.',
    r'requests\.get\s*\(',
    r'requests\.post\s*\(',
    r'urllib\.',
    r'open\s*\([^,]+,\s*[\'"][wa]\+?[\'"]\s*\)',
    r'shutil\.rmtree\s*\(',
    r'sys\.exit\s*\('
]

def scan_for_dangerous_code(code: str) -> tuple[bool, list[str]]:
    """
    Scans the given code for potentially dangerous patterns.
    Returns (is_safe, list_of_violations).
    """
    violations = []
    for pattern in DANGEROUS_PATTERNS:
        matches = re.finditer(pattern, code)
        for match in matches:
            violations.append(f"Matched dangerous pattern: {pattern}")
            
    return len(violations) == 0, violations
