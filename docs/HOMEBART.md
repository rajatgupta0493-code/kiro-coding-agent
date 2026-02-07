# HomerBart: Plan Execution Orchestrator

## Purpose and Overview

HomerBart executes plans created by lisamarge.py using a worker → reviewer cycle pattern. It takes a plan file and executes each step through iterative verification, ensuring quality before advancing to the next step.

**Key Features:**
- Sequential step execution with quality gates
- Worker → reviewer cycle for each step
- File-based state machine for resumable sessions
- Exponential backoff retry logic for transient failures
- Comprehensive execution summaries with JSON output
- Support for custom agents and trust configurations

## Installation and Setup

### Prerequisites
- Python 3.8 or higher
- kiro-cli executable installed and accessible
- Write permissions in working directory for state files
- Plan file created by lisamarge.py (PLAN_DRAFT_<name>.md)

### Setup
```bash
# Ensure homerbart.py is executable
chmod +x homerbart.py

# Verify kiro-cli is accessible
which kiro-cli
```

## Parameter Reference

| Parameter | Required | Type | Default | Description |
|-----------|----------|------|---------|-------------|
| `--kiro-cli-path` | Yes | string | - | Path to kiro-cli executable |
| `--plan-name` | Yes | string | - | Identifier for plan (matches lisamarge plan name) |
| `--max-agent-invocations` | No | int | 10 | Maximum total agent invocations per step (worker + reviewer calls) |
| `--max-retries` | No | int | 3 | Retry limit per kiro-cli invocation |
| `--timeout` | No | int | 600 | Timeout per kiro-cli invocation in seconds |
| `--agent` | No | string | - | Custom agent name for kiro-cli |
| `--trust-tools` | No | string | read,write,@builder-mcp/glob,@builder-mcp/grep,@builder-mcp | Comma-separated list of trusted tools |
| `--trust-all-tools` | No | flag | false | Trust all tools (disables tool confirmation, mutually exclusive with --trust-tools) |
| `--intervene-on-final-retry` | No | flag | false | Enable interactive mode on final retry attempt for debugging |

## Architecture

### Worker → Reviewer Cycle

HomerBart implements a quality gate pattern where each step must be approved before advancing:

```
┌──────────┐
│   Step   │
│ Initial  │
└────┬─────┘
     │
     v
┌──────────┐
│  Worker  │ ──> WORK_<plan>_step_<N>.md
│Implements│
└────┬─────┘
     │
     v
┌──────────┐
│ Reviewer │ ──> REVIEW_<plan>_step_<N>.md
│ Verifies │
└────┬─────┘
     │
     ├─[APPROVED]──> Next Step
     │
     └─[NEEDS REWORK]──> Worker (with feedback)
```

### File-Based State Machine

HomerBart uses file existence and content to track execution state:

**State Detection Priority:**
1. `REVIEW_<plan>_step_<N>.md` starts with "APPROVED" → approved (advance to next step)
2. `REVIEW_<plan>_step_<N>.md` starts with "NEEDS REWORK" → needs_rework (worker revises)
3. `REVIEW_<plan>_step_<N>.md` exists (other content) → review_done
4. `WORK_<plan>_step_<N>.md` exists → work_done (dispatch reviewer)
5. No files → initial (dispatch worker)

**State Files:**
- `WORK_<plan>_step_<N>.md`: Worker's implementation output
- `REVIEW_<plan>_step_<N>.md`: Reviewer's assessment and feedback
- `EXECUTION_STATE_<plan>.json`: Execution metadata and progress

## Usage Examples

### 1. Basic Execution

```bash
# Execute plan created by lisamarge
./homerbart.py \
  --kiro-cli-path /usr/local/bin/kiro-cli \
  --plan-name feature
```

### 2. With Custom Agent

```bash
# Use specific agent for execution
./homerbart.py \
  --kiro-cli-path /usr/local/bin/kiro-cli \
  --plan-name feature \
  --agent worker-agent
```

### 3. With Trust All Tools

```bash
# Disable tool confirmation prompts
./homerbart.py \
  --kiro-cli-path /usr/local/bin/kiro-cli \
  --plan-name feature \
  --trust-all-tools
```

### 4. Resuming Interrupted Execution

```bash
# HomerBart detects existing state and continues from last step
./homerbart.py \
  --kiro-cli-path /usr/local/bin/kiro-cli \
  --plan-name interrupted_plan
```

### 5. Custom Iteration Limits

```bash
# Allow more worker ↔ reviewer cycles per step
./homerbart.py \
  --kiro-cli-path /usr/local/bin/kiro-cli \
  --plan-name complex_feature \
  --max-agent-invocations 20
```

### 6. With Custom Timeout

```bash
# Increase timeout for complex steps
./homerbart.py \
  --kiro-cli-path /usr/local/bin/kiro-cli \
  --plan-name feature \
  --timeout 1200
```

### 7. Debug Mode with Intervention

```bash
# Enable interactive mode on final retry for debugging
./homerbart.py \
  --kiro-cli-path /usr/local/bin/kiro-cli \
  --plan-name feature \
  --intervene-on-final-retry
```

## Exit Codes Reference

| Exit Code | Meaning | Description |
|-----------|---------|-------------|
| 0 | Success | All steps completed and approved |
| 1 | Max Agent Invocations | Maximum agent invocations exceeded for a step |
| 2 | Error | Validation, file system, or kiro-cli issues |
| 3 | Worker Blocked | Insufficient requirements - needs clarification |

**Exit Code Usage:**
```bash
./homerbart.py --kiro-cli-path kiro-cli --plan-name test
EXIT_CODE=$?

case $EXIT_CODE in
  0) echo "Success: All steps completed" ;;
  1) echo "Max iterations exceeded" ;;
  2) echo "Error occurred" ;;
  3) echo "Worker blocked: needs clarification" ;;
esac
```

## State File Format

### WORK File Format
```markdown
# Work Output for Step N

## Implementation Summary
[Worker's description of what was implemented]

## Files Modified
- file1.py: Added validation logic
- file2.py: Updated error handling

## Testing
[Testing performed, if any]
```

### REVIEW File Format
```markdown
APPROVED: All success criteria met

## Verification Details
- Validated file1.py changes
- Confirmed error handling works
- All tests passing

## Notes
[Optional reviewer notes]
```

OR

```markdown
NEEDS REWORK: Missing validation for edge case

## Issues Found
1. Missing null check in file1.py line 45
2. Error handling incomplete for timeout scenario

## Required Changes
- Add null validation
- Implement timeout handling
```

### EXECUTION_STATE File Format
```json
{
  "plan_name": "feature",
  "current_step": 2,
  "total_steps": 5,
  "start_time": "2026-01-24T10:00:00",
  "steps_completed": 1,
  "worker_invocations": 3,
  "reviewer_invocations": 3,
  "revision_cycles": 1
}
```

**Note:** The plan file format supports both "Step N:" and "Microquest N:" headings within step blocks.

## Troubleshooting

### Common Issues

**Issue: "Plan file not found: PLAN_DRAFT_<name>.md"**
```bash
# Solution: Ensure plan file exists from lisamarge.py
ls PLAN_DRAFT_*.md
# Run lisamarge first if missing
./lisamarge.py --kiro-cli-path kiro-cli --problem-statement "..." --plan-name <name>
```

**Issue: "kiro-cli path does not exist"**
```bash
# Solution: Verify kiro-cli path
which kiro-cli
# Use full path in --kiro-cli-path
```

**Issue: "Max agent invocations exceeded for step N"**
```bash
# Worker and reviewer cannot converge
# Options:
# 1. Increase --max-agent-invocations
# 2. Review REVIEW_<plan>_step_<N>.md for feedback patterns
# 3. Manually fix issues and remove review file to retry
```

**Issue: "Worker blocked: insufficient requirements"**
```bash
# Worker needs more information
# Review WORK_<plan>_step_<N>.md for questions
# Update plan file with clarifications
# Remove work/review files and retry
```

**Issue: Execution stuck in rework loop**
```bash
# Check review feedback
cat REVIEW_<plan>_step_<N>.md

# If feedback is unclear, manually intervene:
# 1. Fix issues directly
# 2. Create approved review file:
echo "APPROVED: Manual fix applied" > REVIEW_<plan>_step_<N>.md
# 3. Resume execution
```

### Debug Mode

Enable detailed logging:
```bash
# Set Python logging to DEBUG
export PYTHONLOGLEVEL=DEBUG
./homerbart.py --kiro-cli-path kiro-cli --plan-name debug_test
```

### State Recovery

If state files are corrupted:
```bash
# Remove state files for specific step
rm WORK_<plan>_step_<N>.md REVIEW_<plan>_step_<N>.md

# Or reset entire execution
rm WORK_<plan>_*.md REVIEW_<plan>_*.md EXECUTION_STATE_<plan>.json

# Resume execution
./homerbart.py --kiro-cli-path kiro-cli --plan-name <plan>
```

## Comparison with LisaMarge

| Aspect | LisaMarge | HomerBart |
|--------|-----------|-----------|
| **Purpose** | Plan creation | Plan execution |
| **Agents** | Planner + Reviewer | Worker + Reviewer |
| **Input** | Problem statement | Plan file (from LisaMarge) |
| **Output** | PLAN_FINAL_<name>.md | Completed implementation |
| **Cycle** | Planning iterations | Implementation iterations |
| **State Files** | PLAN_DRAFT, PLAN_REVIEW, PLAN_FINAL | WORK_step_N, REVIEW_step_N |
| **Advancement** | Approval → final plan | Approval → next step |

**Workflow Integration:**
```bash
# Step 1: Create plan with LisaMarge
./lisamarge.py \
  --kiro-cli-path kiro-cli \
  --problem-statement "Feature description" \
  --plan-name feature

# Step 2: Execute plan with HomerBart
./homerbart.py \
  --kiro-cli-path kiro-cli \
  --plan-name feature
```

## Output Files

HomerBart creates several files during execution:

| File | Description | When Created |
|------|-------------|--------------|
| `WORK_<plan>_step_<N>.md` | Worker implementation output | After worker invocation |
| `REVIEW_<plan>_step_<N>.md` | Reviewer assessment | After reviewer invocation |
| `EXECUTION_SUMMARY_<plan>.json` | Final execution summary | At completion or error |

**Execution Summary Format:**
```json
{
  "start_time": "2026-01-24T10:00:00.000000",
  "end_time": "2026-01-24T10:45:30.000000",
  "worker_invocations": 8,
  "reviewer_invocations": 8,
  "revision_cycles": 2,
  "steps_completed": 4,
  "final_state": "approved",
  "outcome": "success"
}
```

## Advanced Configuration

### Retry Strategy

Fixed 2-second delay between retries with configurable max retries:
- Attempt 1: immediate
- Attempt 2: wait 2 seconds
- Attempt 3: wait 4 seconds (if max-retries > 2)

Adjust via `--max-retries` parameter (default: 3).
