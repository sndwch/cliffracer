"""Text that loguru reads as a template: sink paths and format strings."""


def escape_braces(text: str) -> str:
    """``text`` with its braces doubled, so loguru takes it literally.

    Loguru runs a file sink's path and every sink's format through
    ``str.format``; a service name such as ``svc{0}`` placed in either one has
    to arrive as ``svc{{0}}``.
    """
    return text.replace("{", "{{").replace("}", "}}")
