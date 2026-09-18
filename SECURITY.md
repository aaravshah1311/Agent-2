<!--
Author: Aarav Shah
Portfolio: aaravshah1311.is-great.net
github: github.com/aaravshah1311
-->

# 🔒 Security Policy

Security is a fundamental design requirement of **Agent-2**. Because Agent-2 combines powerful autonomous execution, shell interactions, and web interfaces, we enforce strict security boundaries and maintain high standards for vulnerability management.

This document outlines supported versions, security architecture guarantees, and guidelines for reporting security vulnerabilities.

---

## 🛡️ Supported Versions

We actively provide security updates for the following versions of Agent-2:

| Version | Supported | Security Maintenance |
| :--- | :---: | :--- |
| `main` branch (latest release) | ✅ | Active security patches and critical bug fixes |
| $<1.0.0$ pre-release commits | ❌ | Upgrade to the latest `main` commit |

---

## 🔐 Security Posture & Architectural Guarantees

Agent-2 enforces several security boundaries to ensure user data and execution environments remain protected:

### 1. Storage & Privacy
- **Local State Only**: All conversation history, memory, rules, and DAG task state reside locally in `agent2.db`.
- **Zero Telemetry**: Agent-2 transmits no metrics, analytics, or background telemetry to external servers.
- **Separate Master Key**: The secrets master key is stored at `~/.agent2/secret.key` by default—outside the database directory—preventing credential exposure if `agent2.db` is copied.

### 2. Secret & Credential Handling
- **Reference Persistence**: API keys and secrets persist as references (`a2s:`) rather than plaintext in SQLite tables.
- **Encryption Backends**: OS Keyring $\rightarrow$ AES-256-GCM / BLAKE2b+HMAC $\rightarrow$ fallback plaintext (explicitly flagged).
- **Constant-Length Masking**: Secret masking uses standard fixed masks (`••••••••`) rather than partial key leaks (`k[:6]...`).

### 3. Web & API Authentication
- **Access Control**: Web surface requests are validated via `server/auth.py` with `AGENT2_WEB_AUTH` (`auto`, `always`, `off`).
- **Hashed Session Tokens**: Session storage uses SHA-256 digests; raw tokens are never written to disk.
- **Origin Validation**: Unsafe HTTP methods execute Origin checks prior to identity evaluation to guard against cross-site requests.
- **Role Scoping**: Access roles (`owner`, `operator`, `viewer`) restrict capability usage. Unknown roles fall back strictly to `viewer`.

### 4. Transport & Execution Controls
- **Strict TLS Enforcement**: Agent-2 intentionally provides no "skip TLS verification" switch on control channels.
- **Command Watchdog**: Command execution runs through watchdog oversight with configurable ceilings.

---

## 🐛 Reporting a Vulnerability

If you discover a security vulnerability in Agent-2, we appreciate your help in responsibly disclosing it to us.

### How to Report

> [!CAUTION]
> **Do NOT create public GitHub issues or discussions for security vulnerabilities.**

1. **Email Contact**: Send an email to **aaravshah1311@gmail.com** with the details of the vulnerability.
2. **Include Details**:
   - Description of the issue and potential impact
   - Step-by-step reproduction steps or proof-of-concept (PoC)
   - Component affected (CLI, Web UI, Auth, Command Watchdog, MCP Bridge, Secrets, etc.)
   - Environment details (OS, Python version, Agent-2 version/commit)

### Response SLA

We commit to the following timelines for vulnerability reports:

- **Initial Response**: Within **48 hours** acknowledging receipt of your report.
- **Triage & Assessment**: Within **5 business days** to confirm the issue and assess severity.
- **Remediation & Patch**: Critical security fixes are prioritized and released as soon as possible.
- **Public Disclosure**: Coordinated disclosure after a fix has been tested and released.

---

Thank you for keeping Agent-2 and our community secure!
