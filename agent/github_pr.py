import os
import json
import urllib.request
import urllib.error
from pathlib import Path
from dotenv import load_dotenv

env_path = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(dotenv_path=env_path)

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GITHUB_REPO = os.getenv("GITHUB_REPO")
BASE_URL = f"https://api.github.com/repos/{GITHUB_REPO}"


def github_request(method: str, endpoint: str, data: dict = None) -> dict:
    """Base GitHub API wrapper using stdlib only"""
    url = f"{BASE_URL}/{endpoint}"
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
        "Content-Type": "application/json"
    }

    body = json.dumps(data).encode("utf-8") if data else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)

    try:
        with urllib.request.urlopen(req) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8")
        print(f"❌ GitHub API error {e.code}: {error_body}")
        return {}


def get_default_branch() -> str:
    """Get the default branch of the repo"""
    result = github_request("GET", "")
    return result.get("default_branch", "main")


def get_branch_sha(branch: str) -> str:
    """Get the latest commit SHA of a branch"""
    result = github_request("GET", f"git/ref/heads/{branch}")
    return result.get("object", {}).get("sha", "")


def branch_exists(branch_name: str) -> bool:
    """Check if a branch already exists"""
    result = github_request("GET", f"git/ref/heads/{branch_name}")
    return bool(result.get("object", {}).get("sha"))


def create_branch(branch_name: str, from_sha: str) -> bool:
    """Create a new branch from a SHA"""
    result = github_request("POST", "git/refs", {
        "ref": f"refs/heads/{branch_name}",
        "sha": from_sha
    })
    return bool(result.get("ref"))


def get_file_sha(file_path: str, branch: str) -> str:
    """Get SHA of existing file (needed to update it)"""
    result = github_request("GET", f"contents/{file_path}?ref={branch}")
    return result.get("sha", "")


def push_file(file_path: str, content: str, branch: str, commit_message: str) -> bool:
    """Push a file to a branch"""
    import base64
    encoded = base64.b64encode(content.encode("utf-8")).decode("utf-8")

    data = {
        "message": commit_message,
        "content": encoded,
        "branch": branch
    }

    # If file exists, include its SHA to update it
    existing_sha = get_file_sha(file_path, branch)
    if existing_sha:
        data["sha"] = existing_sha

    result = github_request("PUT", f"contents/{file_path}", data)
    return bool(result.get("commit"))


def open_pull_request(
    title: str,
    body: str,
    head_branch: str,
    base_branch: str
) -> str:
    """Open a PR and return its URL"""
    result = github_request("POST", "pulls", {
        "title": title,
        "body": body,
        "head": head_branch,
        "base": base_branch
    })
    return result.get("html_url", "")


def create_fix_pr(model_name: str, fixed_sql: str, diagnosis: dict) -> str:
    """
    Full flow:
    1. Create a new branch
    2. Push the fixed SQL file
    3. Open a PR with the diagnosis as description
    Returns the PR URL or empty string on failure
    """

    if not GITHUB_TOKEN or not GITHUB_REPO:
        print("⚠️  GITHUB_TOKEN or GITHUB_REPO not set in .env — skipping PR.")
        return ""

    print(f"\n🔧 Creating auto-fix PR for {model_name}...")

    # Step 1 — get default branch + its SHA
    default_branch = get_default_branch()
    base_sha = get_branch_sha(default_branch)

    if not base_sha:
        print("❌ Could not get base branch SHA.")
        return ""

    # Step 2 — create fix branch
    branch_name = f"fix/dbt-agent-{model_name.replace('_', '-')}"

    if branch_exists(branch_name):
        print(f"⚠️  Branch '{branch_name}' already exists — reusing it.")
    else:
        created = create_branch(branch_name, base_sha)
        if not created:
            print("❌ Could not create fix branch.")
            return ""

    # Step 3 — push the fixed SQL file
    file_path = f"test_project/models/{model_name}.sql"
    commit_msg = f"fix: auto-fix {model_name} — {diagnosis.get('root_cause', 'schema mismatch')}"

    pushed = push_file(file_path, fixed_sql, branch_name, commit_msg)
    if not pushed:
        print("❌ Could not push fixed SQL file.")
        return ""

    # Step 4 — open the PR
    pr_title = f"🤖 [dbt-agent] Auto-fix: {model_name} — {diagnosis.get('severity', '').upper()}"

    pr_body = f"""## 🤖 Auto-generated fix by dbt-agent

### 📌 Root Cause
{diagnosis.get('root_cause', 'N/A')}

### 📁 Affected File
`{diagnosis.get('affected_file', 'N/A')}` → `{diagnosis.get('affected_line', 'N/A')}`

### 💬 Explanation
{diagnosis.get('explanation', 'N/A')}

### 🔧 What was fixed
{diagnosis.get('suggested_fix', 'N/A')}

### ⚠️ Data Loss Risk
{'YES — review carefully before merging' if diagnosis.get('data_loss_risk') else 'No immediate data loss risk'}

---
*This PR was opened automatically by [dbt-agent](https://github.com/{GITHUB_REPO}) after detecting a pipeline failure.*
"""

    pr_url = open_pull_request(pr_title, pr_body, branch_name, default_branch)

    if pr_url:
        print(f"✅ Fix PR opened: {pr_url}")
    else:
        print("❌ Could not open PR.")

    return pr_url