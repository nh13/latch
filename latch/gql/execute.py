import os
from functools import cache
from typing import Dict, Optional

import gql
from gql.transport.aiohttp import AIOHTTPTransport

from latch.registry.types import JSON
from latch_cli.config.latch import config
from latch_cli.config.user import user_config


class AuthenticationError(RuntimeError):
    ...


@cache
def get_transport() -> AIOHTTPTransport:
    auth_header: Optional[str] = None

    if auth_header is None:
        token = os.environ.get("FLYTE_INTERNAL_EXECUTION_ID", "")
        if token != "":
            auth_header = f"Latch-Execution-Token {token}"

    if auth_header is None:
        token = user_config.token
        if token != "":
            auth_header = f"Latch-SDK-Token {token}"

    if auth_header is None:
        raise AuthenticationError(
            "Unable to find credentials to connect to gql server, aborting"
        )

    return AIOHTTPTransport(
        url=config.gql,
        headers={"Authorization": auth_header},
    )


def execute(
    document: str,
    variables: Optional[Dict[str, JSON]] = None,
):
    client = gql.Client(transport=get_transport())
    return client.execute(gql.gql(document), variables)


# todo(ayush): add generator impl for subscriptions
