"""Data models and risk-scoring helpers for tool activity tracking."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ToolActivityEntry:
    """A single recorded tool invocation."""

    id: str = ""
    session_id: str = ""
    tool: str = ""
    call_id: str = ""
    category: str = ""  # sdk | custom | mcp | skill
    arguments: str = ""
    result: str = ""
    status: str = ""  # started | completed | denied | error
    timestamp: float = 0.0
    duration_ms: float | None = None
    flagged: bool = False
    flag_reason: str = ""
    risk_score: int = 0  # 0-100 computed risk score
    risk_factors: list[str] = field(default_factory=list)
    model: str = ""  # which LLM model initiated this tool call
    interaction_type: str = ""  # "" | hitl | aitl | pitl | filter | deny
    shield_result: str = ""  # "" | clean | attack | error | not_configured
    shield_detail: str = ""  # human-readable detail from Content Safety API
    shield_elapsed_ms: float | None = None  # round-trip time for the shield call


_SUSPICIOUS_PATTERNS: list[tuple[str, int, str]] = [
    # (pattern, severity 1-100, description)
    ("rm -rf", 90, "Recursive forced deletion"),
    ("rm -r /", 100, "Root filesystem deletion"),
    ("DROP TABLE", 85, "SQL table drop"),
    ("DELETE FROM", 60, "SQL mass deletion"),
    ("curl.*|.*sh", 80, "Remote code execution via curl"),
    ("wget.*|.*sh", 80, "Remote code execution via wget"),
    ("eval(", 75, "Dynamic code evaluation"),
    ("exec(", 75, "Dynamic code execution"),
    ("os.system", 70, "Shell command execution"),
    ("subprocess", 50, "Subprocess invocation"),
    ("chmod 777", 65, "World-writable permissions"),
    ("passwd", 55, "Password file access"),
    ("/etc/shadow", 90, "Shadow password file access"),
    ("env | grep", 45, "Environment variable enumeration"),
    ("printenv", 45, "Environment variable dump"),
    ("base64 -d", 60, "Base64 decode (potential obfuscation)"),
    (".ssh/", 70, "SSH directory access"),
    ("id_rsa", 85, "SSH private key access"),
    ("PRIVATE KEY", 95, "Private key exposure"),
    ("API_KEY", 50, "API key in arguments"),
    ("SECRET", 55, "Secret value in arguments"),
    ("TOKEN", 45, "Token value in arguments"),
    ("password", 50, "Password in arguments"),
    ("credentials", 55, "Credentials reference"),
    ("sudo ", 60, "Privilege escalation"),
    ("nc -l", 70, "Netcat listener (reverse shell)"),
    (">&/dev/tcp", 90, "Bash reverse shell"),
    ("/dev/tcp", 85, "Network device access"),
    ("mkfifo", 65, "Named pipe creation"),
    ("nmap", 55, "Network scanning"),
    ("sqlmap", 80, "SQL injection tool"),
    (".env", 40, "Environment file access"),
    ("aws configure", 50, "Cloud credential configuration"),
    ("gcloud auth", 50, "Cloud credential configuration"),
    ("az login", 40, "Azure CLI login"),
    ("docker run", 45, "Container execution"),
    ("kubectl exec", 55, "Kubernetes pod execution"),
]


def check_suspicious(arguments: str, result: str) -> tuple[bool, str, int, list[str]]:
    """Check if a tool call looks suspicious based on arguments/result.

    Returns (flagged, primary_reason, risk_score, risk_factors).
    """
    text = f"{arguments} {result}".lower()
    factors: list[str] = []
    max_severity = 0
    primary_reason = ""
    for pattern, severity, description in _SUSPICIOUS_PATTERNS:
        if pattern.lower() in text:
            factors.append(description)
            if severity > max_severity:
                max_severity = severity
                primary_reason = f"Suspicious pattern: {pattern}"
    flagged = max_severity >= 40
    return flagged, primary_reason, max_severity, factors
