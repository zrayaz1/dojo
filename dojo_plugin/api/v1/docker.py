import base64
import datetime
import hashlib
import hmac
import json
import logging
import os
import pathlib
import re
import secrets
import threading
import time
import uuid

import docker
import docker.errors
import docker.types
import redis
from flask import abort, request, current_app
from flask_restx import Namespace, Resource
from CTFd.cache import cache
from CTFd.models import Users, Solves
from CTFd.utils.user import get_current_user, is_admin
from CTFd.utils.decorators import authed_only
from CTFd.exceptions import UserNotFoundException, UserTokenExpiredException

from ...config import HOST_DATA_PATH, INTERNET_FOR_ALL, SECCOMP, USER_FIREWALL_ALLOWED, WORKSPACE_SECRET
from ...models import DojoModules, DojoChallenges
from ...utils import (
    container_name,
    lookup_workspace_token,
    resolved_tar,
    serialize_user_flag,
    user_docker_client,
    user_node,
    user_ipv4,
    get_current_container,
    is_challenge_locked,
)
from ...utils.dojo import dojo_accessible, get_current_dojo_challenge
from ...utils.workspace import exec_run
from ...utils.feed import publish_container_start
from ...utils.request_logging import get_trace_id, log_generator_output
from ...pages.workspace import forward_port

logger = logging.getLogger(__name__)

docker_namespace = Namespace(
    "docker", description="Endpoint to manage docker containers"
)

HOST_HOMES = pathlib.Path(HOST_DATA_PATH) / "workspace" / "homes"
HOST_HOMES_MOUNTS = HOST_HOMES / "mounts"
HOST_HOMES_OVERLAYS = HOST_HOMES / "overlays"

JOB_REDIS_PREFIX = os.environ.get("DOCKER_JOB_PREFIX", "dojo:docker_job:")
JOB_TTL_SECONDS = int(os.environ.get("DOCKER_JOB_TTL", "900"))
DEFAULT_WORKSPACE_PORT = "80"


def _job_meta_key(job_id):
    return f"{JOB_REDIS_PREFIX}{job_id}"


def _redis_client():
    return redis.from_url(current_app.config["REDIS_URL"])


def _load_job(job_id):
    payload = _redis_client().get(_job_meta_key(job_id))
    if not payload:
        return None
    return json.loads(payload)


def _save_job(job):
    job["updated_at"] = time.time()
    _redis_client().set(
        _job_meta_key(job["id"]),
        json.dumps(job),
        ex=JOB_TTL_SECONDS,
    )
    return job


def _update_job(job_id, **updates):
    job = _load_job(job_id)
    if not job:
        return None
    job.update(updates)
    return _save_job(job)


def _workspace_job_url(job_id, token):
    workspace_host = os.environ.get("WORKSPACE_HOST")
    if not workspace_host:
        return None
    forwarded_proto = request.headers.get("X-Forwarded-Proto")
    scheme = forwarded_proto or request.scheme
    base = f"{scheme}://{workspace_host}"
    return f"{base}/workspace/job/{job_id}/{token}"


def _workspace_redirect(user, container, port=DEFAULT_WORKSPACE_PORT):
    if not WORKSPACE_SECRET:
        raise RuntimeError("WORKSPACE_SECRET is not configured")

    container_id = container.id[:12]
    message = container_id
    node = user_node(user)
    if node not in (None, 0):
        message = f"{container_id}:192.168.42.{node + 1}"

    digest = hmac.new(
        WORKSPACE_SECRET.encode(),
        message.encode(),
        hashlib.sha256,
    ).digest()
    signature = base64.urlsafe_b64encode(digest).decode()
    return forward_port(
        port=port,
        signature=signature,
        message=message,
        user=user,
    )


def _initialize_job(user, as_user, dojo_challenge, practice):
    job_id = uuid.uuid4().hex
    job_token = secrets.token_urlsafe(32)
    now = time.time()
    module = dojo_challenge.module
    dojo = dojo_challenge.dojo
    job = {
        "id": job_id,
        "token": job_token,
        "user_id": user.id,
        "user_name": user.name,
        "as_user_id": as_user.id if as_user else None,
        "as_user_name": (as_user.name if as_user else None),
        "dojo_id": dojo.id,
        "dojo_reference": dojo.reference_id,
        "dojo_name": dojo.name,
        "module_id": module.id if module else None,
        "module_name": module.name if module else None,
        "challenge_id": dojo_challenge.id,
        "challenge_name": dojo_challenge.name,
        "practice": bool(practice),
        "state": "pending",
        "workspace_url": None,
        "error": None,
        "created_at": now,
        "updated_at": now,
    }
    _save_job(job)
    job_url = _workspace_job_url(job_id, job_token)
    job["job_url"] = job_url
    return job


def remove_container(user):
    # Just in case our container is still running on the other docker container, let's make sure we try to kill both
    known_image_name = cache.get(f"user_{user.id}-running-image")
    images = [None, known_image_name]
    for image_name in images:
        docker_client = user_docker_client(user, image_name)
        try:
            container = docker_client.containers.get(container_name(user))
            container.remove(force=True)
            container.wait(condition="removed")
        except (docker.errors.NotFound, docker.errors.APIError):
            pass
        for volume in [f"{user.id}", f"{user.id}-overlay"]:
            try:
                docker_client.volumes.get(volume).remove()
            except (docker.errors.NotFound, docker.errors.APIError):
                pass

def get_available_devices(docker_client):
    key = f"devices-{docker_client.api.base_url}"
    if (cached := cache.get(key)) is not None:
        return cached
    find_command = ["/bin/find", "/dev", "-type", "c"]
    # When using certain logging drivers (like Splunk), docker.containers.run() returns None
    # Use detach=True and logs() to capture output instead
    container = docker_client.containers.run("busybox:uclibc", find_command, privileged=True, detach=True)
    container.wait()
    output = container.logs()
    container.remove()
    devices = output.decode().splitlines() if output else []
    timeout = int(datetime.timedelta(days=1).total_seconds())
    cache.set(key, devices, timeout=timeout)
    return devices

def start_container(docker_client, user, as_user, user_mounts, dojo_challenge, practice):
    resolved_dojo_challenge = dojo_challenge.resolve()

    start_time = time.time()
    hostname = "~".join(
        (["practice"] if practice else [])
        + [
            dojo_challenge.module.id,
            re.sub(
                r"[\s.-]+",
                "-",
                re.sub(r"[^a-z0-9\s.-]", "", dojo_challenge.name.lower()),
            ),
        ]
    )[:64]

    auth_token = os.urandom(32).hex()

    challenge_bin_path = "/run/challenge/bin"
    dojo_bin_path = "/run/dojo/bin"
    image = docker_client.images.get(resolved_dojo_challenge.image)
    image_env = image.attrs["Config"].get("Env") or []
    image_path = next((env_var[len("PATH="):].split(":") for env_var in image_env if env_var.startswith("PATH=")), [])
    env_path = ":".join([challenge_bin_path, dojo_bin_path, *image_path])

    mounts = [
        docker.types.Mount(
            "/nix",
            f"{HOST_DATA_PATH}/workspace/nix",
            "bind",
            read_only=True,
        ),
        docker.types.Mount(
            "/run/dojo/sys",
            "/run/dojo/dojofs",
            "bind",
            read_only=True,
            propagation="slave",
        ),
        *user_mounts,
    ]

    allowed_devices = ["/dev/kvm", "/dev/net/tun"]
    available_devices = set(get_available_devices(docker_client))
    devices = [f"{device}:{device}:rwm" for device in allowed_devices if device in available_devices]

    capabilities = ["SYS_PTRACE"]
    if resolved_dojo_challenge.privileged:
        capabilities.append("SYS_ADMIN")
        if "workspace_net_admin" in resolved_dojo_challenge.dojo.permissions:
            capabilities.append("NET_ADMIN")

    container_create_attributes = dict(
        image=resolved_dojo_challenge.image,
        entrypoint=[
            "/nix/var/nix/profiles/dojo-workspace/bin/dojo-init",
            f"{dojo_bin_path}/sleep",
            "6h",
        ],
        name=container_name(user),
        hostname=hostname,
        user="0",
        working_dir="/home/hacker",
        environment={
            "HOME": "/home/hacker",
            "PATH": env_path,
            "SHELL": f"{dojo_bin_path}/bash",
            "DOJO_AUTH_TOKEN": auth_token,
        },
        labels={
            "dojo.dojo_id": dojo_challenge.dojo.reference_id,
            "dojo.module_id": dojo_challenge.module.id,
            "dojo.challenge_id": dojo_challenge.id,
            "dojo.challenge_description": dojo_challenge.description,
            "dojo.user_id": str(user.id),
            "dojo.as_user_id": str(as_user.id),
            "dojo.auth_token": auth_token,
            "dojo.mode": "privileged" if practice else "standard",
        },
        mounts=mounts,
        devices=devices,
        network=None,
        extra_hosts={
            hostname: "127.0.0.1",
            "vm": "127.0.0.1",
            f"vm_{hostname}"[:64]: "127.0.0.1",
            "challenge.localhost": "127.0.0.1",
            "hacker.localhost": "127.0.0.1",
            "dojo-user": user_ipv4(user),
            **USER_FIREWALL_ALLOWED,
        },
        init=True,
        detach=True,
        stdin_open=True,
        auto_remove=True,
        cpu_period=100000,
        cpu_quota=400000,
        pids_limit=1024,
        mem_limit="4G",
        runtime="io.containerd.run.kata.v2" if resolved_dojo_challenge.privileged else "runc",
        cap_add=capabilities,
        security_opt=[f"seccomp={SECCOMP}"],
        sysctls={"net.ipv4.ip_unprivileged_port_start": 1024},
    )

    container = docker_client.containers.create(**container_create_attributes)

    workspace_net = docker_client.networks.get("workspace_net")
    workspace_net.connect(
        container, ipv4_address=user_ipv4(user), aliases=[container_name(user)]
    )

    default_network = docker_client.networks.get("bridge")
    internet_access = INTERNET_FOR_ALL or any(
        award.name == "INTERNET" for award in user.awards
    )
    if not internet_access:
        default_network.disconnect(container)

    container.start()
    logger.info(f"container started after {time.time()-start_time:.1f} seconds")
    for message in log_generator_output(
        "workspace initialization ", container.logs(stream=True, follow=True), start_time=start_time
    ):
        if b"DOJO_INIT_INITIALIZED" in message or message == b"Initialized.\n":
            logger.info(f"workspace initialized after {time.time()-start_time:.1f} seconds")
            break
    else:
        raise RuntimeError(f"Workspace failed to initialize after {time.time()-start_time:.1f} seconds.")

    cache.set(f"user_{user.id}-running-image", resolved_dojo_challenge.image, timeout=0)
    return container


def insert_challenge(container, as_user, dojo_challenge):
    def is_option_path(path):
        path = pathlib.Path(*path.parts[: len(dojo_challenge.path.parts) + 1])
        return path.name.startswith("_") and path.is_dir()

    exec_run("/run/dojo/bin/mkdir -p /challenge", container=container)

    root_dir = dojo_challenge.path.parent.parent
    challenge_tar = resolved_tar(
        dojo_challenge.path,
        root_dir=root_dir,
        filter=lambda path: not is_option_path(path),
    )
    container.put_archive("/challenge", challenge_tar)

    option_paths = sorted(
        path for path in dojo_challenge.path.iterdir() if is_option_path(path)
    )
    if option_paths:
        secret = current_app.config["SECRET_KEY"]
        option_hash = hashlib.sha256(
            f"{secret}_{as_user.id}_{dojo_challenge.challenge_id}".encode()
        ).digest()
        option = option_paths[
            int.from_bytes(option_hash[:8], "little") % len(option_paths)
        ]
        container.put_archive("/challenge", resolved_tar(option, root_dir=root_dir))

    exec_run(
        r"/run/dojo/bin/find /challenge/ -mindepth 1 -exec /run/dojo/bin/chown root:root {} \;", container=container
    )
    exec_run(r"/run/dojo/bin/find /challenge/ -mindepth 1 -exec /run/dojo/bin/chmod 4755 {} \;", container=container)


def insert_flag(container, flag):
    flag = f"pwn.college{{{flag}}}"
    if "localhost" in container.client.api.base_url:
        socket = container.attach_socket(params=dict(stdin=1, stream=1))
        socket._sock.sendall(flag.encode() + b"\n")
        socket.close()
    else:
        ws = container.attach_socket(params=dict(stdin=1, stream=1), ws=True)
        ws.send_text(f"{flag}\n")
        ws.close()


def _run_challenge_job(app, job_id, user_id, as_user_id, dojo_challenge_id, practice):
    with app.app_context():
        job = _update_job(job_id, state="running")
        if not job:
            logger.warning("Job %s disappeared before it could start", job_id)
            return

        user = Users.query.get(user_id)
        as_user = Users.query.get(as_user_id) if as_user_id else None
        dojo_challenge = DojoChallenges.query.get(dojo_challenge_id)

        if not user or not dojo_challenge:
            _update_job(
                job_id,
                state="error",
                error="Workspace request is no longer valid.",
                finished_at=time.time(),
            )
            return

        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            try:
                logger.info(
                    "Async start job %s for user %s (attempt %s/%s)",
                    job_id,
                    user.id,
                    attempt,
                    max_attempts,
                )
                container = start_challenge(
                    user,
                    dojo_challenge,
                    practice,
                    as_user=as_user,
                )

                workspace_target = _workspace_redirect(as_user or user, container)
                _update_job(
                    job_id,
                    state="ready",
                    workspace_url=workspace_target,
                    finished_at=time.time(),
                )

                dojo = dojo_challenge.dojo
                if dojo.official or dojo.data.get("type") == "public":
                    challenge_data = {
                        "challenge_id": dojo_challenge.challenge_id,
                        "challenge_name": dojo_challenge.name,
                        "module_id": dojo_challenge.module.id
                        if dojo_challenge.module
                        else None,
                        "module_name": dojo_challenge.module.name
                        if dojo_challenge.module
                        else None,
                        "dojo_id": dojo.reference_id,
                        "dojo_name": dojo.name,
                    }
                    mode = "practice" if practice else "assessment"
                    actual_user = as_user or user
                    publish_container_start(actual_user, mode, challenge_data)

                return
            except Exception as exc:
                logger.warning(
                    "Attempt %s for job %s failed for user %s: %s",
                    attempt,
                    job_id,
                    user.id,
                    exc,
                )
                if attempt < max_attempts:
                    time.sleep(2)

        _update_job(
            job_id,
            state="error",
            error="Workspace failed to start. Please retry.",
            finished_at=time.time(),
        )
        logger.error("Docker failed for user %s (job %s)", user.id, job_id)


def start_challenge(user, dojo_challenge, practice, *, as_user=None):
    docker_client = user_docker_client(user, image_name=dojo_challenge.image)
    node_id = user_node(user)
    if node_id is None:
        node_id = -1
    logger.info(f"starting challenge dojo={
        dojo_challenge.dojo.reference_id
    } module={dojo_challenge.module.id} challenge={dojo_challenge.id} {practice=} {as_user=} node_id={node_id+1}")
    remove_container(user)

    user_mounts = []
    if as_user is None:
        user_mounts.append(
            docker.types.Mount(
                "/home/hacker",
                str(user.id),
                "volume",
                no_copy=True,
                driver_config=docker.types.DriverConfig("homefs", options=dict(trace_id=get_trace_id())),
            )
        )
    else:
        user_mounts.extend([
            docker.types.Mount(
                "/home/hacker",
                f"{user.id}-overlay",
                "volume",
                no_copy=True,
                driver_config=docker.types.DriverConfig("homefs", options=dict(overlay=str(as_user.id), trace_id=get_trace_id())),
            ),
            docker.types.Mount(
                "/home/me",
                str(user.id),
                "volume",
                no_copy=True,
                driver_config=docker.types.DriverConfig("homefs", options=dict(trace_id=get_trace_id())),
            ),
        ])

    as_user = as_user or user

    start_time = time.time()
    container = start_container(
        docker_client=docker_client,
        user=user,
        as_user=as_user,
        user_mounts=user_mounts,
        dojo_challenge=dojo_challenge,
        practice=practice,
    )

    if dojo_challenge.path.exists():
        insert_challenge(container, as_user, dojo_challenge)

    if practice:
        flag = "practice"
    elif as_user != user:
        flag = "support_flag"
    else:
        flag = serialize_user_flag(as_user.id, dojo_challenge.challenge_id)
    insert_flag(container, flag)

    for message in log_generator_output(
        "workspace readying ", container.logs(stream=True, follow=True), start_time=start_time
    ):
        if b"DOJO_INIT_READY" in message or message == b"Ready.\n":
            logger.info(f"workspace ready after {time.time()-start_time:.1f} seconds")
            break
        if b"DOJO_INIT_FAILED:" in message:
            cause = message.split(b"DOJO_INIT_FAILED:")[1].split(b"\n")[0]
            raise RuntimeError(f"DOJO_INIT_FAILED: {cause}")
    else:
        raise RuntimeError(f"Workspace failed to become ready.")

    return container

def docker_locked(func):
    def wrapper(*args, **kwargs):
        user = get_current_user()
        redis_client = redis.from_url(current_app.config["REDIS_URL"])
        try:
            with redis_client.lock(f"user.{user.id}.docker.lock",
                                   blocking_timeout=0,
                                   timeout=20,
                                   raise_on_release_error=False):
                return func(*args, **kwargs)
        except redis.exceptions.LockError:
            return {"success": False, "error": "Already starting a challenge; try again in 20 seconds."}
    return wrapper




@docker_namespace.route("/next")
class NextChallenge(Resource):
    @authed_only
    def get(self):
        dojo_challenge = get_current_dojo_challenge()
        if not dojo_challenge:
            return {"success": False, "error": "No active challenge"}

        user = get_current_user()

        # Get all challenges in the current module
        module_challenges = DojoChallenges.query.filter_by(
            dojo_id=dojo_challenge.dojo_id,
            module_index=dojo_challenge.module_index
        ).order_by(DojoChallenges.challenge_index).all()

        # Find the current challenge index
        current_idx = next((i for i, c in enumerate(module_challenges) if c.challenge_index == dojo_challenge.challenge_index), None)

        if current_idx is None:
            return {"success": False, "error": "Current challenge not found in module"}

        # Check if there's a next challenge in the current module
        if current_idx + 1 < len(module_challenges):
            next_challenge = module_challenges[current_idx + 1]
            return {
                "success": True,
                "dojo": dojo_challenge.dojo.reference_id,
                "module": next_challenge.module.id,
                "challenge": next_challenge.id,
                "challenge_index": next_challenge.challenge_index
            }

        # Check if there's a next module
        next_module = DojoModules.query.filter_by(
            dojo_id=dojo_challenge.dojo_id,
            module_index=dojo_challenge.module_index + 1
        ).first()

        if next_module:
            # Get the first challenge of the next module
            first_challenge = DojoChallenges.query.filter_by(
                dojo_id=dojo_challenge.dojo_id,
                module_index=next_module.module_index
            ).order_by(DojoChallenges.challenge_index).first()

            if first_challenge:
                return {
                    "success": True,
                    "dojo": dojo_challenge.dojo.reference_id,
                    "module": first_challenge.module.id,
                    "challenge": first_challenge.id,
                    "challenge_index": first_challenge.challenge_index,
                    "new_module": True
                }

        # No next challenge available
        return {"success": False, "error": "No next challenge available"}


@docker_namespace.route("")
class RunDocker(Resource):
    @authed_only
    @docker_locked
    def post(self):
        data = request.get_json()
        dojo_id = data.get("dojo")
        module_id = data.get("module")
        challenge_id = data.get("challenge")
        practice = data.get("practice")

        user = get_current_user()
        as_user = None

        # https://github.com/CTFd/CTFd/blob/3.6.0/CTFd/utils/initialization/__init__.py#L286-L296
        workspace_token = request.headers.get("X-Workspace-Token")
        if workspace_token:
            try:
                token_user = lookup_workspace_token(workspace_token)
            except UserNotFoundException:
                abort(401, description="Invalid workspace token")
            except UserTokenExpiredException:
                abort(401, description="This workspace token has expired")
            except Exception:
                logger.exception(f"error resolving workspace token for {user.id}:")
                abort(401, description="Internal error while resolving workspace token")
            else:
                as_user = token_user

        dojo = dojo_accessible(dojo_id)
        if not dojo:
            return {"success": False, "error": "Invalid dojo"}

        dojo_challenge = (
            DojoChallenges.query.filter_by(id=challenge_id)
            .join(DojoModules.query.filter_by(dojo=dojo, id=module_id).subquery())
            .first()
        )
        if not dojo_challenge:
            return {"success": False, "error": "Invalid challenge"}

        if not dojo_challenge.visible() and not dojo.is_admin():
            return {"success": False, "error": "Invalid challenge"}

        if practice and not dojo_challenge.allow_privileged:
            return {
                "success": False,
                "error": "This challenge does not support practice mode.",
            }

        if is_challenge_locked(dojo_challenge, user):
            return {
                "success": False,
                "error": "This challenge is locked"
            }

        if dojo.is_admin(user) and "as_user" in data:
            try:
                as_user_id = int(data["as_user"])
            except ValueError:
                return {"success": False, "error": f"Invalid user ID ({data['as_user']})"}
            if is_admin():
                as_user = Users.query.get(as_user_id)
            else:
                student = next((student for student in dojo.students if student.user_id == as_user_id), None)
                if student is None:
                    return {"success": False, "error": f"Not a student in this dojo ({as_user_id})"}
                if not student.official:
                    return {"success": False, "error": f"Not an official student in this dojo ({as_user_id})"}
                as_user = student.user

        job = _initialize_job(user, as_user, dojo_challenge, practice)
        app = current_app._get_current_object()
        job_thread = threading.Thread(
            target=_run_challenge_job,
            args=(
                app,
                job["id"],
                user.id,
                as_user.id if as_user else None,
                dojo_challenge.id,
                practice,
            ),
            daemon=True,
        )
        job_thread.start()

        response = {"success": True, "job_id": job["id"]}
        if job.get("job_url"):
            response["job_url"] = job["job_url"]
        else:
            response["message"] = "Workspace queued"
        return response

    @authed_only
    def get(self):
        dojo_challenge = get_current_dojo_challenge()
        if not dojo_challenge:
            return {"success": False, "error": "No active challenge"}

        user = get_current_user()
        container = get_current_container(user)
        if not container:
            return {"success": False, "error": "No challenge container"}

        practice = container.labels.get("dojo.mode") == "privileged"

        return {
            "success": True,
            "dojo": dojo_challenge.dojo.reference_id,
            "module": dojo_challenge.module.id,
            "challenge": dojo_challenge.id,
            "practice" : practice,
        }

    @authed_only
    def delete(self):
        user = get_current_user()
        container = get_current_container(user)

        if not container:
            return {"success": False, "error": "No active challenge container"}

        try:
            remove_container(user)
            return {"success": True, "message": "Challenge container terminated"}
        except Exception as e:
            logger.error(f"Failed to terminate container for user {user.id}: {e}")
            return {"success": False, "error": "Failed to terminate container"}
