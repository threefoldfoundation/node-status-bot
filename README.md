# Node Status Bot
This bot provides realtime status updates and alerts on status changes for nodes on the ThreeFold Grid. You can find it live on [@tfnodestatusbot](https://t.me/tfnodestatusbot).

## Usage

If you want to run your own copy of the bot, you'll need a Linux system with Python installed. Visit the [BotFather](https://t.me/BotFather) to create a new Telegram bot.

### Install

Here are the steps to prep a virtual environment to use with the bot. In some cases, such as when installing into a container, using a virtual environment might not be needed. If you're not sure, these steps should work everywhere:

```
git clone https://github.com/threefoldfoundation/node-status-bot.git
cd node-status-bot
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### Run

Since v2, the bot is now a two part deployment:

1. The "ingester", which gathers data from tfchain and populates it into an SQLite database
2. The bot itself, which depends on the presence of a database created by the ingester

*Note that the function of the ingester is only needed for the feature of the bot that reports on farmerbot violations. However, there's no option to disable this feature and the bot will not run without a compatible database file. Running the ingester is not strictly required to use other features of the bot, but starting it once is necessary to create a database file.*

#### Ingester

To start up the ingester:

```
python3 ingester.py
```

By default, the ingester will place a database file `tfchain.db` in the same directory. You can change the location of the file with the `-f` or `--file` option. It will then begin to gather all data for the current minting period (the month so far, approximately) and will run continuously processing new blocks as they are created.

In order to efficiently clear any backlog of blocks when the ingester first starts, it will by default spawn 50 worker processes and scale them down later when the queue is cleared. For systems with limited RAM, you might want to cap the number of max workers lower, for example:

```
python3 ingester.py --max-workers 5
```

The ingester has a few other CLI args, which are used to control the start and end points between which data is gathered. These are mostly for testing and other use cases for the generated database.

#### Bot

Once the ingester is running, you can start up the bot in another shell like this, substituting your own bot token:

```
python node-status-bot.py <bot_token>
```

By default, the bot also looks in the current directory for a database file `tfchain.db`. A different path can be specified with `-f`.

Then go say hi to your bot on Telegram and try some commands.

### Database Setup

The bot uses rqlite for storing chat and node data. To make sure that foreign key constraints work properly, be sure to start rqlite with the `-fk` flag.

You can run a single-node rqlite cluster in Docker with:

```bash
docker run -d --name rqlite-node -p 4001:4001 rqlite/rqlite -fk
```

This will start rqlite and expose it on port 4001, which is the default port the bot expects.

### Operation

For best results, both the ingester and the bot should run under a process manager that can restart them if they exit for any reason. Nothing special is needed here really - `zinit`, `systemd`, or other solutions will work fine. Just create basic unit/service files with the same commands shown above. Docker can be used for this purpose too.

## Bugs and support

Bugs? What bugs? 😆

Please open an issue on this repo for any problems you encounter or features you desire.

For support on using the public bot or hosting your own (or just to say "hi"), you can also contact @scottyeager via Telegram.
