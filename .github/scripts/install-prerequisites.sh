#!/bin/bash

# This script is intended to be run by an Ubuntu CI build agent
# The goal is to install OS-level dependencies that are required before trying to install Python dependencies

set -e

if [ -z "$1" ]; then
    echo "USAGE: $0 <engine>"
    exit 1
fi

ENGINE="$1"

COMMON_DEPENDENCIES="libpq-dev netcat-traditional unixodbc-dev"
ENGINE_DEPENDENCIES=""

if [ "$ENGINE" == "spark" ]; then
    ENGINE_DEPENDENCIES="default-jdk"
elif [ "$ENGINE" == "fabric" ]; then
    echo "Installing Microsoft package repository"

    # ref: https://learn.microsoft.com/en-us/sql/connect/odbc/linux-mac/installing-the-microsoft-odbc-driver-for-sql-server
    curl -sSL -O https://packages.microsoft.com/config/ubuntu/$(grep VERSION_ID /etc/os-release | cut -d '"' -f 2)/packages-microsoft-prod.deb
    sudo dpkg -i packages-microsoft-prod.deb
    rm packages-microsoft-prod.deb

    ENGINE_DEPENDENCIES="msodbcsql18"
fi

ALL_DEPENDENCIES="$COMMON_DEPENDENCIES $ENGINE_DEPENDENCIES"

echo "Installing OS-level dependencies: $ALL_DEPENDENCIES"

sudo apt-get clean && sudo apt-get -y update && sudo ACCEPT_EULA='Y' apt-get -y install $ALL_DEPENDENCIES

if [ "$ENGINE" == "spark" ]; then
    echo "Using Java version for spark:"
    java -version
fi

echo "All done"
