#!/usr/bin/env bash

echo "[distutils]" >> ~/.pypirc
echo "index-servers = tobiko-private" >> ~/.pypirc
echo "[tobiko-private]" >> ~/.pypirc
echo "repository = $TOBIKO_PRIVATE_PYPI_URL" >> ~/.pypirc
echo "username = _json_key_base64" >> ~/.pypirc
echo "password = $TOBIKO_PRIVATE_PYPI_KEY" >> ~/.pypirc
