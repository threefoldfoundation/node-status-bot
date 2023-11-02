# Node Status Bot
This bot provides realtime status updates and alerts on status changes for nodes on the ThreeFold Grid. You can find it live on [@tfnodestatusbot](https://t.me/tfnodestatusbot).

## Usage

If you want to run your own copy of the bot, you'll need a Linux system with Python installed. Visit the [BotFather](https://t.me/BotFather) to create a new Telegram bot.

In addition to the Python dependencies, this bot also uses the Reliable Message Bus to ping nodes. It expects the [`rmb-peer`](https://github.com/threefoldtech/rmb-rs) binary to be in the same folder that the bot runs in, and `redis-server` to be installed. You'll also need a TF Chain account that's been activated with a twin.

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

With that, you can run the bot, substituting your bot token and mnemonic seed phrase:

```
python node-status-bot <bot_token> -s "<mnemonic>"
```

Then go say hi to your bot on Telegram and try some commands.
