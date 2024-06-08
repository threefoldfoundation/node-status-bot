# Node Status Bot
This bot provides realtime status updates and alerts on status changes for nodes on the ThreeFold Grid. You can find it live on [@tfnodestatusbot](https://t.me/tfnodestatusbot).

## Usage

If you want to run your own copy of the bot, you'll need a Linux system with Python installed. Visit the [BotFather](https://t.me/BotFather) to create a new Telegram bot.

### v1

The v1 bot is fairly straight forward to host. Since the RMB based ping is disabled until the code can be improved, you really just need to install the dependencies and start the bot using your bot token.

Here are the complete steps to get started:

```
git clone https://github.com/threefoldfoundation/node-status-bot.git
cd node-status-bot
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
wget https://github.com/threefoldtech/rmb-rs/releases/download/v1.0.7/rmb-peer
chmod u+x rmb-peer
```

With that, you can run the bot, substituting your bot token:

```
python node-status-bot <bot_token>
```

Then go say hi to your bot on Telegram and try some commands.

### v2

With v2, the bot has become two independent applications that work together. The ingester is responsible for processing the blocks of tfchain and making relevant events available the bot as a SQLite database.

Running this version is a bit more involved than for v1 and is not documented at this time. If you are interested in trying it, please open an issue on this repo or contact @scottyeager via Telegram.