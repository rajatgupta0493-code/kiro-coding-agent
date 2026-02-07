# LisaMarge: Automated Planning Workflow Orchestrator

## Purpose and Overview

LisaMarge automates iterative planning workflows by coordinating planner and reviewer agents through kiro-cli. It manages the feedback loop between planning and review cycles until plan approval or maximum iterations are reached.

**Key Features:**
- File-based state machine for resumable planning sessions
- Fixed 2-second retry delay for transient failures
- Configurable invocation limits and timeouts
- Comprehensive execution summaries with JSON output
- Configurable trust-tools for kiro-cli security

## Installation and Setup

### Prerequisites
- Python 3.8 or higher
- kiro-cli executable installed and accessible
- Write permissions in working directory for plan files

### Setup
```bash
# Ensure lisamarge.py is executable
chmod +x lisamarge.py

# Verify kiro-cli is accessible
which kiro-cli
```

## Parameter Reference

| Parameter | Required | Type | Default | Description |
|-----------|----------|------|---------|-------------|
| `--kiro-cli-path` | Yes | string | - | Path to kiro-cli executable |
| `--problem-statement` | Yes* | string | - | Problem or feature to plan (as text) |
| `--problem-statement-file` | Yes* | string | - | Read problem statement from file |
| `--plan-name` | Yes | string | - | Identifier for planning session (alphanumeric + underscore) |
| `--max-agent-invocations` | No | int | 10 | Maximum total agent invocations (planner + reviewer calls) |
| `--max-retries` | No | int | 3 | Retry limit per kiro-cli invocation |
| `--timeout` | No | int | 600 | Timeout per kiro-cli invocation in seconds |
| `--agent` | No | string | - | Name of kiro-cli agent to use |
| `--trust-tools` | No | string | read,write,@builder-mcp/glob,@builder-mcp/grep,@builder-mcp | Comma-separated list of trusted tools |
| `--trust-all-tools` | No | flag | - | Trust all tools without confirmation (mutually exclusive with --trust-tools) |
| `--intervene-on-final-retry` | No | flag | - | Enable interactive mode on final retry attempt for debugging |

*Either `--problem-statement` or `--problem-statement-file` must be provided (mutually exclusive).

## State Machine

LisaMarge uses a file-based state machine to track planning progress:

```
┌─────────┐
│ initial │ ──[planner]──> PLAN_DRAFT_<name>.md
└─────────┘
     │
     v
┌──────────────┐
│ draft_ready  │ ──[reviewer]──> PLAN_REVIEW_<name>.md (if revisions needed)
└──────────────┘                  OR
     │                            PLAN_FINAL_<name>.md (if approved)
     v
┌───────────────┐
│ review_ready  │ ──[planner]──> PLAN_DRAFT_<name>.md (revised)
└───────────────┘
     │
     v
┌──────┐
│ done │ ──> PLAN_FINAL_<name>.md exists
└──────┘
```

**State Detection Priority:**
1. `PLAN_FINAL_<name>.md` exists → "done"
2. `PLAN_REVIEW_<name>.md` exists → "review_ready"
3. `PLAN_DRAFT_<name>.md` exists → "draft_ready"
4. No files → "initial"

## Usage Examples

### 1. Basic Usage with Problem Statement

```bash
./lisamarge.py \
  --kiro-cli-path /usr/local/bin/kiro-cli \
  --problem-statement "Add user authentication to API" \
  --plan-name auth_feature
```

### 2. Sudoku Solver Example

```bash
./lisamarge.py \
  --kiro-cli-path /Users/johnbyrn/.local/bin/kiro-cli \
  --problem-statement "Create a sudoku solver in python that works on the command line." \
  --plan-name sudoku
```

### 3. Using Problem Statement from File

```bash
# Create problem statement file
cat > problem.txt << 'EOF'
Implement rate limiting for API endpoints:
- 100 requests per minute per user
- 1000 requests per minute per IP
- Return 429 status when exceeded
EOF

# Run lisamarge
./lisamarge.py \
  --kiro-cli-path /usr/local/bin/kiro-cli \
  --problem-statement-file problem.txt \
  --plan-name rate_limiting
```

### 4. Custom Agent and Trust Configuration

```bash
# Use specific kiro-cli agent and trust all tools
./lisamarge.py \
  --kiro-cli-path /usr/local/bin/kiro-cli \
  --problem-statement "Build notification service" \
  --plan-name notifications \
  --agent my-custom-agent \
  --trust-all-tools

# Or specify specific trusted tools
./lisamarge.py \
  --kiro-cli-path /usr/local/bin/kiro-cli \
  --problem-statement "Build notification service" \
  --plan-name notifications \
  --trust-tools "read,write,execute_bash"
```

### 5. Debugging with Interactive Mode

```bash
# Enable interactive mode on final retry for debugging
./lisamarge.py \
  --kiro-cli-path /usr/local/bin/kiro-cli \
  --problem-statement "Complex feature" \
  --plan-name debug_feature \
  --intervene-on-final-retry
```

### 6. Resuming Interrupted Planning

```bash
# If planning was interrupted, simply re-run with same plan-name
# LisaMarge detects existing state and continues from last checkpoint
./lisamarge.py \
  --kiro-cli-path /usr/local/bin/kiro-cli \
  --problem-statement "Original problem statement" \
  --plan-name interrupted_plan
```

### 7. Batch Planning Multiple Problems

```bash
#!/bin/bash
# batch_plan.sh - Plan multiple features sequentially

PROBLEMS=(
  "Add caching layer"
  "Implement logging"
  "Add metrics collection"
)

for i in "${!PROBLEMS[@]}"; do
  echo "Planning: ${PROBLEMS[$i]}"
  ./lisamarge.py \
    --kiro-cli-path /usr/local/bin/kiro-cli \
    --problem-statement "${PROBLEMS[$i]}" \
    --plan-name "feature_$i" \
    --max-agent-invocations 6
  
  if [ $? -ne 0 ]; then
    echo "Planning failed for: ${PROBLEMS[$i]}"
    exit 1
  fi
done
```

## Exit Codes Reference

| Exit Code | Meaning | Description |
|-----------|---------|-------------|
| 0 | Success | PLAN_FINAL created, planning complete |
| 1 | Max Invocations | Maximum total agent invocations exceeded |
| 2 | Error | KiroCliError, StateFileError, or validation error |
| 3 | Blocked | Planner reports insufficient requirements |

**Exit Code Usage:**
```bash
./lisamarge.py --kiro-cli-path kiro-cli --problem-statement "..." --plan-name test
EXIT_CODE=$?

case $EXIT_CODE in
  0) echo "Success: Plan approved" ;;
  1) echo "Max agent invocations exceeded" ;;
  2) echo "Error occurred" ;;
  3) echo "Planner blocked: needs more requirements" ;;
esac
```

## Troubleshooting

### Common Issues

**Issue: "kiro-cli path does not exist"**
```bash
# Solution: Verify kiro-cli path
which kiro-cli
# Use full path in --kiro-cli-path
```

**Issue: "Plan name must be alphanumeric with underscores only"**
```bash
# Invalid: my-plan, my.plan, my plan
# Valid: my_plan, myplan, my_plan_123
```

**Issue: "Timeout after 600 seconds"**
```bash
# kiro-cli invocation timed out
# Check kiro-cli is responsive:
kiro-cli chat --message "test"
```

**Issue: "Planner blocked: insufficient requirements"**
```bash
# Planner needs more information
# Review problem statement for clarity
# Add more context or constraints
```

**Issue: Planning stuck in loop (max invocations exceeded)**
```bash
# Reviewer keeps rejecting plan or agents failing
# Options:
# 1. Increase --max-agent-invocations
# 2. Simplify problem statement
# 3. Check for agent errors in output
```

### Debug Mode

Enable detailed logging:
```bash
# Set Python logging to DEBUG
export PYTHONLOGLEVEL=DEBUG
./lisamarge.py --kiro-cli-path kiro-cli --problem-statement "..." --plan-name debug_test
```

### State Recovery

If state files are corrupted:
```bash
# Remove state files and restart
rm PLAN_DRAFT_myplan.md PLAN_REVIEW_myplan.md PLAN_FINAL_myplan.md
./lisamarge.py --kiro-cli-path kiro-cli --problem-statement "..." --plan-name myplan
```

## Integration with CI/CD

### GitHub Actions Example

```yaml
name: Automated Planning
on:
  issues:
    types: [labeled]

jobs:
  plan:
    if: github.event.label.name == 'needs-plan'
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
      
      - name: Setup Python
        uses: actions/setup-python@v4
        with:
          python-version: '3.11'
      
      - name: Install kiro-cli
        run: |
          # Install kiro-cli (adjust for your setup)
          pip install kiro-cli
      
      - name: Run LisaMarge
        run: |
          ./lisamarge.py \
            --kiro-cli-path $(which kiro-cli) \
            --problem-statement "${{ github.event.issue.body }}" \
            --plan-name "issue_${{ github.event.issue.number }}"
      
      - name: Upload Plan
        if: success()
        uses: actions/upload-artifact@v3
        with:
          name: plan
          path: PLAN_FINAL_*.md
      
      - name: Comment on Issue
        if: success()
        uses: actions/github-script@v6
        with:
          script: |
            const fs = require('fs');
            const plan = fs.readFileSync('PLAN_FINAL_issue_${{ github.event.issue.number }}.md', 'utf8');
            github.rest.issues.createComment({
              issue_number: context.issue.number,
              owner: context.repo.owner,
              repo: context.repo.repo,
              body: `## Automated Plan\n\n${plan}`
            });
```

### Jenkins Pipeline Example

```groovy
pipeline {
    agent any
    
    parameters {
        text(name: 'PROBLEM_STATEMENT', description: 'Feature to plan')
        string(name: 'PLAN_NAME', description: 'Plan identifier')
    }
    
    stages {
        stage('Plan') {
            steps {
                script {
                    sh """
                        ./lisamarge.py \
                            --kiro-cli-path /usr/local/bin/kiro-cli \
                            --problem-statement '${params.PROBLEM_STATEMENT}' \
                            --plan-name ${params.PLAN_NAME}
                    """
                    
                    def exitCode = sh(returnStatus: true, script: 'echo $?')
                    
                    if (exitCode == 0) {
                        archiveArtifacts artifacts: "PLAN_FINAL_${params.PLAN_NAME}.md"
                        currentBuild.result = 'SUCCESS'
                    } else if (exitCode == 3) {
                        error("Planner blocked: insufficient requirements")
                    } else {
                        error("Planning failed with exit code ${exitCode}")
                    }
                }
            }
        }
    }
    
    post {
        always {
            archiveArtifacts artifacts: "PLAN_SUMMARY_*.json", allowEmptyArchive: true
        }
    }
}
```

## Output Files

LisaMarge creates several files during execution:

| File | Description | When Created |
|------|-------------|--------------|
| `PLAN_DRAFT_<name>.md` | Initial or revised plan draft | After planner invocation |
| `PLAN_REVIEW_<name>.md` | Reviewer feedback | After reviewer requests revisions |
| `PLAN_FINAL_<name>.md` | Approved final plan | After reviewer approval |
| `PLAN_SUMMARY_<name>.json` | Execution summary | At completion or error |

**Execution Summary Format:**
```json
{
  "start_time": "2026-01-23T12:00:00.000000",
  "end_time": "2026-01-23T12:15:30.000000",
  "iterations_completed": 3,
  "final_state": "done",
  "outcome": "success"
}
```

## Advanced Configuration

### Custom Timeout

Modify timeout in code (default: 600 seconds):
```python
# In invoke_kiro_cli function
result = subprocess.run(
    [kiro_cli_path, "chat", "--message", prompt],
    capture_output=True,
    text=True,
    timeout=1200  # 20 minutes
)
```

### Retry Strategy

Fixed 2-second delay between retries:
- Attempt 1: immediate
- Attempt 2: wait 2 seconds
- Attempt 3: wait 4 seconds (if max-retries=3)

Adjust via `--max-retries` parameter.
