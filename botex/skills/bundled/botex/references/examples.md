# BoteX Canonical JSON Examples

## 1. Read-Only Code Review (`mode="readonly"`)

`output_path` MUST NOT be set; findings return in `summary`. Apply the Verifier Firewall: pass only files, contract, and checklist.

```json
{
  "task": "Perform a security review of src/auth/jwt.py for token expiration flaws, signature bypasses, and algorithm confusion. Report all findings with line references.",
  "workspace_dir": "C:/Projects/my-app",
  "files": ["src/auth/jwt.py"],
  "mode": "readonly",
  "recipe": "security-reviewer",
  "profile": "fast",
  "api_key": ""
}
```

## 2. Standard Code Modification (`mode="edit"`)

```json
{
  "task": "Add email format validation to UserRegistration in src/auth/register.py using standard library re. Raise ValueError on invalid email.",
  "workspace_dir": "C:/Projects/my-app",
  "files": ["src/auth/register.py"],
  "mode": "edit",
  "profile": "coding",
  "api_key": ""
}
```

## 3. Edit with Verification (`mode="edit"` + `allow_exec=true`)

```json
{
  "task": "Refactor calculate_tax in src/billing.py to handle decimal precision correctly and ensure all unit tests pass.",
  "workspace_dir": "C:/Projects/my-app",
  "files": ["src/billing.py", "tests/test_billing.py"],
  "mode": "edit",
  "verify_command": "pytest tests/test_billing.py",
  "allow_exec": true,
  "profile": "coding",
  "api_key": ""
}
```

## 4. Asynchronous Delegation (`start_task` -> `get_task_status`)

### Step A: `start_task`
```json
{
  "task": "Generate migration scripts for user schema changes under src/db/migrations/",
  "workspace_dir": "C:/Projects/my-app",
  "files": ["src/db/migrations/"],
  "mode": "edit",
  "profile": "coding",
  "api_key": ""
}
```

### Step B: `get_task_status`
```json
{
  "task_id": "task_20261029_9f3a12b4"
}
```

Final verified response:
```json
{
  "task_id": "task_20261029_9f3a12b4",
  "status": "DONE",
  "result_data": {
    "ok": true,
    "status": "DONE",
    "summary": "Generated migration 0004_user_schema_v2.py under src/db/migrations/.",
    "files_touched": ["src/db/migrations/0004_user_schema_v2.py"],
    "lines_added": 42,
    "lines_removed": 0,
    "duration_s": 18.7,
    "cost_usd": 0.0028
  }
}
```
