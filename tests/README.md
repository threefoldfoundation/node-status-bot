<h1> Node Status Bot Tests </h1>

<h2>Table of Contents</h2>

- [Introduction](#introduction)
- [Build the Test Data Directory](#build-the-test-data-directory)
- [Start the Bot](#start-the-bot)
- [Testing Node State Changes](#testing-node-state-changes)
- [Testing Farmerbot Violations](#testing-farmerbot-violations)
  - [Case 1](#case-1)
  - [Case 2](#case-2)
- [Clean Up](#clean-up)
- [What's Missing](#whats-missing)
- [Other Tests](#other-tests)

---

## Introduction

This directory contains basic tests for the node status bot.

For testing purposes, a Docker Compose file is available here that runs a single Rqlite node and a single Node Status Bot. 

## Build the Test Data Directory

Some basic facilities are available to test the Node Status Bot in a manual end-to-end way. That is, you can start a test bot, simulate various data inputs, and then observe the chat outputs produced by the bot.

Make sure you are in the `tests` directory:

```
cd tests
```

You should either set the Telegram bot token with `export` or by writing an `.env` file.

- Export
    ```
    export BOT_TOKEN=123ABC
    ```
- `.env` file
    ```console
    cat .env
    BOT_TOKEN=<token>
    ```

Then the test deployment can be started up:


- Start the container
    ```
    docker compose up -d
    ```
- Verify the container is running
    ```
    docker ps
    ```

This builds an image from the local bot code and should pull in the latest local changes automatically.

Files that can be manipulated for testing are placed under `test_data`. These files will be owned by root with restrictive permissions. 

- To make life easier, give all users read/write permissions:
    ```
    sudo chmod --recursive a+rw test_data/
    ```

## Start the Bot

The bot can take a bit of time to start up. Also note that the polling interval is set to 5 seconds for this test, so any alerts should be delivered quickly after data is changed.

Issue a `/start` command to the bot and wait for it to respond. Don't forget that this is necessary any time the database is cleared.

Once the bot responds, subscribing to updates on node id 1 will be helpful for further testing: `/sub 1`

## Testing Node State Changes

The bot has a special mode where it uses a local file, rather than remote GraphQL, to query node details. This is very simplistic and does not support multiples nodes. Only the single node defined in the file will be returned upon any query for node details.

```console
$ cat test_data/node_test_data
{"nodeID": 1, "twinID": 1, "updatedAt": 30, "power": {"state": "Up", "target": "Up"}}
```

The `updatedAt` value here is in a special form, which is seconds in the past, rather than an absolute timestamp. This makes it easy to simulate a node that is down, by editing the value to be a large number of seconds in the past.

Editing the power state and target should also cause the bot to respond appropriately, by stating the node is standby or that it received a wakeup signal.

## Testing Farmerbot Violations

The bot will also create an empty `tfchain.db` database with the schema used by the TFChain ingester. There are several scripts here that push simulated events into the database in order to test the bot's ability to detect and alert users about violations.

There are basically two test cases. The first is where an "open" or "probable" violation is simulated. This means that a wake up was triggered for the node and it hasn't yet responded within a period of time that's greater than the allowed time limit for wakeups. Technically it is still possible for the node to avoid a violation, depending on the contents of its next uptime event.

The second is a "sure" violation, where all the evidence is present that the node didn't wake up in time.

To run these tests, use the scripts with the `tfchain.db` file as the argument.

### Case 1

```
python make_open_violation.py test_data/tfchain.db
```

If you are subscribed to node 1, the bot should give a probable violation alert.

```
python wake_node.py test_data/tfchain.db
```

No further alert is generated, but the violation should now include the wake up time when queried with `/violations`.

To proceed to `Case 2`, make sure that you [clean up](#clean-up) the deployment.

### Case 2

```
python make_violation.py test_data/tfchain.db
```

If you are subscribed to node 1, the bot should give a violation alert.

## Clean Up

Between each test, you will need to clean up your deployment. Keep this in mind when testing `Case 1` and `Case 2`.

The Rqlite database is stored in a volume, so clearing the database requires removing volumes with `-v`:

```
docker compose down -v
```

Test data can be cleared with:

```
rm -r test_data
```

If you want to do another test, make sure to redeploy the container:

```
docker compose up -d
```

## What's Missing

* Unit tests
* Testing the case where a probable violation doesn't become an actual violation
* ...

## Other Tests

The `find_violations` code has been tested against the actual minting output from several minting cycles, to ensure it detects the same number of violations as minting itself. Of course, the implementations can diverge if changes are made to minting in the future. Ideally these tests would be ongoing, but they can't be strictly automated since the info about how many violations a node received is not published publicly.
