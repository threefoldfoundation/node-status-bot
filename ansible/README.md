This folder contains Ansible playbooks for deploying Rqlite and the bot into systems using Zinit as a process manager (such as micro VMs running on the ThreeFold Grid).

These playbooks require an Ansible module for Zinit. Install it like this:

```
ansible-galaxy collection install git+https://github.com/scottyeager/ansible-zinit.git
```

Assuming you have an `inventory.ini` file with an inventory for your hosts, you can run the playbooks like so:

```
ansible-playbook -i docker.ini deploy_rqlite_cluster.yaml
ansible-playbook -i docker.ini deploy_bot.yaml -e "bot_token=mytoken git_ref=mybranch"
```

Bot token is mandatory and must be supplied. The `git_ref` is an optional tag, branch, or commit hash to use, with the default being the main branch.

## Docker

There is a Docker Compose file provided for the purpose of testing the Ansible playbooks. As an advantage of using micro VMs which are also based on Docker images, we can use the same image for these tests. While connecting via SSH to the containers would be possible, it's easier to use the Ansible Docker connector, as shown in the `docker.ini` file.

For testing the clustering features in a more efficient way, see the Docker Compose file in the `docker` directory.
