#!/usr/bin/env python3

from importlib import import_module
from pathlib import Path
from unittest import mock

from pdoc.__main__ import cli, parser

# Need this import or else import_module doesn't work
import sqlmesh


def mocked_import(*args, **kwargs):
    """Return a MagicMock if import fails for any reason"""
    try:
        return import_module(*args, **kwargs)
    except Exception:
        mocked_module = mock.MagicMock()
        mocked_module.__name__ = args[0]
        return mocked_module


if __name__ == "__main__":
    # Mock uninstalled dependencies so pdoc can still work
    with mock.patch("importlib.import_module", side_effect=mocked_import):
        opts = parser.parse_args()
        opts.docformat = "google"
        opts.modules = ["sqlmesh"]
        opts.logo_link = "https://tobikodata.com"
        opts.footer_text = "Copyright Tobiko Data Inc. 2022"
        opts.template_directory = Path(__file__).parent.joinpath("templates").absolute()
        opts.edit_url = ["sqlmesh=https://github.com/SQLMesh/sqlmesh/tree/main/sqlmesh/"]

        with mock.patch("pdoc.__main__.parser", **{"parse_args.return_value": opts}):
            cli()
