from __future__ import annotations

import os
from collections.abc import Mapping


# User-facing provider CLIs need their normal home/config directories in order to
# use an existing authenticated session. They do not need model, connector, cloud,
# repository, Python, Node, or application-specific environment variables.
_TRUSTED_CLI_ENVIRONMENT = frozenset({
    "ALL_PROXY",
    "APPDATA",
    "COMSPEC",
    "CURL_CA_BUNDLE",
    "HOME",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "LOCALAPPDATA",
    "NODE_EXTRA_CA_CERTS",
    "NO_PROXY",
    "NUMBER_OF_PROCESSORS",
    "OS",
    "PATH",
    "PATHEXT",
    "PROCESSOR_ARCHITECTURE",
    "PROCESSOR_IDENTIFIER",
    "PROCESSOR_LEVEL",
    "PROCESSOR_REVISION",
    "REQUESTS_CA_BUNDLE",
    "SSH_AUTH_SOCK",
    "SSL_CERT_DIR",
    "SSL_CERT_FILE",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "TMPDIR",
    "USERPROFILE",
    "WINDIR",
})


def trusted_cli_environment(
    source: Mapping[str, str] | None = None,
    *,
    include_ssh_agent: bool = True,
) -> dict[str, str]:
    """Return the small ambient environment required by authenticated CLIs.

    Matching is case-insensitive for Windows, but original key spelling is kept.
    Values containing NUL are discarded so they cannot cross a process boundary.
    """
    values = os.environ if source is None else source
    environment: dict[str, str] = {}
    for key, raw_value in values.items():
        normalized_key = str(key).upper()
        if normalized_key not in _TRUSTED_CLI_ENVIRONMENT:
            continue
        if normalized_key == "SSH_AUTH_SOCK" and not include_ssh_agent:
            continue
        value = str(raw_value)
        if "\x00" not in value:
            environment[str(key)] = value
    return environment
