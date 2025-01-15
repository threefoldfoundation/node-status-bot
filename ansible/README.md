<h1> Deploy with Ansible and Docker</h1>

<h2>Table of Contents</h2>

- [Introduction](#introduction)
- [Prerequisites](#prerequisites)
- [Deployment Steps](#deployment-steps)
- [Clean Up](#clean-up)
- [Notes on Docker](#notes-on-docker)

---

## Introduction

This folder contains Ansible playbooks for deploying Rqlite and the bot into systems using Zinit as a process manager (such as micro VMs running on the ThreeFold Grid).

## Prerequisites

As an example for Ubuntu, these are the prerequisites needed to get started with the proper Ansible version:

```
# Add Ansible repo and update
sudo apt-add-repository ppa:ansible/ansible
sudo apt update

# Install Ansible
sudo apt install ansible

# Check the Ansible version (should be 2.17.7)
ansible --version
```

## Deployment Steps

Here are the basic steps to deploy the node status bot with Ansible

```
# Clone and cd into the repo
git clone https://github.com/threefoldfoundation/node-status-bot
cd ansible

# Deploy the Docker containers for the 3 nodes
docker compose up -d

# Verify the containers are running properly
docker ps

# Generate the `docker.ini` file with the proper container names
bash generate_docker_ini.sh

# Deploy the RQLite cluster
ansible-playbook -i docker.ini deploy_rqlite_cluster.yml

# Deploy the Bot, use your own TG bot token, and write the proper branch, e.g. `main`
ansible-playbook -i docker.ini deploy_bot.yml -e "bot_token=mytoken git_ref=main"
```

The bot token is mandatory and must be supplied. The `git_ref` is an optional tag, branch, or commit hash to use, with the default being the main branch.

## Clean Up

To stop the node status bot simply run:

```
docker compose down
```

## Notes on Docker

There is a Docker Compose file provided for the purpose of testing the Ansible playbooks. As an advantage of using micro VMs which are also based on Docker images, we can use the same image for these tests. While connecting via SSH to the containers would be possible, it's easier to use the Ansible Docker connector, as shown in the `docker.ini` file.

For testing the clustering features in a more efficient way, see the Docker Compose file in the `docker` directory.