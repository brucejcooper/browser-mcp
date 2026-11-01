"""A mitmproxy addon for the tests: each request's URL, one per line."""

import os


def request(flow):
    with open(os.environ["MITM_LOG"], "a") as f:
        f.write(flow.request.pretty_url + "\n")
