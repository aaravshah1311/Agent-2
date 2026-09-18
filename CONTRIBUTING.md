<!--
Author: Aarav Shah
Portfolio: aaravshah1311.is-great.net
github: github.com/aaravshah1311
-->

# 🤝 Contributing to Agent-2

Thank you for your interest in contributing to **Agent-2**! We welcome contributions, bug reports, feature requests, and documentation updates.

Please take a moment to review this document to understand our workflow and project standards.

---

## 📋 Table of Contents

- [Code of Conduct](#-code-of-conduct)
- [Core Architecture Principles](#-core-architecture-principles)
- [Getting Started](#-getting-started)
  - [Prerequisites](#prerequisites)
  - [Local Setup](#local-setup)
- [Development Workflow](#-development-workflow)
  - [Branch Naming](#branch-naming)
  - [Running the Application](#running-the-application)
- [Testing Standards](#-testing-standards)
  - [Running Tests](#running-tests)
  - [Writing Tests](#writing-tests)
- [Submitting Pull Requests](#-submitting-pull-requests)
- [Reporting Issues](#-reporting-issues)

---

## 📜 Code of Conduct

This project and everyone participating in it is governed by the [Agent-2 Code of Conduct](CODE_OF_CONDUCT.md). By participating, you are expected to uphold this code. Please report unacceptable behavior to **aaravshah1311@gmail.com**.

---

## 🧠 Core Architecture Principles

Before submitting code changes, keep in mind the fundamental rules of the Agent-2 codebase:

1. **Single Source of Truth**: Each fact has *exactly one home*. A second copy will drift silently over time.
   - Models & modes: `config.py`
   - SQLite access: `database.py` (`qall`, `qone`, `exe`, `exemany`, `batch`)
   - Context assembly: `core/broker/`
   - Memory writes: `core/memory/`
   - Capability checks: `llm/capabilities.py`
   - Authorization: `server/auth.py` and `core/permissions.py`
2. **Never swallow exceptions quietly without safe fallbacks**: Tools return tool `error`, never an unhandled exception. The model reads errors and adapts; an uncaught exception breaks turn flow.
3. **Preserve Security Defaults**: Security features, token digests, TLS enforcement, and permission scoping must remain non-bypassable.
4. **No Arbitrary Dependencies**: Standard library is preferred where possible. Runtime dependency updates belong in `run.py`.

---

## 🚀 Getting Started

### Prerequisites

- **Python**: 3.9 or higher
- **Git**: Latest release
- **Gemini API Key**: Free key from [Google AI Studio](https://aistudio.google.com/apikey)

### Local Setup

1. **Fork and clone the repository**:
   ```bash
   git clone https://github.com/aaravshah1311/Agent-2.git
   cd Agent-2
   ```

2. **Initialize environment and dependencies**:
   ```bash
   python run.py
   ```
   `run.py` is both the launcher and dependency installer. It automatically creates a `.venv` directory, installs dependencies, and prompts for your Gemini API key.

3. **Verify installation**:
   ```bash
   python run.py --cli
   ```

---

## 🔄 Development Workflow

### Branch Naming

Create a feature or bugfix branch with a descriptive name:

- `feat/short-description` (for new features)
- `fix/short-description` (for bug fixes)
- `docs/short-description` (for documentation updates)
- `refactor/short-description` (for code refactoring)

```bash
git checkout -b feat/add-new-mcp-bridge
```

### Running the Application

You can test different surfaces locally during development:

```bash
# Terminal CLI interface
python run.py --cli

# Web UI server (default: http://localhost:1311)
python run.py --web

# Dual mode (Web UI on background thread + CLI foreground)
python run.py --dual
```

---

## 🧪 Testing Standards

Agent-2 has an extensive test suite to guarantee correctness and guard against regressions.

### Running Tests

All tests are located in `.github/tests/`. Run the full test suite using `pytest`:

```bash
python -m pytest .github/tests/
```

To run a specific test file:
```bash
python -m pytest .github/tests/test_config.py
```

### Writing Tests

- **Add Tests for Every Change**: Every bug fix should include a test demonstrating that the bug is resolved. Every new feature should have test coverage.
- **Sabotage Verification**: Prove your test fails when the underlying capability or logic is deliberately broken, ensuring assertions are meaningful and not tautological.
- **Isolated State**: Tests must not rely on external services or modify persistent `agent2.db` outside of temporary test fixtures.

---

## 📥 Submitting Pull Requests

1. **Run the full test suite** to ensure all tests pass:
   ```bash
   python -m pytest .github/tests/
   ```
2. **Commit your changes** with clear, descriptive commit messages:
   ```bash
   git commit -m "feat(mcp): add validation for custom server headers"
   ```
3. **Push your branch** to your fork:
   ```bash
   git push origin feat/add-new-mcp-bridge
   ```
4. **Open a Pull Request** against the `main` branch on GitHub.
5. **Fill out the Pull Request Template**: Ensure all checklist items are addressed, including documentation updates and test execution.

---

## 🐛 Reporting Issues

- **Bug Reports**: Use our [Bug Report Template](.github/ISSUE_TEMPLATE/bug_report.yml) to report unexpected behavior. Include surface type (CLI/Web/Dual/Docker), OS, logs, and reproduction steps.
- **Feature Requests**: Use our [Feature Request Template](.github/ISSUE_TEMPLATE/feature_request.yml) to propose new capabilities or enhancements.
- **Security Vulnerabilities**: Do **NOT** file public issues for security vulnerabilities. Follow our [Security Policy](SECURITY.md) for responsible disclosure.

---

Thank you for helping make Agent-2 better! 🎉
