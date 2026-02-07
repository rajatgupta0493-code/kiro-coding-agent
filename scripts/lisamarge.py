#!/usr/bin/env python3
"""LisaMarge: LLM-Optimized Plan Generator

Give it a problem, get back a plan with steps that LLMs can reliably execute.

What It Does:
    Takes your problem statement and generates an implementation plan through an
    iterative optimization cycle between two LLM agents (planner and reviewer).
    The output is a sequence of ChangeSpecs - concrete, LLM-executable steps that
    can be reliably processed by language models.

Why Use This:
    - Converts vague requirements into precise, actionable steps
    - Each step qualifies as a ChangeSpec (specific, testable, self-contained)
    - Iterative refinement ensures plan quality before execution
    - Catches ambiguities and missing requirements early
    - Produces plans that LLMs can execute without getting stuck

How It Works:
    1. Planner agent creates initial plan from your problem statement
    2. Reviewer agent evaluates plan quality and provides feedback
    3. Cycle repeats until plan meets quality criteria
    4. Approved plan remains in PLAN_DRAFT_<name>.md (ready for homerbart.py)

Generated Files:
    - PLAN_DRAFT_<name>.md: The actual implementation plan (use this with homerbart.py)
    - PLAN_STUCK_<name>.md: Questions when requirements unclear (planner blocked)
    - PLAN_REVIEW_<name>.md: Review feedback for plan revisions
    - PLAN_FINAL_<name>.md: Reviewer's approval assessment (not the plan itself)
    
    These files form a state machine enabling resumable planning sessions.

Exit Codes:
    0: Success (plan approved, PLAN_DRAFT ready for execution)
    1: Max iterations exceeded
    2: Error (validation, file system, kiro-cli issues)
    3: Planner blocked (insufficient requirements - needs more info)

Example:
    Basic usage:
        $ ./lisamarge.py \\
            --kiro-cli-path /usr/local/bin/kiro-cli \\
            --problem-statement "Add user authentication" \\
            --plan-name auth_feature

    From file:
        $ ./lisamarge.py \\
            --kiro-cli-path /usr/local/bin/kiro-cli \\
            --problem-statement-file problem.txt \\
            --plan-name feature_123

Implementation Details:
    - File-based state machine for resumable sessions
    - Fixed 2-second retry delay for transient failures
    - Configurable iteration limits and timeouts
    - JSON execution summaries for automation
    - All invocations use the Ralph Loop
"""

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass, asdict
from datetime import datetime

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Default trust-tools configuration
DEFAULT_TRUST_TOOLS = "read,write,@builder-mcp/glob,@builder-mcp/grep,@builder-mcp"


# Custom exception classes
class PlanningError(Exception):
    """Base exception for planning workflow failures.
    
    All planning-related exceptions inherit from this base class,
    enabling catch-all error handling for planning operations.
    """
    pass


class KiroCliError(PlanningError):
    """Kiro-cli invocation failures.
    
    Raised when kiro-cli subprocess fails, times out, or returns
    non-zero exit code. Includes stderr output in exception message.
    """
    pass


class StateFileError(PlanningError):
    """State file I/O failures.
    
    Raised when reading or writing plan state files fails due to
    I/O errors, permission issues, or file system problems.
    """
    pass


class PlannerBlockedError(PlanningError):
    """Planner reports insufficient requirements.
    
    Raised when planner agent determines problem statement lacks
    sufficient detail to create a plan. Requires user intervention
    to provide additional context or clarification.
    """
    pass


@dataclass
class ExecutionSummary:
    """Summary of planning orchestration execution.
    
    Tracks execution metadata including timing, agent invocation counts, final state,
    and outcome. Provides formatted output and JSON serialization for logging
    and CI/CD integration.
    
    Attributes:
        start_time (str): ISO 8601 timestamp of execution start
        end_time (str): ISO 8601 timestamp of execution completion (None if in progress)
        planner_invocations (int): Total times planner agent was called
        reviewer_invocations (int): Total times reviewer agent was called
        revision_cycles (int): Number of times planner revised based on reviewer feedback
        final_state (str): Last detected state (initial/draft_ready/review_ready/done)
        outcome (str): Execution result (success/failure/blocked/max_iterations)
    
    Example:
        >>> summary = ExecutionSummary(
        ...     start_time="2026-01-23T12:00:00",
        ...     end_time="2026-01-23T12:15:00",
        ...     planner_invocations=2,
        ...     reviewer_invocations=1,
        ...     revision_cycles=0,
        ...     final_state="done",
        ...     outcome="success"
        ... )
        >>> print(summary)
        === Execution Summary ===
        Start Time: 2026-01-23T12:00:00
        End Time: 2026-01-23T12:15:00
        Duration: 0:15:00
        Planner Invocations: 2
        Reviewer Invocations: 1
        Revision Cycles: 0
        Final State: done
        Outcome: success
        ========================
    """
    start_time: str
    end_time: str = None
    planner_invocations: int = 0
    reviewer_invocations: int = 0
    revision_cycles: int = 0
    final_state: str = None
    outcome: str = None  # success/failure/blocked/max_iterations
    
    def to_dict(self) -> dict:
        """Return dictionary representation for JSON serialization.
        
        Returns:
            dict: All attributes as key-value pairs
        """
        return asdict(self)
    
    def __str__(self) -> str:
        """Return formatted summary string for console output.
        
        Calculates duration from start_time and end_time if both are present.
        
        Returns:
            str: Multi-line formatted summary with box drawing
        """
        duration = "N/A"
        if self.start_time and self.end_time:
            start = datetime.fromisoformat(self.start_time)
            end = datetime.fromisoformat(self.end_time)
            duration = str(end - start)
        
        return f"""
=== Execution Summary ===
Start Time: {self.start_time}
End Time: {self.end_time}
Duration: {duration}
Planner Invocations: {self.planner_invocations}
Reviewer Invocations: {self.reviewer_invocations}
Revision Cycles: {self.revision_cycles}
Final State: {self.final_state}
Outcome: {self.outcome}
========================
"""

def build_planner_prompt(plan_name: str, problem_statement: str) -> str:
    """Build planner prompt for plan decomposition."""
    if not problem_statement:
        problem_statement = "(No problem statement provided)"
    
    return f"""You are a planning specialist. Your task is to create a decomposition plan for the following problem.

**IMPORTANT: YOU ARE IN PLANNING MODE ONLY**
- DO NOT make any code changes or modifications
- DO NOT create, edit, or delete files
- DO NOT run build commands or tests
- DO NOT use shell commands, bash, execute_bash, or any command execution tools
- DO NOT use pwd, cd, ls, or any shell utilities - use read-only file tools instead
- Your role is ANALYSIS and PLANNING only
- Use ONLY read-only tools: fs_read, glob, grep, code search
- Use read-only tools to examine the codebase and create a decomposition plan

**INSUFFICIENT INFORMATION PATHWAY:**
If the problem statement lacks sufficient detail to create a concrete plan, you should:
1. Write your questions to `PLAN_STUCK_{plan_name}.md`
2. Start the file with: "⚠️ DELETE THIS FILE after reading and updating the problem statement ⚠️"
3. Format as a clear list of questions that need answers
4. Explain what information is missing and why it's needed for planning
5. DO NOT create PLAN_DRAFT - only create PLAN_STUCK when blocked

The user will read PLAN_STUCK, provide answers, fold them into the problem statement, DELETE the file, and restart.

**PROBLEM STATEMENT:**
{problem_statement}

**CHECK FOR EXISTING DRAFT:**
Before creating your plan, check if PLAN_DRAFT_{plan_name}.md exists. If it does, read it and use it as your starting point for revision.

**CHECK FOR REVIEW FEEDBACK:**
Also check if PLAN_REVIEW_{plan_name}.md exists. If it does, read it and incorporate the feedback into your revised plan.

**WORKFLOW:**
- If PLAN_DRAFT exists and PLAN_REVIEW exists: Revise the draft based on review feedback
- If only PLAN_DRAFT exists: Review and potentially refine the existing draft
- If neither exists: Create a fresh plan from scratch

**RESEARCH PHASE:**
Before decomposing the problem, examine the codebase to understand:
- File structure and organization
- Key classes, functions, and their relationships
- Existing patterns and conventions
- Dependencies between components
- Test structure and coverage
- Build/quality check requirements

Use code search, file reading, and symbol navigation tools to map out the relevant parts of the codebase that will be affected by this work.

**STEP QUALITY CRITERIA:**
Each step must meet these four criteria:

1. **Specificity**: Clear, unambiguous instructions to get the right outcome. Use semantic descriptions of code locations (e.g., "in the authentication handler", "where user validation occurs") rather than line numbers or counts.

2. **Context Constraint**: Fits in LLM's high-performance context depth zone (<50% context). Generally work on one or just a few files at a time. When working on many files, prefer uniform or related changes that must be made together over mixing complex changes with many simple ones. Don't artificially split single-file modifications just to reduce context - stability takes priority over context size.

3. **Containment**: Self-contained with enough context that an agent seeing ONLY this step understands both their specific work boundaries AND the broader goal. They should know what NOT to do because other steps handle those parts.

4. **Stability**: The result must maintain full production readiness - builds successfully, passes all unit tests, code quality checks (checkstyle, spotbugs, linting), and can be deployed to production without cutting corners. No regressions or broken functionality introduced while making forward progress. You can't just piecemeal compile and call it stable.

**ALGORITHM VALIDATION (Detailed)**: For steps involving non-trivial algorithms or data structure operations:

1. **Correctness Proof**: Explain why the algorithm produces correct results
   - Loop invariants (what remains true each iteration)
   - Preconditions and postconditions
   - Termination guarantees (why loops/recursion end)

2. **Complexity Analysis**: Provide precise time/space complexity
   - Best, average, and worst-case scenarios
   - Space complexity including auxiliary structures
   - Justify complexity claims with reasoning

3. **Edge Case Enumeration**: Comprehensive boundary condition handling
   - Empty inputs (null, empty collections, zero values)
   - Single-element cases
   - Maximum size limits (integer overflow, memory limits)
   - Concurrent access patterns (if applicable)
   - Invalid inputs and error conditions

4. **Data Structure Invariants**: For custom data structures
   - What properties must always hold?
   - How are invariants maintained during operations?
   - What happens if invariants are violated?

5. **Algorithm Alternatives**: Document why this specific approach
   - What simpler algorithms were considered?
   - What are the tradeoffs? (time vs space, simplicity vs performance)
   - When would alternative approaches be better?

**Note**: This detailed validation is required at the planning stage because steps must be specific enough for implementation.

**COMPLEXITY GUIDELINE:**
Target steps that a competent SDE1 can implement confidently with clear instructions. Occasionally, SDE2-level complexity may be necessary when tightly coupled systems must be modified together to maintain stability, but this should be the exception rather than the rule.

**AVOID:**
- Line numbers or positional references ("lines 45-67", "first five methods")
- Count-based decomposition ("convert the next three classes")
- Vague boundaries that might cause scope creep
- Mixing complex changes with many simple changes in one step

**OUTPUT FORMAT:**
Each step MUST use this exact markdown structure with ALL fields present:

```markdown
---STEP_BLOCK---
### Step N: Brief Title

**Description**: 
[Detailed, specific instructions with semantic code locations. This should be comprehensive enough that an LLM can execute without guessing. Include what to modify, where to find it semantically, and what the changes should accomplish.]

**Specificity Criteria**: 
[Explain how this step meets the Specificity criterion - what makes the instructions clear and unambiguous? What semantic code locations are provided?]

**Context Constraint Criteria**: 
[Explain how this step meets the Context Constraint criterion - what files/components are in scope? Why is this a manageable context size?]

**Containment Criteria**: 
[Explain how this step meets the Containment criterion - what is the broader goal? What are the explicit boundaries? What should NOT be done because other steps handle it?]

**Stability Criteria**: 
[Explain how this step meets the Stability criterion - what quality checks must pass? What tests must succeed? How is production readiness maintained?]

**Success Criteria**: 
[Concrete, verifiable conditions for completion. Include specific build commands, test commands, and quality checks that must pass.]

**Dependencies**: 
[List which steps must complete first, or state "None" if this can be done independently]
---END_STEP_BLOCK---
```

**REQUIRED: All Fields Must Be Present**

Every step MUST include ALL fields listed above. Each field serves a specific purpose:
- **Description**: The work to be done
- **Specificity/Context Constraint/Containment/Stability Criteria**: Explicit demonstration that the four quality criteria are met
- **Success Criteria**: How to verify completion
- **Dependencies**: Execution ordering

Missing any field indicates incomplete planning and will require revision.

**PLAN STORAGE:**
When your decomposition is complete, save the full plan to a file named `PLAN_DRAFT_{plan_name}.md` in the working directory.

The plan file should contain your complete analysis and step breakdown in markdown format for easy review and reference."""


def build_plan_reviewer_prompt(plan_name: str, problem_statement: str) -> str:
    """Build plan reviewer prompt for plan quality review."""
    if not problem_statement:
        problem_statement = "(No problem statement provided)"
    
    return f"""You are a plan quality reviewer. Your job is to evaluate a decomposition plan against strict criteria and provide actionable feedback.

**IMPORTANT: YOU ARE IN REVIEW MODE ONLY**
- DO NOT make any code changes or modifications
- DO NOT create, edit, or delete files (except PLAN_FINAL or PLAN_REVIEW)
- DO NOT run build commands or tests
- DO NOT use shell commands, bash, execute_bash, or any command execution tools
- DO NOT use pwd, cd, ls, or any shell utilities - use read-only file tools instead
- Your role is EVALUATION and FEEDBACK only
- Use ONLY read-only tools: fs_read, glob, grep, code search
- Use read-only tools to verify plan quality

**PLAN LOCATION:**
Read the decomposition plan from `PLAN_DRAFT_{plan_name}.md` in the working directory.

**ORIGINAL PROBLEM:**
{problem_statement}

**CODEBASE RESEARCH REVIEW:**
First, verify that the decomposition shows evidence of proper codebase research:
- Does it reference actual file structures and class names?
- Are the semantic code locations accurate and specific?
- Does it account for real dependencies and relationships?
- Are the quality check requirements appropriate for this codebase?

**EVALUATION CRITERIA:**
Review each step for these requirements:

**Markdown Block Format Validation:**
Each step MUST have a corresponding machine-readable block with ALL required fields:
- Block delimiters present: `---STEP_BLOCK---` and `---END_STEP_BLOCK---`
- Markdown is valid and parseable (no syntax errors)
- ALL fields present: Description, Specificity Criteria, Context Constraint Criteria, Containment Criteria, Stability Criteria, Success Criteria, Dependencies
- Each criteria field explicitly demonstrates how that quality criterion is met

**Step Quality Criteria Validation:**

1. **Specificity**: Are instructions semantically clear? No line numbers or count-based references ("first five methods")? Will an LLM know exactly where to work in the codebase? Are the instructions specific enough for a competent SDE1 to implement without guessing implementation details? Does the "Specificity Criteria" field explicitly explain what makes this specific?

2. **Context Constraint**: Can this fit in <50% of LLM context? Does it generally work on one or just a few files at a time? When working on many files, are they uniform or related changes that must be made together rather than mixing complex + simple changes? Is the step avoiding artificial splits that would sacrifice stability? Does the "Context Constraint Criteria" field explicitly explain the scope boundaries?

**COMPLEXITY CHECK**: Is this appropriate for a competent SDE1 to implement, or does it naturally require SDE2-level complexity due to tightly coupled systems that must be modified together? When complexity is unavoidable, are the tightly coupled changes kept together for stability?

3. **Containment**: Does the step provide enough context about the broader goal AND clear boundaries so an agent won't work outside their assigned scope? Would someone seeing only this step understand what they should NOT do? Does the "Containment Criteria" field explicitly explain the broader context and boundaries?

4. **Stability**: Will the result maintain full production readiness? Builds, unit tests, code quality, and deployment-ready without regressions or shortcuts? Does the "Stability Criteria" field explicitly list the quality checks that must pass?

**ALGORITHM VALIDATION CHECK**: For steps involving non-trivial algorithms:
- **Correctness**: Is there reasoning about why the algorithm works? (loop invariants, preconditions, postconditions, termination)
- **Complexity**: Are time/space complexity bounds provided with justification?
- **Edge Cases**: Are boundary conditions comprehensively enumerated? (empty inputs, single elements, max sizes, concurrency, invalid inputs)
- **Data Structure Invariants**: For custom structures, are invariants documented and maintenance explained?
- **Alternatives**: Are simpler approaches considered with tradeoff analysis?

**Note**: Algorithm validation should be detailed at planning stage.

**RED FLAGS:**
- Missing or invalid step markdown blocks
- Missing ANY required fields (Description, Specificity/Context Constraint/Containment/Stability Criteria, Success Criteria, Dependencies)
- Criteria fields that don't explicitly demonstrate how the quality criterion is met
- Line numbers or positional references
- Count-based work division
- Unclear scope boundaries that could cause overlap
- Missing context about broader goals
- Vague quality requirements
- Mixing complex changes with many simple changes
- Splitting tightly coupled changes that should stay together
- Tests without appropriate timeouts that could hang indefinitely
- **Algorithm issues**: Missing correctness reasoning, vague complexity claims, incomplete edge case coverage, undocumented invariants

**OUTPUT FORMAT:**
- **Overall Assessment**: APPROVED / NEEDS REVISION
- **Markdown Block Issues**: Any missing or invalid Markdown blocks
- **Specific Issues**: List problems with step IDs/titles
- **Boundary Problems**: Any unclear or overlapping scopes
- **Context Issues**: Missing broader goal context or containment problems
- **Quality Concerns**: Stability or specificity issues
- **Recommendations**: Concrete suggestions for improvement

**PLAN APPROVAL:**
If the plan is APPROVED:
1. Write your approval assessment to `PLAN_FINAL_{plan_name}.md` with "APPROVED" at the top and a comprehensive summary of why the plan meets all quality criteria
2. **CELEBRATE!** Print a big happy announcement that the plan is complete and ready for implementation.

**PLAN NEEDS REVISION:**
If the plan NEEDS REVISION, write your detailed feedback to `PLAN_REVIEW_{plan_name}.md` with specific issues found and recommendations for improvement."""


def detect_state(plan_name: str) -> str:
    """Detect current planning state by checking file existence.
    
    Implements state machine detection logic by checking for plan files
    in priority order. Higher priority states take precedence when multiple
    files exist (e.g., if both PLAN_DRAFT and PLAN_FINAL exist, returns "done").
    
    Priority order (highest to lowest):
        1. PLAN_FINAL_<plan-name>.md exists → "done"
        2. PLAN_STUCK_<plan-name>.md exists → "stuck"
        3. PLAN_REVIEW_<plan-name>.md exists → "review_ready"
        4. PLAN_DRAFT_<plan-name>.md exists → "draft_ready"
        5. No files → "initial"
    
    Args:
        plan_name (str): Identifier for planning session (alphanumeric + underscore)
        
    Returns:
        str: State string ("initial", "draft_ready", "review_ready", "stuck", or "done")
        
    Example:
        >>> detect_state("auth_feature")
        'initial'
        >>> # After planner creates draft
        >>> detect_state("auth_feature")
        'draft_ready'
    """
    if os.path.exists(f"PLAN_FINAL_{plan_name}.md"):
        logging.info(f"State detection: PLAN_FINAL_{plan_name}.md exists → done")
        return "done"
    if os.path.exists(f"PLAN_STUCK_{plan_name}.md"):
        logging.info(f"State detection: PLAN_STUCK_{plan_name}.md exists → stuck")
        return "stuck"
    if os.path.exists(f"PLAN_REVIEW_{plan_name}.md"):
        logging.info(f"State detection: PLAN_REVIEW_{plan_name}.md exists → review_ready")
        return "review_ready"
    if os.path.exists(f"PLAN_DRAFT_{plan_name}.md"):
        logging.info(f"State detection: PLAN_DRAFT_{plan_name}.md exists → draft_ready")
        return "draft_ready"
    logging.info(f"State detection: No plan files found → initial")
    return "initial"


def invoke_kiro_cli(kiro_cli_path: str, prompt: str, role: str, attempt: int, timeout: int = 600, agent: str = None, trust_tools: str = None, trust_all_tools: bool = False, is_final_attempt: bool = False, intervene_on_final_retry: bool = False) -> tuple[bool, str, str]:
    """Invoke kiro-cli with the given prompt.
    
    Executes kiro-cli as subprocess with timeout protection. Detects planner
    blocking conditions by scanning stdout for specific markers.
    
    Args:
        kiro_cli_path (str): Path to kiro-cli executable
        prompt (str): Prompt to send to kiro-cli
        role (str): Role being invoked ("planner" or "reviewer")
        attempt (int): Attempt number for this invocation (for logging)
        timeout (int): Timeout in seconds (default: 600 = 10 minutes)
        agent (str): Optional name of kiro-cli agent to use
        trust_tools (str): Comma-separated list of trusted tools (default: DEFAULT_TRUST_TOOLS)
        trust_all_tools (bool): Trust all tools without confirmation (default: False)
        is_final_attempt (bool): Whether this is the final retry attempt (default: False)
        intervene_on_final_retry (bool): Whether to enable interactive mode on final attempt (default: False)
        
    Returns:
        tuple[bool, str, str]: (success, stdout, stderr)
        
    Raises:
        KiroCliError: On subprocess failures or non-zero exit code
        PlannerBlockedError: If planner reports insufficient requirements
        
    Example:
        >>> success, stdout, stderr = invoke_kiro_cli(
        ...     "/usr/local/bin/kiro-cli",
        ...     "Create a plan for...",
        ...     "planner",
        ...     1
        ... )
    """
    if trust_tools is None:
        trust_tools = DEFAULT_TRUST_TOOLS
    
    logging.info(f"Invoking kiro-cli: role={role}, attempt={attempt}, timeout={timeout}s, agent={agent}, trust_tools={trust_tools}, trust_all_tools={trust_all_tools}")
    try:
        cmd = [kiro_cli_path, "chat"]
        if not (is_final_attempt and intervene_on_final_retry):
            cmd.append("--no-interactive")
        
        if trust_all_tools:
            cmd.append("--trust-all-tools")
        elif trust_tools:
            cmd.extend(["--trust-tools", trust_tools])
        
        if agent:
            cmd.extend(["--agent", agent])
        
        cmd.append(prompt)
        
        result = subprocess.run(
            cmd,
            text=True,
            timeout=timeout
        )
        if result.returncode != 0:
            raise KiroCliError(f"kiro-cli failed with exit code {result.returncode}")
        
        # Note: With capture_output removed, we can't detect planner blocking from stdout
        # The output streams directly to terminal for visibility
        
        logging.info(f"kiro-cli completed: role={role}, attempt={attempt}, success=True")
        return (True, "", "")
    except subprocess.TimeoutExpired:
        logging.error(f"kiro-cli timeout: role={role}, attempt={attempt}, timeout={timeout}s")
        raise KiroCliError(f"Timeout after {timeout} seconds")


def retry_with_backoff(func: callable, max_retries: int, on_attempt: callable = None, intervene_on_final_retry: bool = False, *args, **kwargs) -> tuple[bool, any]:
    """Retry a function on failure.
    
    Implements retry strategy with fixed 2-second delay:
    - Retries with 2-second delay between attempts
    - Continues until success or max_retries exhausted
    
    Args:
        func (callable): Function to retry
        max_retries (int): Maximum number of retry attempts
        on_attempt (callable): Optional callback called before each attempt with attempt number
        intervene_on_final_retry (bool): Whether to enable interactive mode on final attempt (default: False)
        *args: Positional arguments to pass to func
        **kwargs: Keyword arguments to pass to func
        
    Returns:
        tuple[bool, any]: (success, result)
            - On success: (True, func_result)
            - On failure: (False, last_error_message)
            
    Example:
        >>> def flaky_operation():
        ...     # May fail transiently
        ...     return "success"
        >>> success, result = retry_with_backoff(flaky_operation, 3)
    """
    for attempt in range(1, max_retries + 1):
        if on_attempt:
            on_attempt(attempt)
        try:
            result = func(*args, is_final_attempt=(attempt == max_retries), intervene_on_final_retry=intervene_on_final_retry, **kwargs)
            return (True, result)
        except Exception as e:
            if attempt < max_retries:
                logging.warning(f"Attempt {attempt}/{max_retries} failed: {e}. Retrying in 2s...")
                time.sleep(2)
            else:
                logging.error(f"All {max_retries} retries exhausted. Last error: {e}")
                return (False, str(e))


def build_prompt(
    role: str,
    plan_name: str,
    problem_statement: str = None,
    review_feedback: str = None
) -> str:
    """Build prompt for planner or reviewer role.
    
    Constructs appropriate prompt based on role and context.
    
    Args:
        role (str): "planner" or "reviewer"
        plan_name (str): Planning session identifier
        problem_statement (str): Initial problem (for planner and reviewer)
        review_feedback (str): Feedback from reviewer (for planner revision, optional)
        
    Returns:
        str: Complete prompt string ready for kiro-cli
        
    Raises:
        ValueError: If role is not "planner" or "reviewer"
        
    Example:
        >>> prompt = build_prompt(
        ...     "planner",
        ...     "auth_feature",
        ...     problem_statement="Add user authentication"
        ... )
    """
    if role == "planner":
        return build_planner_prompt(plan_name=plan_name, problem_statement=problem_statement)
    elif role == "reviewer":
        return build_plan_reviewer_prompt(plan_name=plan_name, problem_statement=problem_statement)
    else:
        raise ValueError(f"Unknown role: {role}")


def parse_args():
    """Parse command line arguments.
    
    Defines CLI interface with required and optional parameters. Enforces
    mutual exclusivity between --problem-statement and --problem-statement-file.
    
    Returns:
        argparse.Namespace: Parsed arguments with attributes:
            - kiro_cli_path (str): Path to kiro-cli executable
            - problem_statement (str): Problem text (if provided)
            - problem_statement_file (str): Problem file path (if provided)
            - plan_name (str): Planning session identifier
            - max_agent_invocations (int): Maximum total agent invocations
            - max_retries (int): Retry limit per invocation
            - agent (str): Name of kiro-cli agent to use (optional)
            - trust_tools (str): Comma-separated list of trusted tools (optional)
            - trust_all_tools (bool): Trust all tools without confirmation (optional)
            
    Example:
        >>> args = parse_args()
        >>> print(args.plan_name)
        'auth_feature'
    """
    parser = argparse.ArgumentParser(
        description="Orchestrate iterative planning workflow with planner and reviewer agents"
    )
    
    parser.add_argument(
        "--kiro-cli-path",
        required=True,
        help="Path to kiro-cli executable"
    )
    
    problem_group = parser.add_mutually_exclusive_group(required=True)
    problem_group.add_argument(
        "--problem-statement",
        help="Problem or feature to plan (as text)"
    )
    problem_group.add_argument(
        "--problem-statement-file",
        help="Read problem statement from file"
    )
    
    parser.add_argument(
        "--plan-name",
        required=True,
        help="Identifier for planning session"
    )
    
    parser.add_argument(
        "--max-agent-invocations",
        type=int,
        default=10,
        help="Maximum total agent invocations (planner + reviewer calls, default: 10)"
    )
    
    parser.add_argument(
        "--max-retries",
        type=int,
        default=3,
        help="Retry limit per invocation (default: 3)"
    )
    
    parser.add_argument(
        "--timeout",
        type=int,
        default=600,
        help="Timeout per kiro-cli invocation in seconds (default: 600)"
    )
    
    parser.add_argument(
        "--agent",
        help="Name of kiro-cli agent to use (optional, defaults to default agent)"
    )
    
    trust_group = parser.add_mutually_exclusive_group(required=False)
    
    trust_group.add_argument(
        "--trust-tools",
        default=DEFAULT_TRUST_TOOLS,
        help=f"Comma-separated list of trusted tools for kiro-cli (default: {DEFAULT_TRUST_TOOLS})"
    )
    
    trust_group.add_argument(
        "--trust-all-tools",
        action="store_true",
        help="Trust all tools without confirmation (equivalent to kiro-cli -a)"
    )
    
    parser.add_argument(
        "--intervene-on-final-retry",
        action="store_true",
        default=False,
        help="Enable interactive mode on final retry attempt for debugging"
    )
    
    return parser.parse_args()


def validate_inputs(args: argparse.Namespace) -> None:
    """Validate input arguments before starting orchestration.
    
    Performs comprehensive validation of all inputs to fail fast with clear
    error messages before starting potentially long-running orchestration.
    
    Validation checks:
        - kiro-cli path exists and is executable
        - Problem statement file exists (if provided)
        - Problem statement is non-empty
        - Plan name is alphanumeric with underscores only
    
    Args:
        args (argparse.Namespace): Parsed command line arguments
        
    Raises:
        ValueError: If validation fails with descriptive message
        
    Example:
        >>> args = parse_args()
        >>> validate_inputs(args)  # Raises ValueError if invalid
    """
    # Check kiro-cli path exists and is executable
    if not os.path.exists(args.kiro_cli_path):
        raise ValueError(f"kiro-cli path does not exist: {args.kiro_cli_path}")
    if not os.access(args.kiro_cli_path, os.X_OK):
        raise ValueError(f"kiro-cli path is not executable: {args.kiro_cli_path}")
    
    # Check problem statement file exists if provided
    if args.problem_statement_file and not os.path.exists(args.problem_statement_file):
        raise ValueError(f"Problem statement file does not exist: {args.problem_statement_file}")
    
    # Check problem statement is non-empty
    if args.problem_statement and not args.problem_statement.strip():
        raise ValueError("Problem statement cannot be empty")
    
    # Check plan name is valid (alphanumeric + underscore only)
    if not args.plan_name.replace('_', '').isalnum():
        raise ValueError(f"Plan name must be alphanumeric with underscores only: {args.plan_name}")


def orchestrate_planning(args: argparse.Namespace) -> int:
    """Orchestrate iterative planning workflow with planner and reviewer agents.
    
    Main orchestration loop that coordinates planner and reviewer agents through
    state machine transitions. Handles retry logic, error recovery, and execution
    summary generation.
    
    State Machine Flow:
        1. initial → invoke planner → draft_ready or stuck
        2. draft_ready → invoke reviewer → review_ready or done
        3. review_ready → invoke planner with feedback → draft_ready
        4. stuck → exit with blocked status (user must update problem statement)
        5. done → exit with success
    
    Args:
        args (argparse.Namespace): Parsed command line arguments containing:
            - kiro_cli_path: Path to kiro-cli executable
            - problem_statement or problem_statement_file: Problem to plan
            - plan_name: Planning session identifier
            - max_agent_invocations: Maximum total agent invocations
            - max_retries: Retry limit per invocation
        
    Returns:
        int: Exit code
            - 0: Success (PLAN_FINAL created)
            - 1: Max iterations exceeded
            - 2: Error (KiroCliError, StateFileError)
            - 3: Planner blocked (insufficient requirements)
            
    Raises:
        StateFileError: If reading/writing state files fails
        
    Example:
        >>> args = parse_args()
        >>> exit_code = orchestrate_planning(args)
        >>> sys.exit(exit_code)
    """
    # Create execution summary
    summary = ExecutionSummary(
        start_time=datetime.now().isoformat(),
        planner_invocations=0,
        reviewer_invocations=0,
        revision_cycles=0,
        final_state=None,
        outcome=None
    )
    
    # Read problem statement
    try:
        if args.problem_statement:
            problem_statement = args.problem_statement
            logging.info("Using problem statement from command line")
        else:
            logging.info(f"Reading problem statement from file: {args.problem_statement_file}")
            with open(args.problem_statement_file, 'r') as f:
                problem_statement = f.read()
        logging.info(f"Problem statement length: {len(problem_statement)} characters")
        logging.info(f"Problem statement preview: {problem_statement[:100]}...")
    except (IOError, OSError) as e:
        raise StateFileError(f"Failed to read problem statement: {e}")
    
    planner_invocations = 0
    reviewer_invocations = 0
    revision_cycles = 0
    
    while planner_invocations + reviewer_invocations < args.max_agent_invocations:
        # Check for PLAN_FINAL first to handle approval on final iteration
        if os.path.exists(f"PLAN_FINAL_{args.plan_name}.md"):
            state = "done"
        else:
            state = detect_state(args.plan_name)
        logging.info(f"=== State: {state} | Planner: {planner_invocations} | Reviewer: {reviewer_invocations} | Cycles: {revision_cycles} ===")
        summary.final_state = state
        
        if state == "done":
            logging.info("Planning complete: PLAN_FINAL exists")
            summary.outcome = "success"
            summary.end_time = datetime.now().isoformat()
            
            # Print and write summary
            print(summary)
            summary_file = f"PLAN_SUMMARY_{args.plan_name}.json"
            logging.info(f"Writing execution summary to {summary_file}")
            try:
                with open(summary_file, 'w') as f:
                    json.dump(summary.to_dict(), f, indent=2)
            except (IOError, OSError) as e:
                raise StateFileError(f"Failed to write summary file: {e}")
            
            # Celebration announcement
            print(f"\n{'='*70}")
            print(f"🎉  PLAN APPROVED AND FINALIZED!")
            print(f"{'='*70}")
            print(f"\nYour plan is complete and ready for implementation!")
            print(f"\n📋 Files created:")
            print(f"   • PLAN_DRAFT_{args.plan_name}.md (the plan)")
            print(f"   • PLAN_REVIEW_{args.plan_name}.md (review feedback)")
            print(f"   • PLAN_FINAL_{args.plan_name}.md (approval assessment)")
            print(f"   • PLAN_SUMMARY_{args.plan_name}.json (execution summary)")
            print(f"\n✅ Next step: Review PLAN_DRAFT_{args.plan_name}.md and begin implementation")
            print(f"\n{'='*70}\n")
            
            return 0
        
        if state == "stuck":
            logging.info("Planner stuck: insufficient information in problem statement")
            stuck_file = f"PLAN_STUCK_{args.plan_name}.md"
            logging.info(f"Questions written to {stuck_file}")
            logging.info("User must read questions, provide answers, and update problem statement")
            summary.outcome = "stuck"
            summary.end_time = datetime.now().isoformat()
            
            # Print and write summary
            print(summary)
            print(f"\n{'='*70}")
            print(f"⚠️  PLANNER NEEDS MORE INFORMATION")
            print(f"{'='*70}")
            print(f"\nThe planner cannot create a plan with the current problem statement.")
            print(f"\nNext steps:")
            print(f"  1. Read the questions in: {stuck_file}")
            print(f"  2. Update your problem statement with the answers")
            print(f"  3. DELETE the file: {stuck_file}")
            print(f"  4. Restart lisamarge with the updated problem statement")
            print(f"\n{'='*70}\n")
            
            summary_file = f"PLAN_SUMMARY_{args.plan_name}.json"
            logging.info(f"Writing execution summary to {summary_file}")
            try:
                with open(summary_file, 'w') as f:
                    json.dump(summary.to_dict(), f, indent=2)
            except (IOError, OSError) as e:
                raise StateFileError(f"Failed to write summary file: {e}")
            
            return 3
        
        if state == "initial":
            # Invoke planner with problem statement
            logging.info("Invoking planner with problem statement")
            prompt = build_prompt("planner", args.plan_name, problem_statement=problem_statement)
            
            def invoke(is_final_attempt=False, intervene_on_final_retry=False):
                return invoke_kiro_cli(args.kiro_cli_path, prompt, "planner", planner_invocations, timeout=args.timeout, agent=args.agent, trust_tools=args.trust_tools, trust_all_tools=args.trust_all_tools, is_final_attempt=is_final_attempt, intervene_on_final_retry=intervene_on_final_retry)
            
            def on_attempt(attempt):
                nonlocal planner_invocations
                planner_invocations += 1
                summary.planner_invocations = planner_invocations
            
            success, result = retry_with_backoff(invoke, args.max_retries, on_attempt, args.intervene_on_final_retry)
            if not success:
                if "PlannerBlockedError" in str(result):
                    logging.error("Planner blocked: insufficient requirements")
                    summary.outcome = "blocked"
                    summary.end_time = datetime.now().isoformat()
                    print(summary)
                    summary_file = f"PLAN_SUMMARY_{args.plan_name}.json"
                    logging.info(f"Writing execution summary to {summary_file}")
                    try:
                        with open(summary_file, 'w') as f:
                            json.dump(summary.to_dict(), f, indent=2)
                    except (IOError, OSError) as e:
                        raise StateFileError(f"Failed to write summary file: {e}")
                    return 3
                logging.error(f"Planner failed after retries: {result}")
                summary.outcome = "failure"
                summary.end_time = datetime.now().isoformat()
                print(summary)
                summary_file = f"PLAN_SUMMARY_{args.plan_name}.json"
                logging.info(f"Writing execution summary to {summary_file}")
                try:
                    with open(summary_file, 'w') as f:
                        json.dump(summary.to_dict(), f, indent=2)
                except (IOError, OSError) as e:
                    raise StateFileError(f"Failed to write summary file: {e}")
                return 2
            
        elif state == "draft_ready":
            # Invoke reviewer with draft
            logging.info("Invoking reviewer with draft")
            prompt = build_prompt("reviewer", args.plan_name, problem_statement=problem_statement)
            
            def invoke(is_final_attempt=False, intervene_on_final_retry=False):
                return invoke_kiro_cli(args.kiro_cli_path, prompt, "reviewer", reviewer_invocations, timeout=args.timeout, agent=args.agent, trust_tools=args.trust_tools, trust_all_tools=args.trust_all_tools, is_final_attempt=is_final_attempt, intervene_on_final_retry=intervene_on_final_retry)
            
            def on_attempt(attempt):
                nonlocal reviewer_invocations
                reviewer_invocations += 1
                summary.reviewer_invocations = reviewer_invocations
            
            success, result = retry_with_backoff(invoke, args.max_retries, on_attempt, args.intervene_on_final_retry)
            if not success:
                logging.error(f"Reviewer failed after retries: {result}")
                summary.outcome = "failure"
                summary.end_time = datetime.now().isoformat()
                print(summary)
                summary_file = f"PLAN_SUMMARY_{args.plan_name}.json"
                logging.info(f"Writing execution summary to {summary_file}")
                try:
                    with open(summary_file, 'w') as f:
                        json.dump(summary.to_dict(), f, indent=2)
                except (IOError, OSError) as e:
                    raise StateFileError(f"Failed to write summary file: {e}")
                return 2
            
        elif state == "review_ready":
            # Invoke planner with review feedback
            logging.info("Invoking planner with review feedback")
            review_file = f"PLAN_REVIEW_{args.plan_name}.md"
            logging.info(f"Reading review feedback from {review_file}")
            try:
                with open(review_file, 'r') as f:
                    review_feedback = f.read()
            except (IOError, OSError) as e:
                raise StateFileError(f"Failed to read review file: {e}")
            
            logging.info(f"Building planner prompt with problem_statement length: {len(problem_statement) if problem_statement else 0}")
            prompt = build_prompt("planner", args.plan_name, problem_statement=problem_statement,
                                review_feedback=review_feedback)
            
            def invoke(is_final_attempt=False, intervene_on_final_retry=False):
                return invoke_kiro_cli(args.kiro_cli_path, prompt, "planner", planner_invocations, timeout=args.timeout, agent=args.agent, trust_tools=args.trust_tools, trust_all_tools=args.trust_all_tools, is_final_attempt=is_final_attempt, intervene_on_final_retry=intervene_on_final_retry)
            
            def on_attempt(attempt):
                nonlocal planner_invocations, revision_cycles
                planner_invocations += 1
                if attempt == 1:  # Only count as revision cycle on first attempt
                    revision_cycles += 1
                summary.planner_invocations = planner_invocations
                summary.revision_cycles = revision_cycles
            
            success, result = retry_with_backoff(invoke, args.max_retries, on_attempt, args.intervene_on_final_retry)
            if not success:
                if "PlannerBlockedError" in str(result):
                    logging.error("Planner blocked: insufficient requirements")
                    summary.outcome = "blocked"
                    summary.end_time = datetime.now().isoformat()
                    print(summary)
                    summary_file = f"PLAN_SUMMARY_{args.plan_name}.json"
                    logging.info(f"Writing execution summary to {summary_file}")
                    try:
                        with open(summary_file, 'w') as f:
                            json.dump(summary.to_dict(), f, indent=2)
                    except (IOError, OSError) as e:
                        raise StateFileError(f"Failed to write summary file: {e}")
                    return 3
                logging.error(f"Planner failed after retries: {result}")
                summary.outcome = "failure"
                summary.end_time = datetime.now().isoformat()
                print(summary)
                summary_file = f"PLAN_SUMMARY_{args.plan_name}.json"
                logging.info(f"Writing execution summary to {summary_file}")
                try:
                    with open(summary_file, 'w') as f:
                        json.dump(summary.to_dict(), f, indent=2)
                except (IOError, OSError) as e:
                    raise StateFileError(f"Failed to write summary file: {e}")
                return 2
            
            planner_invocations += 1
            revision_cycles += 1
            summary.planner_invocations = planner_invocations
            summary.revision_cycles = revision_cycles
    
    logging.error(f"Max agent invocations ({args.max_agent_invocations}) exceeded")
    summary.outcome = "max_iterations"
    summary.end_time = datetime.now().isoformat()
    print(summary)
    summary_file = f"PLAN_SUMMARY_{args.plan_name}.json"
    logging.info(f"Writing execution summary to {summary_file}")
    try:
        with open(summary_file, 'w') as f:
            json.dump(summary.to_dict(), f, indent=2)
    except (IOError, OSError) as e:
        raise StateFileError(f"Failed to write summary file: {e}")
    return 1


def main():
    """Main entry point for LisaMarge orchestrator.
    
    Parses command line arguments, validates inputs, and orchestrates planning
    workflow. Handles all exceptions and converts them to appropriate exit codes.
    
    Exit Codes:
        0: Success (PLAN_FINAL created)
        1: Max iterations exceeded
        2: Error (KiroCliError, StateFileError, validation error)
        3: Planner blocked (insufficient requirements)
        
    Example:
        $ ./lisamarge.py --kiro-cli-path kiro-cli --problem-statement "..." --plan-name test
    """
    args = parse_args()
    
    try:
        validate_inputs(args)
        exit_code = orchestrate_planning(args)
    except PlannerBlockedError:
        logging.error("Planner blocked due to insufficient requirements")
        exit_code = 3
    except KiroCliError as e:
        logging.error(f"Kiro-cli error: {e}")
        exit_code = 2
    except StateFileError as e:
        logging.error(f"State file error: {e}")
        exit_code = 2
    except ValueError as e:
        logging.error(f"Validation error: {e}")
        exit_code = 2
    
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
