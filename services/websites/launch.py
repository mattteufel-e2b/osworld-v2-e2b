#!/usr/bin/env python3
"""Stand up the OSWorld-V2 website fleet in a long-lived E2B sandbox.

Clones Task-Web/OSWorld-web@v2026.08.08 (with submodules) inside the sandbox,
generates the compose file, adds a per-site Host-injecting nginx fanout so each
site is reachable on a DISTINCT sandbox port (required because E2B ingress
rejects an overridden Host header — see the receipt's addressing_evidence), then
brings the whole stack up behind Caddy and waits for every control-plane site's
/api/state to answer through its published port.

Re-runnable: reuses the recorded sandbox if it is still alive. On success prints
exactly one line to stdout:  WEBSITE_HOST_SUFFIX=127.0.0.1.nip.io:8090
(port-suffixed: V2's build_website_url composes `<site>.<WEBSITE_HOST_SUFFIX>` with
no separate port, so the host-proxy port rides in the suffix itself.)

Run:
    export E2B_API_KEY=$(grep '^E2B_API_KEY=' .env.local | cut -d= -f2)
    uv run --python 3.12 --with e2b --with requests \
        python services/websites/launch.py
"""

from __future__ import annotations

import json
import re
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fleetlib as fl  # noqa: E402

REPO_URL = "https://github.com/Task-Web/OSWorld-web"
REPO_TAG = "v2026.08.08"
REPO_DIR = "/root/OSWorld-web"
CADDY_SCHEME = "http://"
FANOUT_BASE_PORT = 13001

RECEIPT = fl.REPO_ROOT / "out" / "osworld-v2-evidence" / "services-websites.json"

# Sites exposing the /api/state control plane at the pinned tag (from the repo's
# scripts/verify-control-plane.mjs). Used as the readiness gate.
CONTROL_PLANE_SITES = [
    "awsconsole",
    "budgetwise",
    "calendar",
    "careerlink",
    "cloudcrm",
    "dinogame",
    "eventix",
    "expenseflow",
    "formcraft",
    "glbviewer",
    "insurance-claim",
    "mailhub",
    "overleaf-collab",
    "reviewsphere",
    "slidepuzzle",
    "streamview",
    "teamchat",
    "travelhubpro",
    "trippza",
    "vaultbank",
    "visaapplication",
    "wandb",
]


def clone_repo(sbx) -> None:
    check = sbx.commands.run(
        f"test -d {REPO_DIR}/.git && echo yes || echo no", user="root", timeout=15
    )
    if "yes" not in (check.stdout or ""):
        # Submodules are declared with git@ SSH URLs; rewrite to anonymous HTTPS.
        fl.run(
            sbx,
            'git config --global url."https://github.com/".insteadOf "git@github.com:"',
            timeout=15,
        )
        fl.run(
            sbx,
            f"git clone --branch {REPO_TAG} --recurse-submodules {REPO_URL} {REPO_DIR}",
            timeout=900,
        )
    else:
        fl.log("OSWorld-web already cloned")
    fl.run(sbx, f"cd {REPO_DIR} && bash gen-compose.sh", timeout=60)


def enumerate_sites(sbx) -> dict[str, int]:
    """Parse every web-compose.yml caddy label to get the full subdomain set
    (includes multi-host sites like studio.streamview and overleaf), then assign
    a deterministic distinct port per subdomain."""
    res = fl.run(sbx, rf"grep -rhoE 'caddy: *\"[^\"]+\"' {REPO_DIR}/*/web-compose.yml", timeout=30)
    subs: set[str] = set()
    for line in res.stdout.splitlines():
        for host in re.findall(r"\}([a-z0-9.-]+)\.\$\{HOST_SUFFIX", line):
            subs.add(host)
    if not subs:
        raise RuntimeError(
            f"no caddy hostnames parsed from web-compose labels:\n{res.stdout[:500]}"
        )
    ordered = sorted(subs)
    ports = {sub: FANOUT_BASE_PORT + i for i, sub in enumerate(ordered)}
    fl.log(
        f"enumerated {len(ports)} site hostnames -> ports "
        f"{FANOUT_BASE_PORT}..{FANOUT_BASE_PORT + len(ports) - 1}"
    )
    return ports


def write_fanout(sbx, ports: dict[str, int]) -> None:
    blocks = [
        "map $http_upgrade $connection_upgrade { default upgrade; '' close; }",
        "server_names_hash_bucket_size 128;",
        # Several V2 StreamView states are about 1.55 MB. nginx defaults to a
        # 1 MB request body and otherwise rejects their /api/state PUT with 413.
        "client_max_body_size 16m;",
    ]
    for sub, port in sorted(ports.items(), key=lambda kv: kv[1]):
        blocks.append(
            f"server {{\n"
            f"  listen {port};\n"
            f"  location / {{\n"
            f"    resolver 127.0.0.11 valid=10s;\n"
            f"    set $upstream caddy;\n"
            f"    proxy_pass http://$upstream:80;\n"
            f"    proxy_set_header Host {sub}.{fl.HOST_SUFFIX};\n"
            f"    proxy_set_header X-Forwarded-For $remote_addr;\n"
            f"    proxy_set_header X-Forwarded-Proto http;\n"
            f"    proxy_http_version 1.1;\n"
            f"    proxy_set_header Upgrade $http_upgrade;\n"
            f"    proxy_set_header Connection $connection_upgrade;\n"
            f"    proxy_read_timeout 120s;\n"
            f"  }}\n"
            f"}}"
        )
    sbx.files.write(f"{REPO_DIR}/fanout.conf", "\n".join(blocks) + "\n")

    port_lines = "\n".join(f'      - "{p}:{p}"' for p in sorted(ports.values()))
    compose = (
        "services:\n"
        "  fleet_fanout:\n"
        "    image: nginx:alpine\n"
        "    depends_on:\n"
        "      - caddy\n"
        "    networks:\n"
        "      - web\n"
        "    volumes:\n"
        "      - ./fanout.conf:/etc/nginx/conf.d/fanout.conf:ro\n"
        "    ports:\n"
        f"{port_lines}\n"
    )
    sbx.files.write(f"{REPO_DIR}/docker-compose.fanout.yml", compose)
    fl.log("wrote fanout.conf + docker-compose.fanout.yml")


def compose_up(sbx) -> float:
    t0 = time.time()
    # COMPOSE_PARALLEL_LIMIT caps concurrent image builds so 23 node/texlive
    # builds don't exhaust RAM and wedge the sandbox's command daemon.
    cmd = (
        f"cd {REPO_DIR} && HOST_SUFFIX={fl.HOST_SUFFIX} CADDY_SCHEME={CADDY_SCHEME} "
        f"COMPOSE_PARALLEL_LIMIT=2 "
        f"docker compose -f docker-compose.yml -f docker-compose.fanout.yml up -d --build "
        f"&& echo COMPOSE_OK || echo COMPOSE_FAIL"
    )
    already = fl.poll_cmd(sbx, "grep -qs COMPOSE_OK /var/log/compose.log && echo done || echo no")
    if "done" not in (already or ""):
        # Detached: the 23-image build streams for tens of minutes (and saturates
        # the box), so run it in the background and poll the redirected log rather
        # than holding the exec channel open.
        sbx.commands.run(
            f"sh -c '{cmd}' > /var/log/compose.log 2>&1",
            user="root",
            background=True,
            timeout=0,
        )
    deadline = time.time() + 3000  # up to 50 min for 23 image builds
    while time.time() < deadline:
        out = fl.poll_cmd(sbx, "tail -3 /var/log/compose.log 2>/dev/null") or ""
        if "COMPOSE_OK" in out:
            fl.log(f"compose up complete in {time.time() - t0:.0f}s")
            return time.time() - t0
        if "COMPOSE_FAIL" in out:
            full = fl.poll_cmd(sbx, "tail -60 /var/log/compose.log") or ""
            raise RuntimeError(f"docker compose up failed:\n{full[-3000:]}")
        time.sleep(20)
    raise TimeoutError("docker compose up did not finish within the build window")


def recreate_fanout(sbx) -> None:
    """Apply the generated nginx config even when the base compose run is reused."""
    fl.run(
        sbx,
        f"cd {REPO_DIR} && HOST_SUFFIX={fl.HOST_SUFFIX} CADDY_SCHEME={CADDY_SCHEME} "
        "docker compose -f docker-compose.yml -f docker-compose.fanout.yml "
        "up -d --no-deps --force-recreate fleet_fanout",
        timeout=180,
    )
    fl.log("recreated fleet_fanout with current request-body policy")


def wait_ready(sbx, ports: dict[str, int]) -> dict:
    """Poll each control-plane site's /api/state through its published port
    (from inside the sandbox). Returns {site: seconds_to_ready}."""
    timings: dict[str, float] = {}
    t0 = time.time()
    pending = [s for s in CONTROL_PLANE_SITES if s in ports]
    deadline = time.time() + 900
    while pending and time.time() < deadline:
        for site in list(pending):
            port = ports[site]
            code = fl.poll_cmd(
                sbx,
                f"curl -s -o /dev/null -w '%{{http_code}}' "
                f"'http://localhost:{port}/api/state?cookie=readycheck'",
            )
            if (code or "").strip() == "200":
                timings[site] = round(time.time() - t0, 1)
                pending.remove(site)
                fl.log(
                    f"ready: {site} ({timings[site]}s)  [{len(timings)}/{len(CONTROL_PLANE_SITES)}]"
                )
        if pending:
            time.sleep(6)
    if pending:
        raise TimeoutError(f"sites did not report /api/state ready: {pending}")
    return timings


def probe_host_ingress(sbx, ports: dict[str, int], token: str) -> dict:
    """From the OSWorld host, hit mailhub's published port through E2B ingress
    with the correct connection host + token, and record the negative
    caddy-passthrough evidence (overridden Host is rejected at the edge)."""
    port = ports["mailhub"]
    ingress = sbx.get_host(port)
    headers = {"e2b-traffic-access-token": token}
    ok = requests.get(f"https://{ingress}/api/state?cookie=hostprobe", headers=headers, timeout=30)
    passthrough_host = "mailhub." + fl.HOST_SUFFIX
    caddy_host = sbx.get_host(80)
    try:
        pt = requests.get(
            f"https://{caddy_host}/api/state?cookie=hostprobe",
            headers={**headers, "Host": passthrough_host},
            timeout=15,
        )
        passthrough = {"status": pt.status_code, "body": pt.text[:200]}
    except requests.RequestException as exc:
        passthrough = {"status": None, "error": repr(exc)[:200]}
    return {
        "mode": "per-port-fanout",
        "per_port_probe": {
            "site": "mailhub",
            "port": port,
            "ingress_host": ingress,
            "status": ok.status_code,
            "body_prefix": ok.text[:120],
        },
        "caddy_passthrough_probe": {
            "description": "override HTTP Host to the site name while connecting to the "
            "Caddy ingress host; if ingress forwarded it, single-port routing "
            "would work.",
            "sent_host": passthrough_host,
            "connection_host": caddy_host,
            **passthrough,
        },
    }


V2_CHECKOUT = fl.REPO_ROOT / "OSWorld-V2"


def verify_via_v2_builder(public_suffix: str, site: str = "mailhub") -> dict:
    """Import the pinned checkout's desktop_env.controllers.website, let IT
    construct the URL from WEBSITE_HOST_SUFFIX (exactly as the harness will), and
    request /api/state. Runs in a subprocess so website.py's dep set + env are
    isolated. This is the real host-URL proof the reviewer asked for."""
    import subprocess

    snippet = (
        "import os, sys, json\n"
        f"sys.path.insert(0, {str(V2_CHECKOUT)!r})\n"
        f"os.environ['WEBSITE_HOST_SUFFIX'] = {public_suffix!r}\n"
        "from desktop_env.controllers.website import build_website_url\n"
        "import requests\n"
        f"url = build_website_url({site!r})\n"
        "ep = url + '/api/state?cookie=v2builder'\n"
        "try:\n"
        "    r = requests.get(ep, timeout=15)\n"
        "    out = {'constructed_url': url, 'endpoint': ep, "
        "'status': r.status_code, 'body_prefix': r.text[:120]}\n"
        "except Exception as e:\n"
        "    out = {'constructed_url': url, 'endpoint': ep, "
        "'status': None, 'error': repr(e)[:200]}\n"
        "print(json.dumps(out))\n"
    )
    res = subprocess.run(
        [
            "uv",
            "run",
            "--python",
            "3.12",
            "--with",
            "requests",
            "--with",
            "urllib3",
            "--with",
            "python-dotenv",
            "python",
            "-c",
            snippet,
        ],
        capture_output=True,
        text=True,
        cwd=str(fl.REPO_ROOT),
        timeout=180,
    )
    try:
        return json.loads(res.stdout.strip().splitlines()[-1])
    except Exception:  # noqa: BLE001
        return {"status": None, "error": f"builder subprocess failed: {res.stderr[-300:]}"}


def require_v2_builder_success(result: dict) -> None:
    if result.get("status") != 200:
        raise RuntimeError("V2 website routing check failed")


def main() -> int:
    fl.load_e2b_key()
    template_ref = fl.ensure_fleet_template()
    sbx, created = fl.reuse_or_create("websites", template=template_ref)
    docker_secs = fl.ensure_docker(sbx)
    fl.ensure_swap(sbx)
    clone_repo(sbx)
    ports = enumerate_sites(sbx)
    write_fanout(sbx, ports)
    build_secs = compose_up(sbx)
    recreate_fanout(sbx)
    timings = wait_ready(sbx, ports)

    token = sbx.traffic_access_token
    site_map = {
        sub: {"port": port, "ingress_host": sbx.get_host(port)} for sub, port in ports.items()
    }
    host_evidence = probe_host_ingress(sbx, ports, token)

    proxy_status = fl.restart_host_proxy()
    # WEBSITE_HOST_SUFFIX must carry the host-proxy port: V2's build_website_url
    # does host = f"{path}.{HOST_SUFFIX}" with NO port, so a bare suffix yields a
    # :80 URL that is refused here. The port rides in the suffix so the f-string
    # composes a valid host:port authority.
    public_port = proxy_status["port"]
    public_suffix = f"{fl.HOST_SUFFIX}:{public_port}"

    fl.write_runtime_section(
        "websites",
        {
            "sandbox_id": sbx.sandbox_id,
            "template": template_ref,
            "traffic_token": token,
            "host_suffix": fl.HOST_SUFFIX,  # port-less; used for routing/fanout Host
            "public_host_suffix": public_suffix,  # port-suffixed; what the harness consumes
            "caddy_ingress_host": sbx.get_host(80),
            "mode": "per-port-fanout",
            "sites": site_map,
        },
    )

    proxy_path_check = None
    v2_builder_check = None
    if proxy_status.get("running"):
        proxy_path_check = fl.verify_host_proxy_path(
            f"mailhub.{fl.HOST_SUFFIX}", "/api/state?cookie=proxycheck", public_port
        )
        fl.log(f"host proxy path check: {proxy_path_check}")
        v2_builder_check = verify_via_v2_builder(public_suffix, "mailhub")
        require_v2_builder_success(v2_builder_check)
        fl.log(f"V2 build_website_url check: {v2_builder_check}")

    # Preserve first-boot timing across reuse (MINOR review item): keep a runs log.
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
            "compose_build_seconds": round(build_secs, 1),
        }
    )

    receipt = {
        "schema_version": 2,
        "generated_at": datetime.now(UTC).isoformat(),
        "sandbox_id": sbx.sandbox_id,
        "template": template_ref,
        "sandbox_created_this_run": created,
        "sandbox_cpu": fl.FLEET_CPU,
        "sandbox_memory_mb": fl.FLEET_MEMORY_MB,
        "restricted_ingress": True,
        "repo": {"url": REPO_URL, "tag": REPO_TAG},
        "website_host_suffix": public_suffix,
        "website_host_suffix_portless": fl.HOST_SUFFIX,
        "addressing_mode": "per-port-fanout",
        "addressing_evidence": host_evidence,
        "docker_start_seconds": round(docker_secs, 1),
        "first_boot_compose_build_seconds": prior.get(
            "first_boot_compose_build_seconds", round(build_secs, 1)
        ),
        "compose_build_seconds": round(build_secs, 1),
        "site_port_map": {sub: info["port"] for sub, info in site_map.items()},
        "control_plane_readiness_seconds": timings,
        "host_proxy": proxy_status,
        "host_proxy_path_check": proxy_path_check,
        "v2_build_website_url_check": v2_builder_check,
        "guest_proxy_evidence": prior.get("guest_proxy_evidence"),
        "runs": runs,
    }
    RECEIPT.parent.mkdir(parents=True, exist_ok=True)
    RECEIPT.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    fl.log(f"receipt -> {RECEIPT}")

    print(f"WEBSITE_HOST_SUFFIX={public_suffix}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
