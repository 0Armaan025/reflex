"""Hosting service related utilities."""
from __future__ import annotations

import contextlib
import enum
import json
import os
import re
import time
import uuid
import webbrowser
from datetime import datetime
from http import HTTPStatus
from typing import List, Optional

import httpx
import websockets
from pydantic import Field, ValidationError, root_validator

from reflex import constants
from reflex.base import Base
from reflex.utils import console


def get_existing_access_token() -> tuple[str, str]:
    """Fetch the access token from the existing config if applicable.

    Raises:
        Exception: if runs into any issues, file not exist, ill-formatted, etc.

    Returns:
        The access token and optionally the invitation code if valid, otherwise empty string.
    """
    console.debug("Fetching token from existing config...")
    try:
        with open(constants.Hosting.HOSTING_JSON, "r") as config_file:
            hosting_config = json.load(config_file)

        assert (
            access_token := hosting_config.get("access_token", "")
        ), "no access token found or empty token"
        return access_token, hosting_config.get("code")
    except Exception as ex:
        console.debug(
            f"Unable to fetch token from {constants.Hosting.HOSTING_JSON} due to: {ex}"
        )
        raise Exception("no existing login found") from ex


def validate_token(token: str):
    """Validate the token with the control plane.

    Args:
        token: The access token to validate.

    Raises:
        ValueError: if access denied.
        Exception: if runs into timeout, failed requests, unexpected errors. These should be tried again.
    """
    try:
        response = httpx.post(
            constants.Hosting.POST_VALIDATE_ME_ENDPOINT,
            headers=authorization_header(token),
            timeout=constants.Hosting.HTTP_REQUEST_TIMEOUT,
        )
        if response.status_code == HTTPStatus.FORBIDDEN:
            raise ValueError
        response.raise_for_status()
    except httpx.RequestError as re:
        console.debug(f"Request to auth server failed due to {re}")
        raise Exception("request error") from re
    except httpx.HTTPError as ex:
        console.debug(f"Unable to validate the token due to: {ex}")
        raise Exception("server error") from ex
    except ValueError as ve:
        console.debug(f"Access denied for {token}")
        raise ValueError("access denied") from ve
    except Exception as ex:
        console.debug(f"Unexpected error: {ex}")
        raise Exception("internal errors") from ex


def delete_token_from_config():
    """Delete the invalid token from the config file if applicable."""
    if os.path.exists(constants.Hosting.HOSTING_JSON):
        hosting_config = {}
        try:
            with open(constants.Hosting.HOSTING_JSON, "w") as config_file:
                hosting_config = json.load(config_file)
                del hosting_config["access_token"]
                json.dump(hosting_config, config_file)
        except Exception as ex:
            # Best efforts removing invalid token is OK
            console.debug(
                f"Unable to delete the invalid token from config file, err: {ex}"
            )


def save_token_to_config(token: str, code: str | None = None):
    """Cache the token, and optionally invitation code to the config file.

    Args:
        token: The access token to save.
        code: The invitation code to save if exists.

    Raise:
        Exception: if runs into any issues, file not exist, etc.
    """
    hosting_config: dict[str, str] = {"access_token": token}
    if code:
        hosting_config["code"] = code
    try:
        with open(constants.Hosting.HOSTING_JSON, "w") as config_file:
            json.dump(hosting_config, config_file)
    except Exception as ex:
        console.warn(
            f"Unable to save token to {constants.Hosting.HOSTING_JSON} due to: {ex}"
        )


def authenticated_token() -> str | None:
    """Fetch the access token from the existing config if applicable and validate it.

    Returns:
        The access token if it is valid, None otherwise.
    """
    # Check if the user is authenticated
    try:
        token, _ = get_existing_access_token()
        if not token:
            console.debug("No token found from the existing config.")
            return None
        validate_token(token)
        return token
    except Exception as ex:
        console.debug(f"Unable to validate the token from the existing config: {ex}")
        try:
            console.debug("Try to delete the invalid token from config file")
            with open(constants.Hosting.HOSTING_JSON, "rw") as config_file:
                hosting_config = json.load(config_file)
                del hosting_config["access_token"]
                json.dump(hosting_config, config_file)
        except Exception as ex:
            console.debug(f"Unable to delete the invalid token from config file: {ex}")
        return None


def authorization_header(token: str) -> dict[str, str]:
    """Construct an authorization header with the specified token as bearer token.

    Args:
        token: The access token to use.

    Returns:
        The authorization header in dict format.
    """
    return {"Authorization": f"Bearer {token}"}


class DeploymentPrepInfo(Base):
    """The params/settings returned from the prepare endpoint
    including the deployment key and the frontend/backend URLs once deployed.
    The key becomes part of both frontend and backend URLs.
    """

    # The deployment key
    key: str
    # The backend URL
    api_url: str
    # The frontend URL
    deploy_url: str


class DeploymentPrepareResponse(Base):
    """The params/settings returned from the prepare endpoint,
    used in the CLI for the subsequent launch request.
    """

    # The app prefix, used on the server side only
    app_prefix: str
    # The reply from the server for a prepare request to deploy over a particular key
    # If reply is not None, it means server confirms the key is available for use.
    reply: Optional[DeploymentPrepInfo] = None
    # The list of existing deployments by the user under the same app name.
    # This is used to allow easy upgrade case when user attempts to deploy
    # in the same named app directory, user intends to upgrade the existing deployment.
    existing: Optional[List[DeploymentPrepInfo]] = None
    # The suggested key name based on the app name.
    # This is for a new deployment, user has not deployed this app before.
    # The server returns key suggestion based on the app name.
    suggestion: Optional[DeploymentPrepInfo] = None

    @root_validator(pre=True)
    def ensure_at_least_one_deploy_params(cls, values):
        """Ensure at least one set of param is returned for any of the cases we try to prepare.

        Args:
            values: The values passed in.

        Raises:
            ValueError: If all of the optional fields are None.

        Returns:
            The values passed in.
        """
        if (
            values.get("reply") is None
            and not values.get("existing")  # existing cannot be an empty list either
            and values.get("suggestion") is None
        ):
            raise ValueError(
                "At least one set of params for deploy is required from control plane."
            )
        return values


class DeploymentsPreparePostParam(Base):
    """Params for app API URL creation backend API."""

    # The app name which is found in the config
    app_name: str
    # The deployment key
    key: Optional[str] = None  #  name of the deployment
    # The frontend hostname to deploy to. This is used to deploy at hostname not in the regular domain.
    frontend_hostname: Optional[str] = None


def prepare_deploy(
    app_name: str,
    key: str | None = None,
    frontend_hostname: str | None = None,
) -> DeploymentPrepareResponse:
    """Send a POST request to Control Plane to prepare a new deployment.
    Control Plane checks if there is conflict with the key if provided.
    If the key is absent, it will return existing deployments and a suggested name based on the app_name in the request.

    Args:
        key: The deployment name.
        app_name: The app name.
        frontend_hostname: The frontend hostname to deploy to. This is used to deploy at hostname not in the regular domain.

    Raises:
        Exception: If the operation fails. The exception message is the reason.

    Returns:
        The response containing the backend URLs if successful, None otherwise.
    """
    # Check if the user is authenticated
    if not (token := authenticated_token()):
        raise Exception("not authenticated")
    try:
        response = httpx.post(
            constants.Hosting.POST_DEPLOYMENTS_PREPARE_ENDPOINT,
            headers=authorization_header(token),
            json=DeploymentsPreparePostParam(
                app_name=app_name, key=key, frontend_hostname=frontend_hostname
            ).dict(exclude_none=True),
            timeout=constants.Hosting.HTTP_REQUEST_TIMEOUT,
        )

        response_json = response.json()
        console.debug(f"Response from prepare endpoint: {response_json}")
        if response.status_code == HTTPStatus.FORBIDDEN:
            console.debug(f'Server responded with 403: {response_json.get("detail")}')
            raise ValueError(f'{response_json.get("detail", "forbidden")}')
        response.raise_for_status()
        return DeploymentPrepareResponse(
            app_prefix=response_json["app_prefix"],
            reply=response_json["reply"],
            suggestion=response_json["suggestion"],
            existing=response_json["existing"],
        )
    except httpx.RequestError as re:
        console.debug(f"Unable to prepare launch due to {re}.")
        raise Exception("request error") from re
    except httpx.HTTPError as he:
        console.debug(f"Unable to prepare deploy due to {he}.")
        raise Exception(f"{he}") from he
    except json.JSONDecodeError as jde:
        console.debug(f"Server did not respond with valid json: {jde}")
        raise Exception("internal errors") from jde
    except (KeyError, ValidationError) as kve:
        console.debug(f"The server response format is unexpected {kve}")
        raise Exception("internal errors") from kve
    except ValueError as ve:
        # This is a recognized client error, currently indicates forbidden
        raise Exception(f"{ve}") from ve
    except Exception as ex:
        console.debug(f"Unexpected error: {ex}.")
        raise Exception("internal errors") from ex


class DeploymentPostResponse(Base):
    """The URL for the deployed site."""

    # The frontend URL
    frontend_url: str = Field(..., regex=r"^https?://", min_length=8)
    # The backend URL
    backend_url: str = Field(..., regex=r"^https?://", min_length=8)


class DeploymentsPostParam(Base):
    """Params for hosted instance deployment POST request."""

    # Key is the name of the deployment, it becomes part of the URL
    key: str = Field(..., regex=r"^[a-zA-Z0-9-]+$")
    # Name of the app
    app_name: str = Field(..., min_length=1)
    # json encoded list of regions to deploy to
    regions_json: str = Field(..., min_length=1)
    # The app prefix, used on the server side only
    app_prefix: str = Field(..., min_length=1)
    # The version of reflex CLI used to deploy
    reflex_version: str = Field(..., min_length=1)
    # The number of CPUs
    cpus: Optional[int] = None
    # The memory in MB
    memory_mb: Optional[int] = None
    # Whether to auto start the hosted deployment
    auto_start: Optional[bool] = None
    # Whether to auto stop the hosted deployment when idling
    auto_stop: Optional[bool] = None
    # The frontend hostname to deploy to. This is used to deploy at hostname not in the regular domain.
    frontend_hostname: Optional[str] = None
    # The description of the deployment
    description: Optional[str] = None
    # The json encoded list of environment variables
    envs_json: Optional[str] = None
    # The command line prefix for tracing
    reflex_cli_entrypoint: Optional[str] = None
    # The metrics endpoint
    metrics_endpoint: Optional[str] = None


def deploy(
    frontend_file_name: str,
    backend_file_name: str,
    export_dir: str,
    key: str,
    app_name: str,
    regions: list[str],
    app_prefix: str,
    vm_type: str | None = None,
    cpus: int | None = None,
    memory_mb: int | None = None,
    auto_start: bool | None = None,
    auto_stop: bool | None = None,
    frontend_hostname: str | None = None,
    envs: dict[str, str] | None = None,
    with_tracing: str | None = None,
    with_metrics: str | None = None,
) -> DeploymentPostResponse:
    """Send a POST request to Control Plane to launch a new deployment.

    Args:
        frontend_file_name: The frontend file name.
        backend_file_name: The backend file name.
        export_dir: The directory where the frontend/backend zip files are exported.
        key: The deployment name.
        app_name: The app name.
        regions: The list of regions to deploy to.
        app_prefix: The app prefix.
        vm_type: The VM type.
        cpus: The number of CPUs.
        memory_mb: The memory in MB.
        auto_start: Whether to auto start.
        auto_stop: Whether to auto stop.
        frontend_hostname: The frontend hostname to deploy to. This is used to deploy at hostname not in the regular domain.
        envs: The environment variables.
        with_tracing: A string indicating the command line prefix for tracing.
        with_metrics: A string indicating the metrics endpoint.

    Raises:
        Exception: If the operation fails. The exception message is the reason.

    Returns:
        The response containing the URL of the site to be deployed if successful, None otherwise.
    """
    # Check if the user is authenticated
    if not (token := authenticated_token()):
        raise Exception("not authenticated")

    try:
        params = DeploymentsPostParam(
            key=key,
            app_name=app_name,
            regions_json=json.dumps(regions),
            app_prefix=app_prefix,
            cpus=cpus,
            memory_mb=memory_mb,
            auto_start=auto_start,
            auto_stop=auto_stop,
            envs_json=json.dumps(envs) if envs else None,
            frontend_hostname=frontend_hostname,
            reflex_version=constants.Reflex.VERSION,
            reflex_cli_entrypoint=with_tracing,
            metrics_endpoint=with_metrics,
        )
        with open(
            os.path.join(export_dir, frontend_file_name), "rb"
        ) as frontend_file, open(
            os.path.join(export_dir, backend_file_name), "rb"
        ) as backend_file:
            # https://docs.python-requests.org/en/latest/user/advanced/#post-multiple-multipart-encoded-files
            files = [
                ("files", (frontend_file_name, frontend_file)),
                ("files", (backend_file_name, backend_file)),
            ]
            response = httpx.post(
                constants.Hosting.POST_DEPLOYMENTS_ENDPOINT,
                headers=authorization_header(token),
                data=params.dict(exclude_none=True),
                files=files,
            )
        response.raise_for_status()
        response_json = response.json()
        return DeploymentPostResponse(
            frontend_url=response_json["frontend_url"],
            backend_url=response_json["backend_url"],
        )
    except httpx.RequestError as re:
        console.debug(f"Unable to deploy due to request error: {re}")
        raise Exception("request error") from re
    except httpx.HTTPError as he:
        console.debug(f"Unable to deploy due to {he}.")
        raise Exception("internal errors") from he
    except json.JSONDecodeError as jde:
        console.debug(f"Server did not respond with valid json: {jde}")
        raise Exception("internal errors") from jde
    except (KeyError, ValidationError) as kve:
        console.debug(f"Post params or server response format unexpected: {kve}")
        raise Exception("internal errors") from kve
    except Exception as ex:
        console.debug(f"Unable to deploy due to internal errors: {ex}.")
        raise Exception("internal errors") from ex


class DeploymentsGetParam(Base):
    """Params for hosted instance GET request."""

    # The app name which is found in the config
    app_name: Optional[str]


class DeploymentGetResponse(Base):
    """The params/settings returned from the GET endpoint."""

    # The deployment key
    key: str
    # The list of regions to deploy to
    regions: List[str]
    # The app name which is found in the config
    app_name: str
    # The VM type
    vm_type: str
    # The number of CPUs
    cpus: int
    # The memory in MB
    memory_mb: int
    # The site URL
    url: str
    # The list of environment variable names (values are never shown)
    envs: List[str]


def list_deployments(
    app_name: str | None = None,
) -> list[dict]:
    """Send a GET request to Control Plane to list deployments.

    Args:
        app_name: the app name as an optional filter when listing deployments.

    Raises:
        Exception: If the operation fails. The exception message shows the reason.

    Returns:
        The list of deployments if successful, None otherwise.
    """
    if not (token := authenticated_token()):
        raise Exception("not authenticated")

    params = DeploymentsGetParam(app_name=app_name)

    try:
        response = httpx.get(
            constants.Hosting.GET_DEPLOYMENTS_ENDPOINT,
            headers=authorization_header(token),
            params=params.dict(exclude_none=True),
            timeout=constants.Hosting.HTTP_REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        return [
            DeploymentGetResponse(
                key=deployment["key"],
                regions=deployment["regions"],
                app_name=deployment["app_name"],
                vm_type=deployment["vm_type"],
                cpus=deployment["cpus"],
                memory_mb=deployment["memory_mb"],
                url=deployment["url"],
                envs=deployment["envs"],
            ).dict()
            for deployment in response.json()
        ]
    except httpx.RequestError as re:
        console.debug(f"Unable to list deployments due to request error: {re}")
        raise Exception("request timeout") from re
    except httpx.HTTPError as he:
        console.debug(f"Unable to list deployments due to {he}.")
        raise Exception("internal errors") from he
    except (ValidationError, KeyError, json.JSONDecodeError) as vkje:
        console.debug(f"Server response format unexpected: {vkje}")
        raise Exception("internal errors") from vkje
    except Exception as ex:
        console.error(f"Unexpected error: {ex}.")
        raise Exception("internal errors") from ex


def fetch_token(request_id: str) -> tuple[str, str]:
    """Fetch the access token for the request_id from Control Plane.

    Args:
        request_id: The request ID used when the user opens the browser for authentication.

    Raises:
        Exception: For request timeout, failed requests, ill-formed responses, unexpected errors.

    Returns:
        The access token if it exists, None otherwise.
    """
    try:
        resp = httpx.get(
            f"{constants.Hosting.FETCH_TOKEN_ENDPOINT}/{request_id}",
            timeout=constants.Hosting.HTTP_REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        return (resp_json := resp.json())["access_token"], resp_json.get("code", "")
    except httpx.RequestError as re:
        console.debug(f"Unable to fetch token due to request error: {re}")
        raise Exception("request timeout") from re
    except httpx.HTTPError as he:
        console.debug(f"Unable to fetch token due to {he}")
        raise Exception("not found") from he
    except json.JSONDecodeError as jde:
        console.debug(f"Server did not respond with valid json: {jde}")
        raise Exception("internal errors") from jde
    except KeyError as ke:
        console.debug(f"Server response format unexpected: {ke}")
        raise Exception("internal errors") from ke
    except Exception as ex:
        console.debug("Unexpected errors: {ex}")
        raise Exception("internal errors") from ex


def poll_backend(backend_url: str) -> bool:
    """Poll the backend to check if it is up.

    Args:
        backend_url: The URL of the backend to poll.

    Returns:
        True if the backend is up, False otherwise.
    """
    try:
        console.debug(f"Polling backend at {backend_url}")
        resp = httpx.get(
            f"{backend_url}/ping", timeout=constants.Hosting.HTTP_REQUEST_TIMEOUT
        )
        resp.raise_for_status()
        return True
    except httpx.HTTPError:
        return False


def poll_frontend(frontend_url: str) -> bool:
    """Poll the frontend to check if it is up.

    Args:
        frontend_url: The URL of the frontend to poll.

    Returns:
        True if the frontend is up, False otherwise.
    """
    try:
        console.debug(f"Polling frontend at {frontend_url}")
        resp = httpx.get(
            f"{frontend_url}", timeout=constants.Hosting.HTTP_REQUEST_TIMEOUT
        )
        resp.raise_for_status()
        return True
    except httpx.HTTPError:
        return False


class DeploymentDeleteParam(Base):
    """Params for hosted instance DELETE request."""

    # key is the name of the deployment, it becomes part of the site URL
    key: str


def delete_deployment(key: str):
    """Send a DELETE request to Control Plane to delete a deployment.

    Args:
        key: The deployment name.

    Raises:
        ValueError: If the key is not provided.
        Exception: If the operation fails. The exception message is the reason.
    """
    if not (token := authenticated_token()):
        raise Exception("not authenticated")
    if not key:
        raise ValueError("Valid key is required for the delete.")

    try:
        response = httpx.delete(
            f"{constants.Hosting.DELETE_DEPLOYMENTS_ENDPOINT}/{key}",
            headers=authorization_header(token),
            timeout=constants.Hosting.HTTP_REQUEST_TIMEOUT,
        )
        response.raise_for_status()

    except httpx.TimeoutException as te:
        console.debug("Unable to delete deployment due to request timeout.")
        raise Exception("request timeout") from te
    except httpx.HTTPError as he:
        console.debug(f"Unable to delete deployment due to {he}.")
        raise Exception("internal errors") from he
    except Exception as ex:
        console.debug(f"Unexpected errors {ex}.")
        raise Exception("internal errors") from ex


class SiteStatus(Base):
    """Deployment status info."""

    # The frontend URL
    frontend_url: Optional[str] = None
    # The backend URL
    backend_url: Optional[str] = None
    # Whether the frontend/backend URL is reachable
    reachable: bool
    # The last updated iso formatted timestamp if site is reachable
    updated_at: Optional[str] = None

    @root_validator(pre=True)
    def ensure_one_of_urls(cls, values):
        """Ensure at least one of the frontend/backend URLs is provided.

        Args:
            values: The values passed in.

        Raises:
            ValueError: If none of the URLs is provided.

        Returns:
            The values passed in.
        """
        if values.get("frontend_url") is None and values.get("backend_url") is None:
            raise ValueError("At least one of the URLs is required.")
        return values


class DeploymentStatusResponse(Base):
    """Response for deployment status request."""

    # The frontend status
    frontend: SiteStatus
    # The backend status
    backend: SiteStatus


def get_deployment_status(key: str) -> DeploymentStatusResponse:
    """Get the deployment status.

    Args:
        key: The deployment name.

    Raises:
        ValueError: If the key is not provided.
        Exception: If the operation fails. The exception message is the reason.

    Returns:
        The deployment status response including backend and frontend.
    """
    if not key:
        raise ValueError(
            "A non empty key is required for querying the deployment status."
        )

    if not (token := authenticated_token()):
        raise Exception("not authenticated")

    try:
        response = httpx.get(
            f"{constants.Hosting.GET_DEPLOYMENT_STATUS_ENDPOINT}/{key}/status",
            headers=authorization_header(token),
            timeout=constants.Hosting.HTTP_REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        response_json = response.json()
        return DeploymentStatusResponse(
            frontend=SiteStatus(
                frontend_url=response_json["frontend"]["url"],
                reachable=response_json["frontend"]["reachable"],
                updated_at=response_json["frontend"]["updated_at"],
            ),
            backend=SiteStatus(
                backend_url=response_json["backend"]["url"],
                reachable=response_json["backend"]["reachable"],
                updated_at=response_json["backend"]["updated_at"],
            ),
        )
    except Exception as ex:
        console.debug(f"Unable to get deployment status due to {ex}.")
        raise Exception("internal errors") from ex


def convert_to_local_time(iso_timestamp: str) -> str:
    """Convert the iso timestamp to local time.

    Args:
        iso_timestamp: The iso timestamp to convert.

    Returns:
        The converted timestamp string.
    """
    try:
        local_dt = datetime.fromisoformat(iso_timestamp).astimezone()
        return local_dt.strftime("%Y-%m-%d %H:%M:%S.%f %Z")
    except Exception as ex:
        console.debug(f"Unable to convert iso timestamp {iso_timestamp} due to {ex}.")
        return iso_timestamp


class LogType(str, enum.Enum):
    """Enum for log types."""

    # Logs printed from the user code, the "app"
    APP_LOG = "app"
    # Build logs are the server messages while building/running user deployment
    BUILD_LOG = "build"
    # Deploy logs are specifically for the messages at deploy time
    # returned to the user the current stage of the deployment, such as building, uploading.
    DEPLOY_LOG = "deploy"
    # All the logs which can be printed by all above types.
    ALL_LOG = "all"


async def get_logs(
    key: str,
    log_type: LogType = LogType.APP_LOG,
    from_iso_timestamp: datetime | None = None,
):
    """Get the deployment logs and stream on console.

    Args:
        key: The deployment name.
        log_type: The type of logs to query from server.
                  See the LogType definitions for how they are used.
        from_iso_timestamp: An optional timestamp with timezone info to limit
                            where the log queries should start from.

    Raises:
        ValueError: If the key is not provided.
        Exception: If the operation fails. The exception message is the reason.

    """
    if not (token := authenticated_token()):
        raise Exception("not authenticated")
    if not key:
        raise ValueError("Valid key is required for querying logs.")
    try:
        logs_endpoint = f"{constants.Hosting.DEPLOYMENT_LOGS_ENDPOINT}/{key}/logs?access_token={token}&log_type={log_type.value}"
        console.debug(f"log server endpoint: {logs_endpoint}")
        if from_iso_timestamp is not None:
            logs_endpoint += (
                f"&from_iso_timestamp={from_iso_timestamp.astimezone().isoformat()}"
            )
        _ws = websockets.connect(logs_endpoint)  # type: ignore
        async with _ws as ws:
            while True:
                row_json = json.loads(await ws.recv())
                console.debug(f"Server responded with logs: {row_json}")
                if row_json and isinstance(row_json, dict):
                    if "timestamp" in row_json:
                        row_json["timestamp"] = convert_to_local_time(
                            row_json["timestamp"]
                        )
                    print(" | ".join(row_json.values()))
                else:
                    console.debug("Server responded, no new logs, this is normal")
    except Exception as ex:
        console.debug(f"Unable to get more deployment logs due to {ex}.")
        console.print("Log server disconnected ...")
        console.print(
            "Note that the server has limit to only stream logs for several minutes to conserve resources"
        )


def check_requirements_txt_exist():
    """Check if requirements.txt exists in the current directory.

    Raises:
        Exception: If the requirements.txt does not exist.
    """
    if not os.path.exists(constants.RequirementsTxt.FILE):
        raise Exception(
            f"Unable to find {constants.RequirementsTxt.FILE} in the current directory."
        )


def authenticate_on_browser(
    invitation_code: str | None,
) -> tuple[str | None, str | None]:
    """Open the browser to authenticate the user.

    Args:
        invitation_code: The invitation code if it exists.

    Raises:
        SystemExit: If the browser cannot be opened.

    Returns:
        The access token and invitation if valid, Nones otherwise.
    """
    console.print(f"Opening {constants.Hosting.CP_WEB_URL} ...")
    request_id = uuid.uuid4().hex
    if not webbrowser.open(
        f"{constants.Hosting.CP_WEB_URL}?request-id={request_id}&code={invitation_code}"
    ):
        console.error(
            f"Unable to open the browser to authenticate. Please contact support."
        )
        raise SystemExit("Unable to open browser for authentication.")
    with console.status("Waiting for access token ..."):
        for _ in range(constants.Hosting.WEB_AUTH_RETRIES):
            try:
                return fetch_token(request_id)
            except Exception:
                pass
            time.sleep(constants.Hosting.WEB_AUTH_SLEEP_DURATION)

    return None, None


def validate_token_with_retries(access_token: str) -> bool:
    """Validate the access token with retries.

    Args:
        access_token: The access token to validate.

    Raises:
        SystemExit: If the token is confirmed invalid by server.

    Returns:
        True if the token is valid, False otherwise.
    """
    with console.status("Validating access token ..."):
        for _ in range(constants.Hosting.WEB_AUTH_RETRIES):
            try:
                validate_token(access_token)
                return True
            except ValueError as ve:
                console.error(f"Access denied")
                delete_token_from_config()
                raise SystemExit("Access denied") from ve
            except Exception as ex:
                console.debug(f"Unable to validate token due to: {ex}")
                time.sleep(constants.Hosting.WEB_AUTH_SLEEP_DURATION)
    return False


def interactive_get_deployment_key_from_user_input(
    pre_deploy_response: DeploymentPrepareResponse,
    app_name: str,
    frontend_hostname: str | None = None,
) -> tuple[str, str, str]:
    """Interactive get the deployment key from user input.

    Args:
        pre_deploy_response: The response from the initial prepare call to server.
        app_name: The app name.
        frontend_hostname: The frontend hostname to deploy to. This is used to deploy at hostname not in the regular domain.

    Returns:
        The deployment key, backend URL, frontend URL.
    """
    key_candidate = api_url = deploy_url = ""
    if reply := pre_deploy_response.reply:
        api_url = reply.api_url
        deploy_url = reply.deploy_url
        key_candidate = reply.key
    elif pre_deploy_response.existing:
        # validator already checks existing field is not empty list
        # Note: keeping this simple as we only allow one deployment per app
        existing = pre_deploy_response.existing[0]
        console.print(f"Overwrite deployment [ {existing.key} ] ...")
        key_candidate = existing.key
        api_url = existing.api_url
        deploy_url = existing.deploy_url
    elif suggestion := pre_deploy_response.suggestion:
        key_candidate = suggestion.key
        api_url = suggestion.api_url
        deploy_url = suggestion.deploy_url

        # If user takes the suggestion, we will use the suggested key and proceed
        while key_input := console.ask(f"Name of deployment", default=key_candidate):
            try:
                pre_deploy_response = prepare_deploy(
                    app_name,
                    key=key_input,
                    frontend_hostname=frontend_hostname,
                )
                assert pre_deploy_response.reply is not None
                assert key_input == pre_deploy_response.reply.key
                key_candidate = pre_deploy_response.reply.key
                api_url = pre_deploy_response.reply.api_url
                deploy_url = pre_deploy_response.reply.deploy_url
                # we get the confirmation, so break from the loop
                break
            except Exception:
                console.error(
                    "Cannot deploy at this name, try picking a different name"
                )

    return key_candidate, api_url, deploy_url


def process_envs(envs: list[str]) -> dict[str, str]:
    """Process the environment variables.

    Args:
        envs: The environment variables expected in key=value format.

    Raises:
        SystemExit: If the envs are not in valid format.

    Returns:
        The processed environment variables in a dict.
    """
    processed_envs = {}
    for env in envs:
        kv = env.split("=", maxsplit=1)
        if len(kv) != 2:
            raise SystemExit("Invalid env format: should be <key>=<value>.")

        if not re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*$", kv[0]):
            raise SystemExit(
                "Invalid env name: should start with a letter or underscore, followed by letters, digits, or underscores."
            )
        processed_envs[kv[0]] = kv[1]
    return processed_envs


def log_out_on_browser():
    """Open the browser to authenticate the user.

    Raises:
        SystemExit: If the browser cannot be opened.
    """
    # Fetching existing invitation code so user sees the log out page without having to enter it
    invitation_code = None
    with contextlib.suppress(Exception):
        _, invitation_code = get_existing_access_token()
        console.debug("Found existing invitation code in config")
    console.print(f"Opening {constants.Hosting.CP_WEB_URL} ...")
    if not webbrowser.open(f"{constants.Hosting.CP_WEB_URL}?code={invitation_code}"):
        raise SystemExit(
            f"Unable to open the browser to log out. Please contact support."
        )


async def display_deploy_milestones(key: str, from_iso_timestamp: datetime):
    """Display the deploy milestone messages reported back from the hosting server.

    Args:
        key: The deployment key.
        from_iso_timestamp: The timestamp of the deployment request time, this helps with the milestone query.

    Raises:
        ValueError: If a non-empty key is not provided.
        Exception: If the user is not authenticated.
    """
    if not key:
        raise ValueError("Non-empty key is required for querying deploy status.")
    if not (token := authenticated_token()):
        raise Exception("not authenticated")

    try:
        logs_endpoint = f"{constants.Hosting.DEPLOYMENT_LOGS_ENDPOINT}/{key}/logs?access_token={token}&log_type={LogType.DEPLOY_LOG.value}&from_iso_timestamp={from_iso_timestamp.astimezone().isoformat()}"
        console.debug(f"log server endpoint: {logs_endpoint}")
        _ws = websockets.connect(logs_endpoint)  # type: ignore
        async with _ws as ws:
            # Stream back the deploy events reported back from the server
            for _ in range(constants.Hosting.DEPLOYMENT_EVENT_MESSAGES_RETRIES):
                row_json = json.loads(await ws.recv())
                console.debug(f"Server responded with: {row_json}")
                if row_json and isinstance(row_json, dict):
                    # Only show the timestamp and actual message
                    console.print(
                        " | ".join(
                            [
                                convert_to_local_time(row_json["timestamp"]),
                                row_json["message"],
                            ]
                        )
                    )
                    if any(
                        msg in row_json["message"].lower()
                        for msg in constants.Hosting.END_OF_DEPLOYMENT_MESSAGES
                    ):
                        console.debug(
                            "Received end of deployment message, stop event message streaming"
                        )
                        return
                else:
                    console.debug("Server responded, no new events yet, this is normal")
    except Exception as ex:
        console.debug(f"Unable to get more deployment events due to {ex}.")


def wait_for_server_to_pick_up_request():
    """Wait for server to pick up the request. Right now is just sleep."""
    with console.status(
        f"Waiting for server to pick up request ~ {constants.Hosting.DEPLOYMENT_PICKUP_DELAY} seconds ..."
    ):
        for _ in range(constants.Hosting.DEPLOYMENT_PICKUP_DELAY):
            time.sleep(1)


def interactive_prompt_for_envs() -> list[str]:
    """Interactive prompt for environment variables.

    Returns:
        The list of environment variables in key=value string format.
    """
    envs = []
    envs_finished = False
    env_key_prompt = "  Env name (enter to skip)"
    console.print("Environment variables ...")
    while not envs_finished:
        env_key = console.ask(env_key_prompt)
        env_key_prompt = "  env name (enter to finish)"
        if not env_key:
            envs_finished = True
            if envs:
                console.print("Finished adding envs.")
            else:
                console.print("No envs added. Continuing ...")
            break
        # If it possible to have empty values for env, so we do not check here
        env_value = console.ask("  env value")
        envs.append(f"{env_key}={env_value}")
    return envs
