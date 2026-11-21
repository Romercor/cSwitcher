"""User-facing failure: the CLI prints the message and exits with status 1."""


class CswitchError(Exception):
    pass
