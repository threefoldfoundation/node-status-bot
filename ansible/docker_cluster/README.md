# Testing Node Status Bot deployment with Docker

Here is a Docker Compose file provided for the purpose of testing the Ansible playbooks. The Docker images used here are the same images used for base Ubuntu micro VMs on the ThreeFold Grid. This provides a very close test environment to the final intended deployment environment.

There are two options for connecting to the containers with Ansible:

1. SSH (best compatibility but requires authorized_keys file)
2. Docker connection (slightly limited compatibility)

The Docker connection won't work with feature of the `bootstrap_ingester` playbook that uses SSH to sync between two cluster machines. All other playbooks are supported. You also need Ansible version 2.15.0 or higher.

## SSH

To use an SSH connection, you will need to provide an `authorized keys` file that contains your SSH public key. This file must also be owned by root, since it will be bind mounted inside the container and root's authorized key file must be owned by root for `sshd` to use it.

Here's an example. Replace the path to the public key file if needed:

```
cp ~/.ssh/authorized_keys ./
sudo chown root:root authorized_keys
```

Now you can:

```
docker compose up -d
```

Once the cluster is up, you can also generate an inventory file for SSH based connection using the appropriate script:

```
./generate_ssh_inventory.sh
```

This will generate an inventory file that you can use with the playbooks.

## Docker connection

To use the Docker connection, just bring up the cluster of containers and use the other script to generate an inventory file:

```
docker compose up -d
./generate_docker_inventory.sh
```
