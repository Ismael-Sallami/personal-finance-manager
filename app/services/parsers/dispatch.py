"""Picks the parser for a broker.

The parsers pull in pandas and pdfplumber, which take seconds to import. They
are loaded when a statement is uploaded, not when the process starts.
"""
from importlib import import_module

from app.services.parsers.broker_types import ParseResult

# broker -> module that parses it. Nothing is imported yet.
PARSER_MODULES = {
    "myinvestor": "app.services.parsers.myinvestor",
    "kraken": "app.services.parsers.kraken",
    "traderepublic": "app.services.parsers.traderepublic",
}

BROKERS = list(PARSER_MODULES.keys())


def parse(broker: str, content: bytes) -> ParseResult:
    """Parse a statement with the parser of the given broker.

    An unknown broker returns a ParseResult carrying the error instead of
    raising, so the upload page can show it and the request still completes.
    """
    module_path = PARSER_MODULES.get(broker)
    if module_path is None:
        return ParseResult(broker, [], [f"Unknown broker: {broker}"])
    return import_module(module_path).parse(content)
