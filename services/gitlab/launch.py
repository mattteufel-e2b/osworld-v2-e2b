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
    GITLAB_URL=https://gitlab.127.0.0.1.nip.io:8090
    GITLAB_TOKEN_FILE=<abs path to gitignored token file>

Run:
    export E2B_API_KEY=$(grep '^E2B_API_KEY=' .env.local | cut -d= -f2)
    uv run --python 3.12 --with e2b --with requests \
        python services/gitlab/launch.py
"""

from __future__ import annotations

import json
import re
import secrets
import shlex
import ssl
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import campaign_tls  # noqa: E402
import fleetlib as fl  # noqa: E402

REPO_URL = "https://github.com/Task-Web/gitlab"
REPO_DIR = "/root/gitlab"
GITLAB_SUBDOMAIN = "gitlab"
GITLAB_PORT = 8929  # distinct sandbox port the fanout publishes for GitLab
TOKEN_FILE = fl.SERVICES_DIR / ".gitlab-token"
RECEIPT = fl.REPO_ROOT / "out" / "osworld-v2-raw" / "services" / "gitlab.json"
LOCKFILE = fl.REPO_ROOT / "examples" / "osworld-v2" / "upstream.lock.json"

HEALTH_CAP_S = 12 * 60  # GitLab boot is typically 3-5 min; cap at 12.


def gitlab_url() -> str:
    return f"http://{GITLAB_SUBDOMAIN}.{fl.HOST_SUFFIX}"


def gitlab_pin() -> str:
    """Validate the release lock and return its immutable GitLab commit."""
    return fl.release_lock()["gitlab_code"]["commit"]


def clone_repo(sbx, commit: str) -> None:
    fl.clone_repo(
        sbx,
        REPO_URL,
        commit,
        REPO_DIR,
        label="gitlab",
        already_cloned_log="gitlab repo already cloned",
        clone_timeout=120,
        checkout_timeout=30,
    )


def write_fanout(sbx) -> None:
    """nginx fanout: publish GITLAB_PORT and forward to the gitlab container on
    its docker network with Host restored to the external_url host, so GitLab
    sees the Host its external_url expects."""
    host = f"{GITLAB_SUBDOMAIN}.{fl.HOST_SUFFIX}"
    pages_host = rf"[a-z0-9](?:[a-z0-9-]{{0,61}}[a-z0-9])?\.{re.escape(host)}"
    conf = (
        "map $http_upgrade $connection_upgrade { default upgrade; '' close; }\n"
        "map $http_x_osworld_pages_host $gitlab_upstream_host {\n"
        f"  default {host};\n"
        f'  "~^{pages_host}$" $http_x_osworld_pages_host;\n'
        "}\n"
        f"server {{\n"
        f"  listen {GITLAB_PORT};\n"
        f"  client_max_body_size 512m;\n"
        f"  location / {{\n"
        f"    resolver 127.0.0.11 valid=10s;\n"
        f"    set $upstream gitlab;\n"
        f"    proxy_pass http://$upstream:80;\n"
        f"    proxy_set_header Host $gitlab_upstream_host;\n"
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
        "  gitlab:\n"
        f"    image: {fl.service_image('gitlab')}\n"
        "  gitlab-init-token:\n"
        f"    image: {fl.service_image('gitlab_init')}\n"
        "  gitlab-runner:\n"
        f"    image: {fl.service_image('gitlab_runner')}\n"
        "  fleet_fanout:\n"
        f"    image: {fl.service_image('fanout')}\n"
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
    already = fl.poll_cmd(
        sbx, "grep -qs COMPOSE_OK /var/log/compose.log && echo done || echo no"
    )
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
    # The compose process can finish during the last sleep. Observe that final
    # state before declaring a timeout and tearing down a healthy sandbox.
    out = fl.poll_cmd(sbx, "tail -3 /var/log/compose.log 2>/dev/null") or ""
    if "COMPOSE_OK" in out:
        fl.log("gitlab compose up (containers created; GitLab still initializing)")
        return
    if "COMPOSE_FAIL" in out:
        full = fl.poll_cmd(sbx, "tail -60 /var/log/compose.log") or ""
        raise RuntimeError(f"gitlab compose up failed:\n{full[-3000:]}")
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
    raise TimeoutError(
        f"gitlab API did not reach 200 within {HEALTH_CAP_S}s (last={last})"
    )


def ensure_runner(sbx, token: str) -> dict:
    """Register once per config volume and require an online, untagged runner."""
    base = f"https://{sbx.get_host(GITLAB_PORT)}/api/v4"
    internal_url = f"http://fleet_fanout:{GITLAB_PORT}"

    def api(method, path, **kwargs):
        response = requests.request(
            method,
            base + path,
            timeout=30,
            headers={
                "PRIVATE-TOKEN": token,
                "e2b-traffic-access-token": sbx.traffic_access_token,
            },
            **kwargs,
        )
        if not response.ok:
            raise RuntimeError(
                f"GitLab runner API {method} failed ({response.status_code})"
            )
        return response

    def command(script, **kwargs):
        # Registration output and SDK exceptions can contain the runner token.
        try:
            result = sbx.commands.run(script, user="root", timeout=60, **kwargs)
            if result.exit_code != 0:
                raise RuntimeError()
            return result.stdout.strip()
        except Exception:
            raise RuntimeError("GitLab runner command failed") from None

    config = "/etc/gitlab-runner/config.toml"
    local_id = command(
        "docker exec gitlab-runner sh -c "
        + shlex.quote(
            f"if [ -f {config} ]; then "
            "awk '/^[[:space:]]*\\[\\[runners\\]\\]/ {configured=1} "
            "/^[[:space:]]*id[[:space:]]*=/ {print $3} "
            'END {if (!configured) print "NONE"}\' '
            f"{config}; else echo NONE; fi"
        )
    )
    created = local_id == "NONE"
    if not created and (not local_id.isdigit() or int(local_id) <= 0):
        raise RuntimeError("GitLab runner config has no unique numeric runner ID")
    if created:
        network = command(
            "docker inspect gitlab-runner --format "
            "'{{range $name, $_ := .NetworkSettings.Networks}}{{$name}}{{\"\\n\"}}{{end}}'"
        )
        if not network or len(network.splitlines()) != 1:
            raise RuntimeError("GitLab runner must have one Compose network")
        registration = api(
            "POST",
            "/user/runners",
            json={
                "runner_type": "instance_type",
                "description": "osworld-docker-runner",
                "tag_list": "osworld,docker",
                "run_untagged": True,
                "locked": False,
                "paused": False,
            },
        ).json()
        runner_id = registration["id"]
    else:
        runner_id = int(local_id)
    try:
        if created:
            backup = (
                f"rm -f {config}.osworld-backup; "
                f"if [ -f {config} ]; then cp -p {config} {config}.osworld-backup; fi"
            )
            command(
                "docker exec gitlab-runner sh -c " + shlex.quote(backup) + " && "
                "docker exec -e RUNNER_TOKEN gitlab-runner gitlab-runner register "
                f"--non-interactive --url {internal_url} --clone-url {internal_url} "
                '--token "$RUNNER_TOKEN" --executor docker --docker-image alpine:3.20 '
                f"--docker-network-mode {shlex.quote(network)} "
                "--description osworld-docker-runner",
                envs={"RUNNER_TOKEN": registration["token"]},
            )
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            state = api("GET", f"/runners/{runner_id}").json()
            if (
                state.get("online") is True
                and state.get("status") == "online"
                and state.get("paused") is False
                and state.get("run_untagged") is True
            ):
                if created:
                    command(f"docker exec gitlab-runner rm -f {config}.osworld-backup")
                return {"id": runner_id, "status": "online", "online": True}
            time.sleep(3)
        raise TimeoutError(
            "GitLab runner did not become online and accept untagged jobs"
        )
    except BaseException:
        if created:
            try:
                api("DELETE", f"/runners/{runner_id}")
            finally:
                restore = (
                    f"if [ -f {config}.osworld-backup ]; then "
                    f"mv {config}.osworld-backup {config}; else rm -f {config}; fi"
                )
                command("docker exec gitlab-runner sh -c " + shlex.quote(restore))
        raise


def main() -> int:
    commit = gitlab_pin()
    campaign = fl.campaign_id()
    fl.load_e2b_key()
    template_ref = fl.ensure_fleet_template()
    sbx, created = fl.reuse_or_create("gitlab", template=template_ref)
    published = False
    try:
        fl.ensure_docker(sbx)
        fl.ensure_swap(sbx)

        # Reuse the token across reruns if one already exists (so the same PAT keeps
        # working for the already-running GitLab); else mint a fresh one.
        existing = fl.read_runtime().get("gitlab", {})
        token = existing.get("private_token") or ("glpat-" + secrets.token_hex(20))

        clone_repo(sbx, commit)
        write_fanout(sbx)
        compose_up(sbx, token)
        ready_secs = wait_api_ready(sbx, token)
        runner = ensure_runner(sbx, token)

        ingress = sbx.get_host(GITLAB_PORT)
        api = requests.get(
            f"https://{ingress}/api/v4/user",
            headers={
                "PRIVATE-TOKEN": token,
                "e2b-traffic-access-token": sbx.traffic_access_token,
            },
            timeout=30,
        )
        unauthenticated = requests.get(
            f"https://{ingress}/api/v4/user",
            headers={"PRIVATE-TOKEN": token},
            timeout=30,
        )
        fl.log(f"host ingress /api/v4/user -> {api.status_code}")
        fl.log(
            f"unauthenticated host ingress /api/v4/user -> {unauthenticated.status_code}"
        )
        fl.require_restricted_ingress(
            "GitLab",
            authenticated_status=api.status_code,
            unauthenticated_status=unauthenticated.status_code,
        )

        # Extends the campaign leaf if the websites launcher already created
        # the CA, or creates the CA itself if GitLab launches first -- either
        # launch order is supported.
        tls = campaign_tls.ensure_campaign_tls(
            campaign,
            [
                f"{GITLAB_SUBDOMAIN}.{fl.HOST_SUFFIX}",
                f"*.{GITLAB_SUBDOMAIN}.{fl.HOST_SUFFIX}",
            ],
        )
        proxy_status = fl.restart_host_proxy()
        if not proxy_status.get("running"):
            raise RuntimeError("host proxy failed to start")
        public_port = proxy_status["port"]
        public_url = f"https://{GITLAB_SUBDOMAIN}.{fl.HOST_SUFFIX}:{public_port}"

        # The proxy reads its route and traffic credential from the runtime file,
        # so publish provisionally, exercise the exact harness URL, and roll back
        # the section if any later gate fails.
        fl.write_runtime_section(
            "gitlab",
            {
                "sandbox_id": sbx.sandbox_id,
                "campaign_id": campaign,
                "template": template_ref,
                "traffic_token": sbx.traffic_access_token,
                "host": f"{GITLAB_SUBDOMAIN}.{fl.HOST_SUFFIX}",
                "ingress_host": ingress,
                "port": GITLAB_PORT,
                "url": public_url,
                "external_url": gitlab_url(),
                # Task 041 hardcodes this public GitLab host; the hostmap
                # proxy routes it here as an alias of our real GitLab host.
                "aliases": [campaign_tls.TASK_041_GITLAB_ALIAS],
                "private_token": token,
                "token_file": str(TOKEN_FILE),
            },
        )
        published = True

        # Exact harness URL: GET {GITLAB_URL}/api/v4/user with only the
        # PRIVATE-TOKEN — the proxy injects the sandbox traffic token. Trusts
        # the campaign CA since the host proxy's leaf is signed by it, not by
        # a public CA.
        import urllib.request

        ssl_context = ssl.create_default_context(cafile=tls["ca_cert"])
        url = f"{public_url}/api/v4/user"
        try:
            req = urllib.request.Request(url, headers={"PRIVATE-TOKEN": token})
            with urllib.request.urlopen(req, timeout=30, context=ssl_context) as r:
                proxy_path_check = {
                    "url": url,
                    "status": r.status,
                    "body_prefix": r.read(160).decode("utf-8", "replace"),
                }
        except Exception as exc:  # noqa: BLE001
            proxy_path_check = {"url": url, "status": None, "error": repr(exc)[:200]}
        fl.log(f"harness-URL /api/v4/user -> {proxy_path_check}")
        if proxy_path_check.get("status") != 200:
            raise RuntimeError("harness URL GitLab API check failed")

        fl.write_private_text(TOKEN_FILE, token + "\n")
        prior, runs = fl.append_launch_receipt(
            RECEIPT,
            {
                "at": datetime.now(UTC).isoformat(),
                "sandbox_created_this_run": created,
                "api_ready_seconds": ready_secs,
            },
        )

        receipt = {
            "schema_version": 2,
            "generated_at": datetime.now(UTC).isoformat(),
            "sandbox_id": sbx.sandbox_id,
            "campaign_id": campaign,
            "template": template_ref,
            "sandbox_created_this_run": created,
            "sandbox_memory_mb": fl.FLEET_MEMORY_MB,
            "restricted_ingress": True,
            "repo": {"url": REPO_URL, "commit": commit},
            "gitlab_image": fl.service_image("gitlab"),
            "gitlab_url": public_url,
            "gitlab_external_url": gitlab_url(),
            "gitlab_port": GITLAB_PORT,
            "runner": runner,
            "ingress_host": ingress,
            "first_boot_api_ready_seconds": prior.get(
                "first_boot_api_ready_seconds", ready_secs
            ),
            "api_ready_seconds": ready_secs,
            "host_ingress_api_status": api.status_code,
            "unauthenticated_host_ingress_api_status": unauthenticated.status_code,
            "host_proxy": proxy_status,
            "harness_url_api_check": proxy_path_check,
            "token_file": str(TOKEN_FILE),
            "token_committed": False,
            "runs": runs,
        }
        RECEIPT.parent.mkdir(parents=True, exist_ok=True)
        RECEIPT.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
        fl.log(f"receipt -> {RECEIPT}")
    except BaseException:
        fl.rollback_launch(
            "gitlab", sbx, created, token_file=TOKEN_FILE, published=published
        )
        raise

    fl.stop_host_proxy()
    print(f"GITLAB_URL={public_url}")
    print(f"GITLAB_TOKEN_FILE={TOKEN_FILE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
