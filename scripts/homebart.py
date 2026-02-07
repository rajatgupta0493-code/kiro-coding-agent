#!/usr/bin/env python3
"""HomerBart: Plan Execution Orchestrator

Executes plans created by lisamarge.py using worker → reviewer cycle pattern.

What It Does:
    Takes a plan file (PLAN_DRAFT_<name>.md) and executes each step through an
    iterative worker → reviewer cycle. Workers implement the step, reviewers
    verify completion. Only advances to next step after approval.

Why Use This:
    - Executes LLM-optimized plans from lisamarge.py
    - Iterative verification ensures quality before advancing
    - File-based state tracking enables resumable execution
    - Catches implementation issues early through review cycle

How It Works:
    1. Parse plan file to extract steps
    2. For each step sequentially:
       a. Worker implements the step
       b. Reviewer verifies completion
       c. Repeat until approved
    3. Advance to next step only after approval

Generated Files:
    - WORK_<name>_step_<N>.md: Worker's completion summary for each step
    - REVIEW_<name>_step_<N>.md: Reviewer's assessment (APPROVED or NEEDS REWORK)
    - EXECUTION_SUMMARY_<name>.json: Final execution metrics and outcome
    
    These files form a state machine enabling resumable execution sessions.

⚠️ CRITICAL: DO NOT RUN MULTIPLE PLANS CONCURRENTLY IN THE SAME CODEBASE ⚠️

If two plans modify overlapping code that needs to compile/build, they WILL
interfere with each other and cause failures. Run them sequentially instead:

    $ ./homerbart.py --plan-name plan1 && ./homerbart.py --plan-name plan2

Then go get a coffee. Seriously. Don't try to parallelize this.

Exit Codes:
    0: Success (all steps completed and approved)
    1: Max iterations exceeded
    2: Error (validation, file system, kiro-cli issues)
    3: Worker blocked (insufficient requirements - needs clarification)

Example:
    Basic usage:
        $ ./homerbart.py \\
            --kiro-cli-path /usr/local/bin/kiro-cli \\
            --plan-name feature

    With custom agent and trust settings:
        $ ./homerbart.py \\
            --kiro-cli-path /usr/local/bin/kiro-cli \\
            --plan-name feature \\
            --agent my-agent \\
            --trust-all-tools

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
import re
import subprocess
import sys
import time
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import List, Dict, Any

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Default trust-tools configuration
DEFAULT_TRUST_TOOLS = "read,write,@builder-mcp/glob,@builder-mcp/grep,@builder-mcp"


@dataclass
class ExecutionSummary:
    """Summary of execution orchestration.
    
    Tracks execution metadata including timing, agent invocation counts, final state,
    and outcome. Provides formatted output and JSON serialization for logging
    and CI/CD integration.
    
    Attributes:
        start_time (str): ISO 8601 timestamp of execution start
        end_time (str): ISO 8601 timestamp of execution completion (None if in progress)
        worker_invocations (int): Total times worker agent was called
        reviewer_invocations (int): Total times reviewer agent was called
        revision_cycles (int): Number of times worker revised based on reviewer feedback
        steps_completed (int): Number of steps successfully completed
        final_state (str): Last detected state
        outcome (str): Execution result (success/failure/max_agent_invocations)
    """
    start_time: str
    end_time: str = None
    worker_invocations: int = 0
    reviewer_invocations: int = 0
    revision_cycles: int = 0
    steps_completed: int = 0
    final_state: str = None
    outcome: str = None
    
    def to_dict(self) -> dict:
        """Return dictionary representation for JSON serialization."""
        return asdict(self)
    
    def __str__(self) -> str:
        """Return formatted summary string for console output."""
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
Worker Invocations: {self.worker_invocations}
Reviewer Invocations: {self.reviewer_invocations}
Revision Cycles: {self.revision_cycles}
Steps Completed: {self.steps_completed}
Final State: {self.final_state}
Outcome: {self.outcome}
========================
"""


# Custom exception classes
class ExecutionError(Exception):
    """Base exception for execution workflow failures."""
    pass


class KiroCliError(ExecutionError):
    """Kiro-cli invocation failures."""
    pass


class StateFileError(ExecutionError):
    """State file I/O failures."""
    pass


class WorkerBlockedError(ExecutionError):
    """Worker reports insufficient requirements."""
    pass


def detect_step_state(plan_name: str, step_num: int) -> str:
    """Detect current step state by checking file existence.
    
    Implements state machine detection logic by checking for step files
    in priority order. Higher priority states take precedence.
    
    Priority order (highest to lowest):
        1. REVIEW_<plan-name>_step_<N>.md exists and starts with "APPROVED" → "approved"
        2. REVIEW_<plan-name>_step_<N>.md exists and starts with "NEEDS REWORK" → "needs_rework"
        3. REVIEW_<plan-name>_step_<N>.md exists (other content) → "review_done"
        4. WORK_<plan-name>_step_<N>.md exists → "work_done"
        5. No files → "initial"
    
    Args:
        plan_name (str): Identifier for plan execution session
        step_num (int): Step number (1-based)
        
    Returns:
        str: State string ("initial", "work_done", "review_done", "approved", or "needs_rework")
        
    Example:
        >>> detect_step_state("feature", 1)
        'initial'
        >>> # After worker creates work file
        >>> detect_step_state("feature", 1)
        'work_done'
    """
    review_file = f"REVIEW_{plan_name}_step_{step_num}.md"
    work_file = f"WORK_{plan_name}_step_{step_num}.md"
    
    if os.path.exists(review_file):
        logging.info(f"State detection: {review_file} exists, checking content")
        try:
            with open(review_file, 'r') as f:
                first_line = f.readline()
                if "APPROVED" in first_line:
                    logging.info(f"State detection: {review_file} first line contains APPROVED → approved")
                    return "approved"
                elif "NEEDS REWORK" in first_line:
                    logging.info(f"State detection: {review_file} first line contains NEEDS REWORK → needs_rework")
                    return "needs_rework"
                else:
                    logging.info(f"State detection: {review_file} exists but no status marker → review_done")
                    return "review_done"
        except (IOError, OSError) as e:
            raise StateFileError(f"Failed to read review file {review_file}: {e}")
    
    if os.path.exists(work_file):
        logging.info(f"State detection: {work_file} exists → work_done")
        return "work_done"
    
    logging.info(f"State detection: No files found for step {step_num} → initial")
    return "initial"


def parse_plan_file(plan_file_path: str) -> List[Dict[str, Any]]:
    """Parse plan file to extract step blocks.
    
    Reads PLAN_DRAFT_<plan_name>.md file and extracts step blocks marked by
    ---STEP_BLOCK--- and ---END_STEP_BLOCK--- delimiters.
    The heading "### Step N:" appears BEFORE the block delimiter.
    
    Args:
        plan_file_path (str): Path to plan file
        
    Returns:
        List[Dict[str, Any]]: List of step dictionaries with keys:
            - step_num (int): Step number
            - description (str): Full step description
            - success_criteria (str): Success criteria section
            
    Raises:
        StateFileError: If file doesn't exist, can't be read, or has malformed blocks
        
    Example:
        >>> steps = parse_plan_file("PLAN_DRAFT_feature.md")
        >>> print(steps[0]['step_num'])
        1
    """
    if not os.path.exists(plan_file_path):
        raise StateFileError(f"Plan file not found: {plan_file_path}")
    
    try:
        with open(plan_file_path, 'r', encoding='utf-8') as f:
            content = f.read()
    except (IOError, OSError) as e:
        raise StateFileError(f"Failed to read plan file {plan_file_path}: {e}")
    except UnicodeDecodeError as e:
        raise StateFileError(f"Encoding error reading plan file {plan_file_path}: {e}")
    
    # Extract block pairs where heading is INSIDE the block
    # Pattern: ---STEP_BLOCK--- followed by ### Step N: ... then ---END_STEP_BLOCK---
    pattern = r'---STEP_BLOCK---\s*###\s*(?:Step|Microquest)\s*(\d+):[^\n]*\n(.*?)---END_STEP_BLOCK---'
    matches = re.findall(pattern, content, re.DOTALL | re.IGNORECASE)
    
    if not matches:
        logging.warning(f"No step blocks found in {plan_file_path}")
        return []
    
    steps = []
    seen_step_nums = set()
    
    for i, (step_num_str, block) in enumerate(matches, 1):
        step_num = int(step_num_str)
        
        # Check for duplicate step numbers
        if step_num in seen_step_nums:
            raise StateFileError(f"Duplicate step number {step_num} found")
        seen_step_nums.add(step_num)
        
        # Extract success criteria section
        success_criteria_match = re.search(r'\*\*Success Criteria\*\*:\s*(.*?)(?=\*\*|$)', block, re.DOTALL | re.IGNORECASE)
        success_criteria = success_criteria_match.group(1).strip() if success_criteria_match else ""
        
        steps.append({
            'step_num': step_num,
            'description': block.strip(),
            'success_criteria': success_criteria
        })
    
    # Check for sequential step numbers
    expected_nums = set(range(1, len(steps) + 1))
    actual_nums = set(s['step_num'] for s in steps)
    if expected_nums != actual_nums:
        missing = expected_nums - actual_nums
        raise StateFileError(f"Non-sequential step numbers: missing steps {sorted(missing)}")
    
    # Sort by step number
    steps.sort(key=lambda s: s['step_num'])
    
    logging.info(f"Parsed {len(steps)} steps from {plan_file_path}")
    return steps


def build_worker_prompt(plan_name: str, step_num: int, step_description: str, review_feedback: str = None) -> str:
    """Build worker prompt for step implementation.
    
    Args:
        plan_name: Plan identifier
        step_num: Step number (1-based)
        step_description: Full step description from plan file
        review_feedback: Optional feedback from reviewer (for rework scenarios)
        
    Returns:
        str: Complete prompt for worker agent
    """
    feedback_section = ""
    if review_feedback:
        feedback_section = f"""
**⚠️ REWORK REQUIRED ⚠️**

The reviewer has identified issues with your previous work. Read this feedback carefully and address ALL points:

{review_feedback}

---
"""
    
    return f"""{feedback_section}You are implementing Step {step_num} of the plan "{plan_name}".

**IMPORTANT: YOU ARE IN IMPLEMENTATION MODE ONLY**
- DO NOT use quest-manager tools (add_quest, modify_quest, complete_quest, etc.)
- DO NOT move to the next step - only work on Step {step_num}
- DO NOT mark this step complete - the reviewer will verify your work
- Your role is IMPLEMENTATION only

**STEP DESCRIPTION:**
{step_description}

**YOUR TASK:**
Implement the changes described above. When complete, write a summary to `WORK_{plan_name}_step_{step_num}.md` containing:
- What was accomplished
- Files modified
- Tests run and results
- Any issues encountered

**PROHIBITED:**
- Using quest-manager tools
- Moving to next step
- Calling modify_quest or complete_quest
- Making changes outside this step's scope"""


def build_reviewer_prompt(plan_name: str, step_num: int, step_description: str, work_content: str) -> str:
    """Build reviewer prompt for work verification.
    
    Args:
        plan_name: Plan identifier
        step_num: Step number (1-based)
        step_description: Full step description for context
        work_content: Work completion summary from WORK file
        
    Returns:
        str: Complete prompt for reviewer agent
    """
    return f"""You are reviewing Step {step_num} of the plan "{plan_name}".

**IMPORTANT: YOU ARE IN REVIEW MODE ONLY**
- DO NOT use quest-manager tools
- DO NOT move to the next step
- Your role is EVALUATION only

**STEP DESCRIPTION:**
{step_description}

**WORK COMPLETED:**
{work_content}

**YOUR TASK:**
Evaluate whether the work meets the step's success criteria. Write your assessment to `REVIEW_{plan_name}_step_{step_num}.md`:

**For APPROVAL:**
- Start file with "APPROVED"
- Include verification details confirming success criteria met

**For REWORK:**
- Start file with "NEEDS REWORK"
- List specific issues found
- Provide clear guidance on what needs fixing

**PROHIBITED:**
- Using quest-manager tools
- Moving to next step
- Making code changes"""



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
        return (True, "", "")
    except subprocess.TimeoutExpired:
        raise KiroCliError(f"kiro-cli timed out after {timeout} seconds")
    except Exception as e:
        raise KiroCliError(f"kiro-cli invocation failed: {e}")


def retry_with_backoff(func: callable, max_retries: int, on_attempt: callable = None, *args, **kwargs) -> tuple[bool, any]:
    """Retry a function on failure.
    
    Implements retry strategy with fixed 2-second delay:
    - Retries with 2-second delay between attempts
    - Continues until success or max_retries exhausted
    
    Args:
        func (callable): Function to retry
        max_retries (int): Maximum number of retry attempts
        on_attempt (callable): Optional callback called before each attempt with attempt number
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
            result = func(*args, **kwargs)
            return (True, result)
        except Exception as e:
            if attempt < max_retries:
                logging.warning(f"Attempt {attempt}/{max_retries} failed: {e}. Retrying in 2s...")
                time.sleep(2)
            else:
                logging.error(f"All {max_retries} retries exhausted. Last error: {e}")
                return (False, str(e))


def orchestrate_execution(args: argparse.Namespace) -> int:
    """Orchestrate plan execution through worker → reviewer cycles.
    
    Args:
        args: Parsed command line arguments
        
    Returns:
        int: Exit code (0=success, 1=max iterations, 2=error, 3=worker blocked)
    """
    summary = ExecutionSummary(start_time=datetime.now().isoformat())
    plan_file = f"PLAN_DRAFT_{args.plan_name}.md"
    
    try:
        steps = parse_plan_file(plan_file)
        if not steps:
            raise StateFileError(f"No steps found in {plan_file}")
        
        logging.info(f"Executing {len(steps)} steps from {plan_file}")
        
        for step in steps:
            step_num = step['step_num']
            logging.info(f"\n{'='*70}")
            logging.info(f"STEP {step_num}/{len(steps)}")
            logging.info(f"{'='*70}")
            
            iteration = 0
            step_revision_cycles = 0
            last_role = None  # Track last successful role
            
            while iteration < args.max_agent_invocations:
                iteration += 1
                
                # If last role was worker, always dispatch reviewer next
                if last_role == "worker":
                    state = "work_done"
                    logging.info(f"Step {step_num} state: {state} (iteration {iteration}) [forced after worker success]")
                else:
                    state = detect_step_state(args.plan_name, step_num)
                    logging.info(f"Step {step_num} state: {state} (iteration {iteration})")
                
                summary.final_state = state
                
                if state == "approved":
                    logging.info(f"Step {step_num} approved, advancing to next step")
                    summary.steps_completed += 1
                    break
                
                if state in ["initial", "needs_rework"]:
                    if state == "needs_rework":
                        step_revision_cycles += 1
                        summary.revision_cycles += 1
                    
                    review_feedback = None
                    if state == "needs_rework":
                        review_file = f"REVIEW_{args.plan_name}_step_{step_num}.md"
                        try:
                            with open(review_file, 'r') as f:
                                content = f.read()
                                review_feedback = content.replace("NEEDS REWORK\n", "").strip()
                        except (IOError, OSError) as e:
                            raise StateFileError(f"Failed to read review file: {e}")
                    
                    prompt = build_worker_prompt(args.plan_name, step_num, step['description'], review_feedback)
                    is_final_attempt = (iteration == args.max_agent_invocations)
                    
                    try:
                        invoke_kiro_cli(args.kiro_cli_path, prompt, role="worker", attempt=iteration, timeout=args.timeout, agent=args.agent, trust_tools=args.trust_tools, trust_all_tools=args.trust_all_tools, is_final_attempt=is_final_attempt, intervene_on_final_retry=args.intervene_on_final_retry)
                        summary.worker_invocations += 1
                        last_role = "worker"  # Track successful worker completion
                    except KiroCliError as e:
                        if "timed out" in str(e):
                            work_file = f"WORK_{args.plan_name}_step_{step_num}.md"
                            timeout_feedback = f"""Worker timed out after {args.timeout} seconds while working on this step.

This likely means the step is too complex or unclear. The step should be:
1. Broken down into smaller substeps, or
2. Clarified with more specific requirements

Original step description:
{step['description']}"""
                            try:
                                with open(work_file, 'w') as f:
                                    f.write(timeout_feedback)
                                logging.warning(f"Worker timed out, wrote timeout feedback to {work_file}")
                                summary.worker_invocations += 1
                            except (IOError, OSError) as write_error:
                                raise StateFileError(f"Failed to write timeout feedback: {write_error}")
                        else:
                            raise
                    continue
                
                if state == "work_done":
                    work_file = f"WORK_{args.plan_name}_step_{step_num}.md"
                    try:
                        with open(work_file, 'r') as f:
                            work_content = f.read()
                    except (IOError, OSError) as e:
                        raise StateFileError(f"Failed to read work file: {e}")
                    
                    prompt = build_reviewer_prompt(args.plan_name, step_num, step['description'], work_content)
                    is_final_attempt = (iteration == args.max_agent_invocations)
                    
                    try:
                        invoke_kiro_cli(args.kiro_cli_path, prompt, role="reviewer", attempt=iteration, timeout=args.timeout, agent=args.agent, trust_tools=args.trust_tools, trust_all_tools=args.trust_all_tools, is_final_attempt=is_final_attempt, intervene_on_final_retry=args.intervene_on_final_retry)
                        summary.reviewer_invocations += 1
                        last_role = "reviewer"  # Track successful reviewer completion
                    except KiroCliError as e:
                        if "timed out" in str(e):
                            review_file = f"REVIEW_{args.plan_name}_step_{step_num}.md"
                            timeout_feedback = f"""NEEDS REWORK

Reviewer timed out after {args.timeout} seconds while trying to verify your work.

**ACTION REQUIRED:**
1. Check your WORK_{args.plan_name}_step_{step_num}.md file - does it clearly describe what you did?
2. Verify the code actually works (run tests, check compilation)
3. Make sure you completed ALL parts of the step description
4. If the step is too complex, break it into smaller pieces

**Common causes of reviewer timeout:**
- WORK file is vague or missing details
- Code has bugs that cause the reviewer to investigate deeply
- Step requirements weren't fully met
- Tests are failing or not comprehensive

**What to do:**
- Re-read the step description carefully
- Update your WORK file with specific details about what changed
- Fix any bugs or incomplete work
- Ensure all success criteria are met"""
                            try:
                                with open(review_file, 'w') as f:
                                    f.write(timeout_feedback)
                                logging.warning(f"Reviewer timed out, wrote timeout feedback to {review_file}")
                                summary.reviewer_invocations += 1
                            except (IOError, OSError) as write_error:
                                raise StateFileError(f"Failed to write timeout feedback: {write_error}")
                        else:
                            raise
                    continue
                
                if state == "review_done":
                    logging.warning(f"Review file exists but no status marker, treating as needs_rework")
                    continue
            
            if iteration >= args.max_agent_invocations:
                logging.error(f"Step {step_num} exceeded max agent invocations ({args.max_agent_invocations})")
                summary.end_time = datetime.now().isoformat()
                summary.outcome = "max_agent_invocations"
                write_summary(args.plan_name, summary)
                return 1
        
        logging.info(f"\n{'='*70}")
        logging.info("ALL STEPS COMPLETED")
        logging.info(f"{'='*70}")
        
        summary.end_time = datetime.now().isoformat()
        summary.outcome = "success"
        write_summary(args.plan_name, summary)
        return 0
        
    except Exception as e:
        logging.error(f"Execution failed: {e}")
        summary.end_time = datetime.now().isoformat()
        summary.outcome = "failure"
        write_summary(args.plan_name, summary)
        raise


def write_summary(plan_name: str, summary: ExecutionSummary) -> None:
    """Write execution summary to JSON file.
    
    Args:
        plan_name: Plan identifier
        summary: ExecutionSummary instance
    """
    summary_file = f"EXECUTION_SUMMARY_{plan_name}.json"
    try:
        with open(summary_file, 'w') as f:
            json.dump(summary.to_dict(), f, indent=2)
        logging.info(f"Execution summary written to {summary_file}")
        logging.info(str(summary))
    except (IOError, OSError) as e:
        logging.error(f"Failed to write summary file: {e}")


def parse_args():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(description="Execute plan through worker → reviewer cycles")
    
    parser.add_argument("--kiro-cli-path", required=True, help="Path to kiro-cli executable")
    parser.add_argument("--plan-name", required=True, help="Plan identifier (used to find PLAN_DRAFT_<plan_name>.md)")
    parser.add_argument("--max-agent-invocations", type=int, default=10, help="Maximum total agent invocations per step (worker + reviewer calls, default: 10)")
    parser.add_argument("--max-retries", type=int, default=3, help="Retry limit per invocation (default: 3)")
    parser.add_argument("--timeout", type=int, default=600, help="Timeout per kiro-cli invocation in seconds (default: 600)")
    parser.add_argument("--agent", help="Kiro-cli agent name (optional)")
    
    trust_group = parser.add_mutually_exclusive_group(required=False)
    trust_group.add_argument("--trust-tools", default=DEFAULT_TRUST_TOOLS, help=f"Comma-separated list of trusted tools (default: {DEFAULT_TRUST_TOOLS})")
    trust_group.add_argument("--trust-all-tools", action="store_true", help="Trust all tools without confirmation")
    
    parser.add_argument(
        "--intervene-on-final-retry",
        action="store_true",
        default=False,
        help="Enable interactive mode on final retry attempt for debugging"
    )
    
    return parser.parse_args()


def validate_inputs(args: argparse.Namespace) -> None:
    """Validate command line arguments."""
    if not os.path.exists(args.kiro_cli_path):
        raise ValueError(f"Kiro-cli not found: {args.kiro_cli_path}")
    if not os.access(args.kiro_cli_path, os.X_OK):
        raise ValueError(f"Kiro-cli not executable: {args.kiro_cli_path}")
    
    plan_file = f"PLAN_DRAFT_{args.plan_name}.md"
    if not os.path.exists(plan_file):
        raise ValueError(f"Plan file not found: {plan_file}")
    
    if not args.plan_name.replace('_', '').isalnum():
        raise ValueError(f"Plan name must be alphanumeric with underscores only: {args.plan_name}")


def main():
    """Main entry point."""
    args = parse_args()
    
    try:
        validate_inputs(args)
        exit_code = orchestrate_execution(args)
    except WorkerBlockedError:
        logging.error("Worker blocked due to insufficient requirements")
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
