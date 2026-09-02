#!/usr/bin/env python3
"""Stand up the OSWorld-V2 GitLab service in a long-lived E2B sandbox.

Clones Task-Web/gitlab (gitlab-ce 18.7.0) inside the sandbox, brings it up with a
run-local root personal-access token, fronts it with a Host-injecting nginx
fanout on a distinct sandbox port (so the Host-mapping proxy reaches it the same
way it reaches the websites), and waits for the GitLab API to answer.

Secrets: the private token is random per run, written only to a gitignored file
(services/.gitlab-token) and the gitignored runtime file — never to the receipt
or any committed file.

Re-runnable: reuses the recorded sandbox if it is still alive. On success prints
exactly these lines to stdout:
    GITLAB_URL=http://gitlab.127.0.0.1.nip.io:8090
    GITLAB_TOKEN_FILE=<abs path to gitignored token file>

Run:
    export E2B_API_KEY=$(grep '^E2B_API_KEY=' .env.local | cut -d= -f2)
    uv run --python 3.12 --with e2b --with requests \
        python services/gitlab/launch.py
"""

from __future__ import annotations

import json
import secrets
import shlex
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fleetlib as fl  # noqa: E402

REPO_URL = "https://github.com/Task-Web/gitlab"
REPO_DIR = "/root/gitlab"
GITLAB_SUBDOMAIN = "gitlab"
GITLAB_PORT = 8929  # distinct sandbox port the fanout publishes for GitLab
TOKEN_FILE = fl.SERVICES_DIR / ".gitlab-token"
RECEIPT = fl.REPO_ROOT / "out" / "osworld-v2-evidence" / "services-gitlab.json"
LOCKFILE = fl.REPO_ROOT / "examples" / "osworld-v2" / "upstream.lock.json"

HEALTH_CAP_S = 12 * 60  # GitLab boot is typically 3-5 min; cap at 12.


def gitlab_url() -> str:
    return f"http://{GITLAB_SUBDOMAIN}.{fl.HOST_SUFFIX}"


def gitlab_pin() -> str:
    """Read the pinned commit straight from the lock file (no hardcoded
    duplicate here): examples/osworld-v2/upstream.lock.json gitlab_code.commit."""
    return json.loads(LOCKFILE.read_text())["gitlab_code"]["commit"]


def clone_repo(sbx) -> None:
    check = sbx.commands.run(
        f"test -d {REPO_DIR}/.git && echo yes || echo no", user="root", timeout=15
    )
    if "yes" not in (check.stdout or ""):
        fl.run(sbx, f"git clone {REPO_URL} {REPO_DIR}", timeout=120)
    else:
        fl.log("gitlab repo already cloned")
    commit = gitlab_pin()
    fl.run(sbx, f"git -C {REPO_DIR} checkout {commit}", timeout=30)
    fl.log(f"gitlab repo pinned to {commit}")


def write_fanout(sbx) -> None:
    """nginx fanout: publish GITLAB_PORT and forward to the gitlab container on
    its docker network with Host restored to the external_url host, so GitLab
    sees the Host its external_url expects."""
    conf = (
        "map $http_upgrade $connection_upgrade { default upgrade; '' close; }\n"
        f"server {{\n"
        f"  listen {GITLAB_PORT};\n"
        f"  client_max_body_size 512m;\n"
        f"  location / {{\n"
        f"    resolver 127.0.0.11 valid=10s;\n"
        f"    set $upstream gitlab;\n"
        f"    proxy_pass http://$upstream:80;\n"
        f"    proxy_set_header Host {GITLAB_SUBDOMAIN}.{fl.HOST_SUFFIX};\n"
        f"    proxy_set_header X-Forwarded-For $remote_addr;\n"
        f"    proxy_set_header X-Forwarded-Proto http;\n"
        f"    proxy_http_version 1.1;\n"
        f"    proxy_set_header Upgrade $http_upgrade;\n"
        f"    proxy_set_header Connection $connection_upgrade;\n"
        f"    proxy_read_timeout 300s;\n"
        f"  }}\n"
        f"}}\n"
    )
    sbx.files.write(f"{REPO_DIR}/fanout.conf", conf)
    compose = (
        "services:\n"
        "  fleet_fanout:\n"
        "    image: nginx:alpine\n"
        "    depends_on:\n"
        "      - gitlab\n"
        "    volumes:\n"
        "      - ./fanout.conf:/etc/nginx/conf.d/fanout.conf:ro\n"
        "    ports:\n"
        f'      - "{GITLAB_PORT}:{GITLAB_PORT}"\n'
    )
    sbx.files.write(f"{REPO_DIR}/docker-compose.fanout.yml", compose)
    fl.log(f"wrote gitlab fanout on port {GITLAB_PORT}")


def compose_up(sbx, token: str) -> None:
    envs = {"GITLAB_URL": gitlab_url(), "GITLAB_PRIVATE_TOKEN": token}
    cmd = (
        f"cd {REPO_DIR} && "
        f"docker compose -f docker-compose.yml -f docker-compose.fanout.yml up -d "
        f"&& echo COMPOSE_OK || echo COMPOSE_FAIL"
    )
    # Detached: the gitlab-ce image pull is large; poll the log instead of holding
    # the exec channel open.
    already = fl.poll_cmd(sbx, "grep -qs COMPOSE_OK /var/log/compose.log && echo done || echo no")
    if "done" not in (already or ""):
        sbx.commands.run(
            f"sh -c {shlex.quote(cmd)} > /var/log/compose.log 2>&1",
            user="root",
            background=True,
            timeout=0,
            envs=envs,
        )
    deadline = time.time() + 900  # image pull + container create
    while time.time() < deadline:
        out = fl.poll_cmd(sbx, "tail -3 /var/log/compose.log 2>/dev/null") or ""
        if "COMPOSE_OK" in out:
            fl.log("gitlab compose up (containers created; GitLab still initializing)")
            return
        if "COMPOSE_FAIL" in out:
            full = fl.poll_cmd(sbx, "tail -60 /var/log/compose.log") or ""
            raise RuntimeError(f"gitlab compose up failed:\n{full[-3000:]}")
        time.sleep(15)
    raise TimeoutError("gitlab compose up did not create containers in time")


def wait_api_ready(sbx, token: str) -> float:
    """Poll /api/v4/user with the PRIVATE-TOKEN through the fanout port until 200
    (proves both GitLab is up and the init-token PAT exists). Returns seconds."""
    t0 = time.time()
    deadline = time.time() + HEALTH_CAP_S
    last = None
    while time.time() < deadline:
        code = (
            fl.poll_cmd(
                sbx,
                f"curl -s -o /dev/null -w '%{{http_code}}' "
                f'-H "PRIVATE-TOKEN: $GITLAB_PRIVATE_TOKEN" '
                f"http://localhost:{GITLAB_PORT}/api/v4/user",
                envs={"GITLAB_PRIVATE_TOKEN": token},
            )
            or ""
        ).strip()
        if code != last:
            fl.log(f"gitlab /api/v4/user -> {code} ({time.time() - t0:.0f}s)")
            last = code
        if code == "200":
            return round(time.time() - t0, 1)
        time.sleep(15)
    raise TimeoutError(f"gitlab API did not reach 200 within {HEALTH_CAP_S}s (last={last})")


def main() -> int:
    fl.load_e2b_key()
    template_ref = fl.ensure_fleet_template()
    sbx, created = fl.reuse_or_create("gitlab", template=template_ref)
    fl.ensure_docker(sbx)
    fl.ensure_swap(sbx)

    # Reuse the token across reruns if one already exists (so the same PAT keeps
    # working for the already-running GitLab); else mint a fresh one.
    existing = fl.read_runtime().get("gitlab", {})
    token = existing.get("private_token") or ("glpat-" + secrets.token_hex(20))

    clone_repo(sbx)
    write_fanout(sbx)
    compose_up(sbx, token)
    ready_secs = wait_api_ready(sbx, token)

    ingress = sbx.get_host(GITLAB_PORT)
    # Host-path verification through E2B ingress: the restricted-ingress gate
    # requires the sandbox traffic token (exactly what the Host-mapping proxy
    # injects), plus the GitLab PRIVATE-TOKEN for the API itself.
    api = requests.get(
        f"https://{ingress}/api/v4/user",
        headers={
            "PRIVATE-TOKEN": token,
            "e2b-traffic-access-token": sbx.traffic_access_token,
        },
        timeout=30,
    )
    fl.log(f"host ingress /api/v4/user -> {api.status_code}")

    TOKEN_FILE.write_text(token + "\n")
    TOKEN_FILE.chmod(0o600)

    proxy_status = fl.restart_host_proxy()
    # The harness reads GITLAB_URL verbatim (gitlab.py: os.getenv('GITLAB_URL')),
    # so it must carry the host-proxy port. GitLab's own external_url (set in the
    # compose env) stays PORT-LESS so its internal nginx binds :80 and the fanout
    # -> gitlab:80 keeps working.
    public_port = proxy_status["port"]
    public_url = f"http://{GITLAB_SUBDOMAIN}.{fl.HOST_SUFFIX}:{public_port}"

    fl.write_runtime_section(
        "gitlab",
        {
            "sandbox_id": sbx.sandbox_id,
            "template": template_ref,
            "traffic_token": sbx.traffic_access_token,
            "host": f"{GITLAB_SUBDOMAIN}.{fl.HOST_SUFFIX}",
            "ingress_host": ingress,
            "port": GITLAB_PORT,
            "url": public_url,  # harness-facing, port-suffixed
            "external_url": gitlab_url(),  # GitLab's own external_url (port-less)
            "private_token": token,
            "token_file": str(TOKEN_FILE),
        },
    )

    proxy_path_check = None
    if proxy_status.get("running"):
        # Exact harness URL: GET {GITLAB_URL}/api/v4/user with only the
        # PRIVATE-TOKEN — the proxy injects the sandbox traffic token.
        import urllib.request

        url = f"{public_url}/api/v4/user"
        try:
            req = urllib.request.Request(url, headers={"PRIVATE-TOKEN": token})
            with urllib.request.urlopen(req, timeout=30) as r:
                proxy_path_check = {
                    "url": url,
                    "status": r.status,
                    "body_prefix": r.read(160).decode("utf-8", "replace"),
                }
        except Exception as exc:  # noqa: BLE001
            proxy_path_check = {"url": url, "status": None, "error": repr(exc)[:200]}
        fl.log(f"harness-URL /api/v4/user -> {proxy_path_check}")

    prior = {}
    if RECEIPT.is_file():
        try:
            prior = json.loads(RECEIPT.read_text())
        except json.JSONDecodeError:
            prior = {}
    runs = list(prior.get("runs", []))
    runs.append(
        {
            "at": datetime.now(UTC).isoformat(),
            "sandbox_created_this_run": created,
            "api_ready_seconds": ready_secs,
        }
    )

    receipt = {
        "schema_version": 2,
        "generated_at": datetime.now(UTC).isoformat(),
        "sandbox_id": sbx.sandbox_id,
        "template": template_ref,
        "sandbox_created_this_run": created,
        "sandbox_memory_mb": fl.FLEET_MEMORY_MB,
        "restricted_ingress": True,
        "repo": {"url": REPO_URL},
        "gitlab_image": "gitlab/gitlab-ce:18.7.0-ce.0",
        "gitlab_url": public_url,
        "gitlab_external_url": gitlab_url(),
        "gitlab_port": GITLAB_PORT,
        "ingress_host": ingress,
        "first_boot_api_ready_seconds": prior.get("first_boot_api_ready_seconds", ready_secs),
        "api_ready_seconds": ready_secs,
        "host_ingress_api_status": api.status_code,
        "host_proxy": proxy_status,
        "harness_url_api_check": proxy_path_check,
        "token_file": str(TOKEN_FILE),
        "token_committed": False,
        "runs": runs,
    }
    RECEIPT.parent.mkdir(parents=True, exist_ok=True)
    RECEIPT.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    fl.log(f"receipt -> {RECEIPT}")

    print(f"GITLAB_URL={public_url}")
    print(f"GITLAB_TOKEN_FILE={TOKEN_FILE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
