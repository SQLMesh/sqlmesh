#!/bin/bash

# This script is intended to be run by an Ubuntu dev container

# Exit immediately if any command returns a non-zero code
set -e

# Install OS-level dependencies

# Note that msodbcsql18 install requires the Microsoft APT repo to be available to this script. If this script is used
# in conjunction with a container image from the Microsoft container registry this should already be the case.
ALL_DEPENDENCIES="libpq-dev netcat-traditional unixodbc-dev default-jdk msodbcsql18"

sudo apt-get clean && sudo apt-get -y update && sudo ACCEPT_EULA='Y' apt-get -y install $ALL_DEPENDENCIES

# Install Python dependencies
make install-dev