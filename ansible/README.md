# Node Status Bot Ansible Playbooks

This folder contains Ansible playbooks for deploying Rqlite and the bot into systems using Zinit as a process manager (such as micro VMs running on the ThreeFold Grid).

First of all you will of course need Ansible. Check the [offical docs](https://docs.ansible.com/ansible/latest/installation_guide/index.html) for installation instructions.

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

## Ingester bootstrap

Since gathering historic data with the ingester is rather time consuming and resource intensive, it's nice to bootstrap from an existing database file. The `bootstrap_ingester` playbook provides two ways to do this:

1. Copy a local database file to a remote machine (local file should not have any open database connections)
2. Live sync the database from another node in the cluster using `sqlite3_rsync`

### Bootstrap strategy

When bringing a new cluster live, we might want to seed it with a database file on our local machine, such as a backup taken from another cluster. Rather than upload to all cluster machines from our local machine, we might prefer to:

1. Upload to one cluster machine
2. Sync from the first cluster machine to the others

Also, when replacing one member of a cluster, it makes sense to sync to the new machine from one of the existing cluster members.

To achieve these strategies, we can use limits when calling Ansible, so apply playbooks to a subset of hosts in the inventory. How to use limits will be demonstrated in the examples below.

### Local DB file

The local file option is triggered by passing a `local_db_path` to the playbook. Here we copy our local database file to a single host in the cluster, `host1`:

```
ansible-playbook -i inventory.ini bootstrap_ingester.yaml -e "local_db_path=./tfchain.db" --limit host1
```

Unfortunately, Ansible doesn't seem to have a way to display progress on file transfers. Just using a tool like `scp` or `rsync` directly might make more sense, especially when only copying to a single host.

### sqlite3_rsync

The SQLite project now provides a tool called `sqlite3_rsync` for syncing two database files with the following features:

* Can work over `ssh` to sync with a remote machine
* One or both database files can be actively in use

This makes it a nice option for bootstrapping between nodes in the custer, even if the source database has an active ingester writing data to it.

#### Agent forwarding

In order to use this feature, there must be SSH connectivity between the two machines involved. Since generally cluster machines won't have indepent SSH access to each other, agent forwarding is enabled in this playbook to allow using the local machine's SSH keys to authorize between two cluster machines.

To use agent forwarding, you will need to make sure the agent is started and your SSH key is added:

```
ssh-add -l
```

If the agent can't be reached or your key is missing, run these commands:

```
# For bash:
eval $(ssh-agent -s)

# For fish:
# eval (ssh-agent -c)

ssh-add ~/.ssh/id_rsa.pub # Or replace with path to your public key
```

#### Example

Before using `sqlite3_rsync`, it must be installed:

```
ansible-playbook -i inventory.ini install_sqlite3_rsync.yaml
```

Here's an example of syncing within the cluster, by specifying an `origin_path` with the internal WireGuard IP of `host1`. Combined with the example above, this completes seeding all machines in a three host cluster with the database file originating on our local machine.

```
ansible-playbook -i inventory.ini bootstrap_ingester.yaml -e "origin_path=root@10.1.3.2:/opt/tfchain.db --limit host2,host3
```

By limiting to a single host, this form could also be used to bootstrap a new cluster member from the database of another member with an actively running ingester.

## Development

If you want to hack on the playbooks themselves, there's a Docker Compose file under `ansible/docker` to bring up a local cluster for rapid testing. These are only intended for testing the Ansible based deployment process. To quickly test clusters of the bot without going through the deployment process, use the other Docker Compose file under `docker` in the repo root.
