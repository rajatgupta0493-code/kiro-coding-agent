# ChangeSpec Definition

A **ChangeSpec** (Change Specification) is an LLM-executable work unit - a decomposed fragment of a larger task designed for reliable execution by language models.

## Four Required Qualities

### Specificity
Clear, unambiguous instructions to get the right outcome. Use semantic descriptions of code locations (e.g., "in the authentication handler", "where user validation occurs") rather than line numbers or counts.

### Context Constraint
Fits in LLM's high-performance context depth zone (<50% context). Generally work on one or just a few files at a time. When working on many files, prefer uniform or related changes that must be made together over mixing complex changes with many simple ones. Don't artificially split single-file modifications to reduce context - stability takes priority over context size.

### Containment
Self-contained with enough context that an agent seeing ONLY this ChangeSpec understands both their specific work boundaries AND the broader goal. They should know what NOT to do because other ChangeSpecs handle those parts.

### Stability
The result must maintain full production readiness - builds successfully, passes all unit tests, code quality checks (checkstyle, spotbugs, linting), and can be deployed to production without cutting corners. No regressions or broken functionality introduced while making forward progress. You can't just piecemeal compile and call it stable.

## Usage in Planning Tools

ChangeSpecs are the output of lisamarge.py's planning process. They form the steps in PLAN_DRAFT files that homerbart.py executes sequentially. Each step is a ChangeSpec that can be reliably implemented by an LLM worker agent.

## Example

**Problem**: "Add input validation to UserService"

**ChangeSpecs (Plan Steps)**:
1. "Add email validation to UserService.createUser() method - validate format and domain, throw ValidationException on invalid input"
2. "Add username validation to UserService.createUser() method - check length (3-20 chars), alphanumeric only, throw ValidationException on invalid input"
3. "Add age validation to UserService.createUser() method - verify 18+, throw ValidationException if under age"

Each ChangeSpec is specific (clear validation rules), fits in context (single method, single concern), self-contained (agent knows their validation boundary), and stable (all tests pass, code quality checks pass, production-ready).
