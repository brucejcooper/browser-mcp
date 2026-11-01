"""browser-mcp: an MCP server that browses and searches the web in a real Firefox."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("browser-mcp")
except PackageNotFoundError:  # running from the source tree
    __version__ = "0.0.0"
