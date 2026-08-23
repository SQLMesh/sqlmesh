#!/bin/bash

# This script is intended to be run by an Ubuntu development container

# Exit immediately if any command returns a non-zero code
set -e

# Install OS-level dependencies

## Ensure that the Microsoft package repository is available as it is required for msodbcsql18. Note that the repository
## may have already been added by a development container feature (e.g. docker-in-docker or java).
if apt-cache policy | grep -q 'packages.microsoft.com'; then
    echo "Microsoft package repository already present"
else
    echo "Microsoft package repository not present, it will be added."
    
    # ref: https://learn.microsoft.com/en-us/sql/connect/odbc/linux-mac/installing-the-microsoft-odbc-driver-for-sql-server
    curl -sSL -O https://packages.microsoft.com/config/ubuntu/$(grep VERSION_ID /etc/os-release | cut -d '"' -f 2)/packages-microsoft-prod.deb
    sudo dpkg -i packages-microsoft-prod.deb
    rm packages-microsoft-prod.deb
fi

ALL_DEPENDENCIES="libpq-dev netcat-traditional unixodbc-dev default-jdk msodbcsql18"
sudo apt-get clean && sudo apt-get -y update && sudo ACCEPT_EULA='Y' apt-get -y install $ALL_DEPENDENCIES

# Install Python dependencies
make install-dev