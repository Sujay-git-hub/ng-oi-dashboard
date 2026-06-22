"""
push_dashboard.py — Push the generated dashboard to GitHub Pages.

Usage:
    python push_dashboard.py              # one-shot push
    python push_dashboard.py --message "custom commit message"

Requires: git installed, GitHub repo created at:
    https://github.com/Sujay-git-hub/ng-oi-dashboard
"""

import subprocess
import argparse
import shutil
import os
from pathlib import Path
from datetime import datetime

def _load_gh_pat():
    """Load GH_PAT from env or .env file."""
    pat = os.environ.get('GH_PAT', '')
    if not pat:
        env_file = Path(__file__).parent / '.env'
        if env_file.exists():
            for line in env_file.read_text().splitlines():
                if line.startswith('GH_PAT='):
                    pat = line.split('=', 1)[1].strip()
                    break
    return pat

SCRIPT_DIR    = Path(__file__).parent
DASHBOARD_SRC = SCRIPT_DIR / 'output' / 'dashboard.html'
REPO_DIR      = SCRIPT_DIR / '_github_push'
GH_USER       = 'Sujay-git-hub'
GH_REPO       = 'ng-oi-dashboard'
BRANCH        = 'gh-pages'

def _repo_url():
    pat = _load_gh_pat()
    return f'https://{GH_USER}:{pat}@github.com/{GH_USER}/{GH_REPO}.git'

REPO_URL = property(_repo_url)  # computed on use


def run(cmd: list, cwd=None, check=True):
    result = subprocess.run(cmd, cwd=str(cwd) if cwd else None, capture_output=True, text=True)
    if check and result.returncode != 0:
        print(f"  ERROR: {' '.join(cmd)}")
        print(f"  {result.stderr.strip()}")
        raise RuntimeError(result.stderr)
    return result


def push(message: str = None):
    if not DASHBOARD_SRC.exists():
        print("ERROR: output/dashboard.html not found. Run ng_option_chain.py first.")
        return False

    repo_url = _repo_url()
    msg = message or f"OI snapshot {datetime.now().strftime('%Y-%m-%d %H:%M IST')}"

    if not REPO_DIR.exists():
        print(f"  Setting up GitHub repo (first time)...")
        clone = run(
            ['git', 'clone', '--depth=1', '--branch', BRANCH, repo_url, str(REPO_DIR)],
            check=False
        )
        if clone.returncode != 0:
            REPO_DIR.mkdir(parents=True)
            run(['git', 'init'], cwd=REPO_DIR)
            run(['git', 'remote', 'add', 'origin', repo_url], cwd=REPO_DIR)
            run(['git', 'checkout', '--orphan', BRANCH], cwd=REPO_DIR)
            print(f"  Created new gh-pages branch")
    else:
        print(f"  Syncing with remote...")
        # Update remote URL in case PAT changed
        run(['git', 'remote', 'set-url', 'origin', repo_url], cwd=REPO_DIR, check=False)
        run(['git', 'fetch', 'origin', BRANCH], cwd=REPO_DIR, check=False)
        run(['git', 'reset', '--hard', f'origin/{BRANCH}'], cwd=REPO_DIR, check=False)

    shutil.copy2(DASHBOARD_SRC, REPO_DIR / 'index.html')
    shutil.copy2(DASHBOARD_SRC, REPO_DIR / 'dashboard.html')

    run(['git', 'add', 'index.html', 'dashboard.html'], cwd=REPO_DIR)

    diff = run(['git', 'diff', '--cached', '--quiet'], cwd=REPO_DIR, check=False)
    if diff.returncode == 0:
        print("  No changes to push (dashboard unchanged since last push).")
        return True

    run(['git', 'commit', '-m', msg], cwd=REPO_DIR)
    run(['git', 'push', 'origin', BRANCH], cwd=REPO_DIR)
    print(f"  [OK] Pushed: {msg}")
    print(f"  Live URL: https://sujay-git-hub.github.io/ng-oi-dashboard/")
    return True


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--message', '-m', help='Custom commit message')
    args = parser.parse_args()
    push(args.message)
