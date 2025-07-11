import argparse
import logging
import os
import random
import sqlite3
import time
import uuid
from datetime import datetime

import grid3.graphql
import telegram
from gql import gql
from grid3.minting.period import Period
from grid3.types import Node
from telegram import ParseMode, Update
from telegram.ext import (
    CallbackContext,
    CommandHandler,
    Defaults,
    Updater,
)

import find_violations
from db import RqliteDB
from ingester import prep_db

# Technically Telegram supports messages up to 4096 characters, beyond which an
# error is returned. However in my experience, messages longer than 3800 chars
# with html formatting don't get formatted past 3800
MAX_TEXT_LENGTH = 3800

NETWORKS = ["main", "test", "dev"]
DEFAULT_PING_TIMEOUT = 10
DEFAULT_HEARTBEAT_INTERVAL = 30
JITTER_TIME = 5
DB_RETRY_WAIT = 5
BOOT_TOLERANCE = 60 * 40


def check_chat(update: Update, context: CallbackContext):
    chat = update.effective_chat.id
    send_message(context, chat, text="Your chat id is {}".format(chat))


def check_job(context: CallbackContext):
    """
    The main attraction. This function collects all the node ids that have an active subscription, checks their status, then sends alerts to users whose nodes have a status change.
    """
    db = context.bot_data["db"]
    con, periods = get_con_and_periods()

    for net in NETWORKS:
        try:
            # Get all subscribed nodes and their chat subscriptions
            subscribed_nodes = db.get_all_subscribed_nodes()

            # Convert to format: {node_id: [chat_ids]}
            subbed_nodes = {node_id: chat_ids for node_id, chat_ids in subscribed_nodes}

            # Get current node statuses
            updates = get_nodes(net, subbed_nodes.keys())

            if net == "main":
                # Check for violations only on mainnet
                all_violations = {}
                for node_id in subbed_nodes.keys():
                    violations = get_violations(con, node_id, periods)
                    all_violations[node_id] = violations

        except:
            logging.exception("Error fetching node data for check")
            continue

        for update in updates:
            try:
                # Get current node state from database
                node_data = db.get_node(update.nodeId, net)
                if not node_data:
                    # New node, create initial record
                    db.create_node(update, net)
                    node_data = db.get_node(update.nodeId, net)

                # Check for status changes
                if (
                    node_data["power"]["target"] == "Down"
                    and update.power["target"] == "Up"
                ):
                    for chat_id in subbed_nodes[update.nodeId]:
                        send_message(
                            context,
                            chat_id,
                            text="Node {} wake up initiated \N{HOT BEVERAGE}".format(
                                update.nodeId
                            ),
                        )

                if node_data["status"] == "up" and update.status == "down":
                    for chat_id in subbed_nodes[update.nodeId]:
                        send_message(
                            context,
                            chat_id,
                            text="Node {} has gone offline \N{WARNING SIGN}".format(
                                update.nodeId
                            ),
                        )

                elif node_data["status"] == "up" and update.status == "standby":
                    for chat_id in subbed_nodes[update.nodeId]:
                        send_message(
                            context,
                            chat_id,
                            text="Node {} has gone to sleep \N{LAST QUARTER MOON WITH FACE}".format(
                                update.nodeId
                            ),
                        )

                elif node_data["status"] == "standby" and update.status == "down":
                    for chat_id in subbed_nodes[update.nodeId]:
                        send_message(
                            context,
                            chat_id,
                            text="Node {} did not wake up within 24 hours \N{WARNING SIGN}".format(
                                update.nodeId
                            ),
                        )

                elif (
                    node_data["status"] in ("down", "standby") and update.status == "up"
                ):
                    for chat_id in subbed_nodes[update.nodeId]:
                        send_message(
                            context,
                            chat_id,
                            text="Node {} has come online \N{ELECTRIC LIGHT BULB}".format(
                                update.nodeId
                            ),
                        )

                # We track which nodes have ever been managed by farmerbot,
                # since those are the only ones that can get violations and
                # scanning for violations is a relatively expensive operation
                if node_data["status"] == "standby" or update.status == "standby":
                    node_data["farmerbot"] = True
                # Update node status in database
                db.update_node(update, net)

                # Check for new violations
                if update.nodeId in all_violations:
                    existing_violations = node_data["violations"]
                    for violation in all_violations[update.nodeId]:
                        if violation.boot_requested not in existing_violations:
                            if violation.finalized:
                                for chat_id in subbed_nodes[update.nodeId]:
                                    send_message(
                                        context,
                                        chat_id,
                                        text="🚨 Farmerbot violation detected for node {}. Node failed to boot within 30 minutes 🚨\n\n{}".format(
                                            update.nodeId, format_violation(violation)
                                        ),
                                    )
                            # The idea here was to give a bit of wiggle room before alerting the user, since these are only possible violations at this point. However, if the condition wasn't met when the violation was first detected, then the user was never alerted. To reenable this, we'd need some additional logic here in the bot or in code that finds violations.
                            # elif (
                            #     violation.end_time - violation.boot_requested
                            #     > BOOT_TOLERANCE
                            # ):
                            else:
                                for chat_id in subbed_nodes[update.nodeId]:
                                    send_message(
                                        context,
                                        chat_id,
                                        text="🚨 Possible farmerbot violation detected for node {}. Node appears to have not booted within 30 minutes of boot request. Check again with /violations after node boots 🚨\n\n{}".format(
                                            update.nodeId, format_violation(violation)
                                        ),
                                    )

                            # Add new violation to database
                            db.add_violation(update.nodeId, net, violation)

            except:
                logging.exception("Error in alert block")


def format_list(items):
    if len(items) == 1:
        text = " " + str(items[0])
    elif len(items) == 2:
        text = "s " + str(items[0]) + " and " + str(items[1])
    else:
        text = "s "
        for i in items[:-1]:
            text = text + str(i) + ", "
        text = text + "and " + str(items[-1])
    return text


def format_nodes(up, down, standby):
    up.sort()
    down.sort()
    standby.sort()
    text = ""

    if up:
        text += "<b><u>Up nodes:</u></b>\n"
        text += format_vertical_list(up)
    if down:
        if up:
            text += "\n"
        text += "<b><u>Down nodes:</u></b>\n"
        text += format_vertical_list(down)
    if standby:
        if up or down:
            text += "\n"
        text += "<b><u>Standby nodes:</u></b>\n"
        text += format_vertical_list(standby)

    return text


def format_vertical_list(items):
    text = ""
    for item in items:
        text += str(item) + "\n"
    return text


def format_violation(violation):
    text = ""
    requested = datetime.fromtimestamp(violation.boot_requested)
    text += "<i>Boot requested at:</i>\n"
    text += "{} UTC\n".format(requested)
    if violation.booted_at:
        booted = datetime.fromtimestamp(violation.booted_at)
        text += "<i>Node booted at:</i>\n"
        text += "{} UTC\n".format(booted)
    else:
        text += "Node has not booted\n"
    return text


def format_violations(node_id, violations):
    text = "<b><u>Violations for node {}:</u></b>\n\n".format(node_id)
    for violation in violations:
        requested = datetime.fromtimestamp(violation.boot_requested)
        text += "<i>Boot requested at:</i>\n"
        text += "{} UTC\n".format(requested)
        if violation.booted_at:
            booted = datetime.fromtimestamp(violation.booted_at)
            text += "<i>Node booted at:</i>\n"
            text += "{} UTC\n".format(booted)
        else:
            text += "Node has not booted\n"
        text += "\n"
    return text


def get_con_and_periods():
    con = sqlite3.connect(args.db_file)
    current_period = Period()
    last_period = Period(offset=current_period.offset - 1)
    periods = (current_period, last_period)
    return con, periods


def get_nodes(net, node_ids):
    """
    Query a list of node ids in GraphQL, create Node objects for consistency and easy field access, then assign them a status and return them.
    """
    graphql = graphqls[net]
    nodes = graphql.nodes(
        ["nodeID", "twinID", "updatedAt", "power"], nodeID_in=node_ids
    )
    nodes = [Node(node) for node in nodes]

    one_hour_ago = time.time() - 60 * 60

    for node in nodes:
        if node.power is None:
            node.power = {"state": None, "target": None}
        node.status = get_node_status(node)

        if node.status == "standby":
            node.farmerbot = True
        else:
            node.farmerbot = False
        node.violations = {}

    return nodes


def get_nodes_from_file(net, node_ids):
    """
    For use in test mode, to emulate get_nodes using data in a file. The updatedAt value is given in the file as a delta of how many seconds in the past and converted to absolute time here
    """
    if net == "main":
        try:
            text = open("node_test_data", "r").read()
        except FileNotFoundError:
            # Create sample node data
            sample_data = {
                "nodeID": 1,
                "twinID": 1,
                "updatedAt": 30,  # 30 seconds ago
                "power": {"state": "Up", "target": "Up"},
            }
            with open("node_test_data", "w") as f:
                json.dump(sample_data, f)
            text = json.dumps(sample_data)

        node = Node(json.loads(text))
        node.updatedAt = time.time() - node.updatedAt
        node.status = get_node_status(node)

        return [node]

    else:
        return []


def get_node_status(node):
    """
    More or less the same methodology that Grid Proxy uses. Nodes are supposed to report every 40 minutes, so we consider them offline after one hour. Standby nodes should wake up once every 24 hours, so we consider them offline after that.
    """
    one_hour_ago = time.time() - 60 * 60
    one_day_ago = time.time() - 60 * 60 * 24

    # It's possible that some node might not have a power state
    if node.updatedAt > one_hour_ago and node.power["state"] != "Down":
        return "up"
    elif node.power["state"] == "Down" and node.updatedAt > one_day_ago:
        return "standby"
    else:
        return "down"


def get_violations(con, node_id, periods):
    violations = []
    for period in periods:
        violations.extend(find_violations.check_node(con, node_id, period))
    return violations


def is_leader_valid(leader_info):
    if not leader_info:
        return False

    try:
        leader_id, last_heartbeat = leader_info.split(":")
        last_heartbeat = float(last_heartbeat)
        return time.time() - last_heartbeat < 2 * args.heartbeat_interval
    except:
        return False


def update_leader(db):
    try:
        leader_value = f"{args.node_id}:{time.time()}"
        db.set_metadata("leader", leader_value)
        return True
    except:
        logging.exception("Failed to update leader")
        return False


def get_leader(db):
    try:
        return db.get_metadata("leader")
    except:
        logging.exception("Failed to get leader")
        return None


def initialize_dbs(bot_data):
    # Initialize SQLite database if it doesn't exist
    if not os.path.exists(args.db_file):
        logging.info(f"Creating new database at {args.db_file}")
        con = sqlite3.connect(args.db_file)
        prep_db(con)
        con.close()

    bot_data["db"] = RqliteDB(host=args.rqlite_host, port=args.rqlite_port)


def network(update: Update, context: CallbackContext):
    chat_id = update.effective_chat.id
    db = context.bot_data["db"]

    if context.args:
        net = context.args[0]
        if net in NETWORKS:
            db.update_chat_network(chat_id, net)
            send_message(context, chat_id, text="Set network to {}net".format(net))
        else:
            send_message(
                context,
                chat_id,
                text="Please specify a valid network: dev, test, or main",
            )
    else:
        net = db.get_chat_network(chat_id)
        send_message(context, chat_id, text="Network is set to {}net".format(net))


def node_used_farmerbot(con, node_id):
    # Check if the node ever went standby, which is a requirement for it to receive a violation
    result = con.execute(
        "SELECT 1 FROM PowerStateChanged WHERE node_id=? AND state='Down'", (node_id,)
    ).fetchone()
    return result is not None


# This function existed to make a smooth transition when the violations feature
# was launched. It stores all historic violations for all nodes that were
# already in the system. For a new bot, it's not relevant since there won't be
# any active nodes from the start.
def populate_violations(bot_data):
    db = bot_data["db"]

    # Check if violations have already been populated
    if db.get_metadata("violations_populated") == "true":
        return

    logging.info("Populating violations")

    con, periods = get_con_and_periods()

    # Get all nodes that have ever been in standby (managed by farmerbot)
    res = con.execute(
        "SELECT DISTINCT node_id FROM PowerStateChanged WHERE state='Down'"
    )
    farmerbot_nodes = [row[0] for row in res.fetchall()]

    # Only check nodes that are already in our database
    existing_nodes = db.get_nodes("main")
    farmerbot_nodes = [n for n in farmerbot_nodes if n in existing_nodes]

    # For each farmerbot-managed node, check for existing violations and store them
    for node_id in farmerbot_nodes:
        violations = get_violations(con, node_id, periods)
        if violations:
            db.add_violations(node_id, "main", violations)

    # Mark violations as populated
    db.set_metadata("violations_populated", "true")


def send_message(context, chat_id, text):
    try:
        if len(text) > MAX_TEXT_LENGTH:
            for message in split_message(text):
                context.bot.send_message(chat_id=chat_id, text=message)
        else:
            context.bot.send_message(chat_id=chat_id, text=text)
    except telegram.error.Unauthorized:
        # User blocked the bot or deleted their account
        pass
    except:
        logging.exception("Error sending message")


def split_message(text):
    # The only messages that get over length at the time of writing this
    # function are violations reports. Since each node's violations are
    # separated by two blank lines, we can split on those and avoid a message
    # break in the middle of one node's section
    messages = []
    message = ""
    splitter = "\n\n\n"
    for chunk in text.split(splitter):
        if len(message) + len(chunk) > MAX_TEXT_LENGTH:
            messages.append(message.rstrip(splitter))
            message = chunk + splitter
        else:
            message += chunk + splitter
    messages.append(message.rstrip(splitter))

    return messages


def start(update: Update, context: CallbackContext):
    chat_id = update.effective_chat.id
    db = context.bot_data["db"]
    db.create_chat(chat_id)
    msg = """
Hey there,

I'm the ThreeFold node status bot. Beep boop.

I can give you information about whether a node is up or down right now (/status) and also notify you if its state changes in the future (/subscribe).

Here are all the commands I support:

/help - print this message again.

/status - check the current status of one node. This uses a similar method as the Dashboard for determining node status, and update may be delayed by an hour. With no input, a status report will be generated for all subscribed nodes, if any.
Example: /status 1

/violations - scan for farmerbot related violations during the current minting period. Like status, this works on all subscribed nodes when no input is given.

/subscribe - subscribe to updates about one or more nodes. If you don't provide an input, the nodes you are currently subscribed to will be shown.
Example: /sub 1 2 3

/unsubscribe - unsubscribe from updates about one or more nodes. To unsubscribe from all node and thus stop all alerts, write "/unsubscribe all"

/network - change the network to "dev", "test", or "main" (default is main). If you don't provide an input, the currently selected network is shown.
Example: /network main

To report bugs, request features, or just say hi, please contact @scottyeager. Please also subscribe to the updates channel here for news on the bot: t.me/node_bot_updates

You can find the bot's source code on GitHub: github.com/threefoldfoundation/node-status-bot

This bot is developed and operated on a best effort basis. Only you are responsible for your node's uptime and your farming rewards.
    """
    send_message(context, chat_id, text=msg)


def status_gql(update: Update, context: CallbackContext):
    chat_id = update.effective_chat.id
    db = context.bot_data["db"]

    # Get current network for this chat
    net = db.get_chat_network(chat_id)

    if context.args:
        try:
            node = get_nodes(net, context.args)[0]
            send_message(
                context, chat_id, text="Node {} is {}".format(node.nodeId, node.status)
            )
        except IndexError:
            send_message(
                context, chat_id, text="Node id not valid on {}net".format(net)
            )
        except:
            logging.exception("Failed to fetch node info")
            send_message(
                context,
                chat_id,
                text="Error fetching node data. Please wait a moment and try again.",
            )

    else:
        subbed_nodes = db.get_subscribed_nodes(chat_id, net)

        if subbed_nodes:
            up, down, standby = [], [], []
            text = ""
            nodes = get_nodes(net, subbed_nodes)
            for node in nodes:
                if node.status == "up":
                    up.append(node.nodeId)
                elif node.status == "down":
                    down.append(node.nodeId)
                elif node.status == "standby":
                    standby.append(node.nodeId)
            text = format_nodes(up, down, standby)
            send_message(context, chat_id, text=text)
        else:
            send_message(context, chat_id, text="Please specify a node id")


def status_ping(update: Update, context: CallbackContext):
    """
    Get the node status using a ping.
    """
    chat_id = update.effective_chat.id
    send_message(context, chat_id, text="Ping is disabled for now.")
    return


def subscribe(update: Update, context: CallbackContext):
    chat_id = update.effective_chat.id
    db = context.bot_data["db"]

    # Get current network for this chat
    net = db.get_chat_network(chat_id)

    # Get currently subscribed nodes
    current_subs = db.get_subscribed_nodes(chat_id, net)

    node_ids = []
    if context.args:
        try:
            for arg in context.args:
                node_ids.append(int(arg))
        except ValueError:
            send_message(
                context,
                chat_id,
                text="There was a problem processing your input. This command accepts one or more node ids separated by a space.",
            )
            return
    else:
        if current_subs:
            send_message(
                context,
                chat_id,
                text="You are currently subscribed to node" + format_list(current_subs),
            )
            return
        else:
            send_message(context, chat_id, text="You are not subscribed to any nodes")
            return

    try:
        # Get node data for new subscriptions
        new_ids = [n for n in node_ids if n not in current_subs]
        new_nodes = {node.nodeId: node for node in get_nodes(net, new_ids)}

        if new_nodes:
            # Add nodes to database first
            for node_id, node in new_nodes.items():
                db.create_node(node, net)

                # Fetch and store violations for the newly added node
                con, periods = get_con_and_periods()
                if node_used_farmerbot(con, node_id):
                    violations = get_violations(con, node_id, periods)
                    if violations:
                        db.add_violations(node_id, net, violations)

            # Add all subscriptions in one go
            db.add_subscriptions(chat_id, net, list(new_nodes.keys()))

            new_subs = [n for n in node_ids if n in new_nodes]
        else:
            text = "No valid node ids found to add. Either the nodes don't exist or you were already subscribed to them."
            if current_subs:
                text += " You are currently subscribed to node" + format_list(
                    current_subs
                )
            send_message(context, chat_id, text=text)
            return

    except:
        logging.exception("Failed to fetch node info")
        send_message(
            context,
            chat_id,
            text="Error fetching node data. Please wait a moment and try again.",
        )
        return

    msg = "You have been successfully subscribed to node" + format_list(new_subs)

    if current_subs:
        msg += "\n\nYou are now subscribed to node" + format_list(
            current_subs + new_subs
        )

    send_message(context, chat_id, text=msg)


def timeout(update: Update, context: CallbackContext):
    """
    Sets a custom ping timeout for the user.
    """
    chat_id = update.effective_chat.id
    db = context.bot_data["db"]

    if context.args:
        try:
            timeout = int(context.args[0])
            if timeout <= 0:
                raise ValueError("Timeout must be positive")

            db.set_chat_timeout(chat_id, timeout)
            send_message(
                context,
                chat_id,
                text="Ping timeout successfully set to {} seconds.".format(timeout),
            )
        except ValueError:
            send_message(
                context,
                chat_id,
                text="There was a problem processing your input. This command accepts a positive whole number timeout value in seconds",
            )
    else:
        timeout = db.get_chat_timeout(chat_id)
        send_message(
            context,
            chat_id,
            text="Timeout currently set for {} seconds.".format(timeout),
        )


def unsubscribe(update: Update, context: CallbackContext):
    chat_id = update.effective_chat.id
    db = context.bot_data["db"]

    # Get current network for this chat
    net = db.get_chat_network(chat_id)

    # Get currently subscribed nodes
    current_subs = db.get_subscribed_nodes(chat_id, net)

    if not current_subs:
        send_message(context, chat_id, text="You weren't subscribed to any updates.")
        return

    if context.args and context.args[0] == "all":
        # Remove all subscriptions for this chat
        for node_id in current_subs:
            db.remove_subscription(chat_id, net, node_id)
        send_message(
            context,
            chat_id,
            text=f"You have been unsubscribed from all nodes on {net}net",
        )
    elif context.args:
        removed_nodes = []
        node_ids = []
        for node in context.args:
            try:
                node_id = int(node)
                if node_id in current_subs:
                    node_ids.append(node_id)
                    removed_nodes.append(node_id)
            except ValueError:
                pass

        if node_ids:
            db.remove_subscriptions(chat_id, net, node_ids)

        if removed_nodes:
            send_message(
                context,
                chat_id,
                text="You have been unsubscribed from node"
                + format_list(removed_nodes),
            )
        else:
            send_message(
                context, chat_id, text="No valid and subscribed node ids found."
            )
    else:
        send_message(
            context,
            chat_id,
            text='Please write "/unsubscribe all" if you wish to remove all subscribed nodes.',
        )


def violations(update: Update, context: CallbackContext):
    chat_id = update.effective_chat.id

    # This is mostly copied from the subscribe command. TODO: refactor?
    node_ids = []
    if context.args:
        try:
            for arg in context.args:
                node_ids.append(int(arg))
            using_subs = False
        except ValueError:
            send_message(
                context,
                chat_id,
                text="There was a problem processing your input. This command accepts one or more node ids separated by a space.",
            )
            return
    else:
        db = context.bot_data["db"]
        net = db.get_chat_network(chat_id)
        subbed_nodes = db.get_subscribed_nodes(chat_id, net)
        if not subbed_nodes:
            send_message(
                context,
                chat_id,
                text="No input detected and no active subscriptions. Please try again with one or more valid node ids.",
            )
            return
        else:
            node_ids = subbed_nodes
            using_subs = True

    farmerbot_node_ids = []
    for node_id in node_ids:
        con = sqlite3.connect(args.db_file)
        exists = con.execute(
            "SELECT 1 FROM PowerTargetChanged WHERE node_id=?", (node_id,)
        ).fetchone()
        if exists:
            farmerbot_node_ids.append(node_id)

    if not farmerbot_node_ids:
        send_message(
            context,
            chat_id,
            text="None of the nodes to check appear to have used the farmerbot.",
        )
        return
    else:
        if using_subs:
            send_message(context, chat_id, text="Checking for violations...")
        else:
            send_message(
                context,
                chat_id,
                text="Checking node{} for violations...".format(
                    format_list(farmerbot_node_ids)
                ),
            )

        current_period = Period()
        text = ""
        for node_id in sorted(farmerbot_node_ids):
            violations = find_violations.check_node(con, node_id, current_period)
            if violations:
                text += format_violations(node_id, violations) + "\n"
        if text:
            send_message(context, chat_id, text=text)
        else:
            send_message(context, chat_id, text="No violations found")


def heartbeat_job(context: CallbackContext):
    leader_info = get_leader(context.bot_data["db"])

    if not leader_info or not is_leader_valid(leader_info):
        # Leader is invalid, try to become leader
        if not update_leader(context.bot_data["db"]):
            logging.error("Failed to update leader, shutting down")
            os._exit(1)
        return

    current_leader_id = leader_info.split(":")[0]
    if current_leader_id != args.node_id:
        logging.info(f"Another node ({current_leader_id}) is now leader, shutting down")
        os._exit(0)

    # Update our heartbeat
    if not update_leader(context.bot_data["db"]):
        logging.error("Failed to update heartbeat, shutting down")

        os._exit(1)


parser = argparse.ArgumentParser()
parser.add_argument("token", help="Specify a bot token")
parser.add_argument("--rqlite-host", help="Rqlite host", default="localhost")
parser.add_argument("--rqlite-port", help="Rqlite port", type=int, default=4001)
parser.add_argument("-v", "--verbose", help="Verbose output", action="store_true")
parser.add_argument(
    "-p", "--poll", help="Set polling frequency in seconds", type=int, default=60
)
parser.add_argument("-t", "--test", help="Enable test feature", action="store_true")
parser.add_argument(
    "-f", "--db_file", help="Specify file for sqlite db", type=str, default="tfchain.db"
)
parser.add_argument(
    "--node-id", help="Unique node ID for leader election", default=str(uuid.uuid4())
)
parser.add_argument(
    "--heartbeat-interval",
    help="Heartbeat interval in seconds",
    type=int,
    default=DEFAULT_HEARTBEAT_INTERVAL,
)
args = parser.parse_args()

# pickler = PicklePersistence(filename='bot_data')

defaults = Defaults(parse_mode=ParseMode.HTML)
updater = Updater(
    token=args.token, persistence=None, use_context=True, defaults=defaults
)

dispatcher = updater.dispatcher

mainnet_gql = grid3.graphql.GraphQL("https://graphql.grid.tf/graphql")
testnet_gql = grid3.graphql.GraphQL("https://graphql.test.grid.tf/graphql")
devnet_gql = grid3.graphql.GraphQL("https://graphql.dev.grid.tf/graphql")

graphqls = {"main": mainnet_gql, "test": testnet_gql, "dev": devnet_gql}

if args.verbose:
    log_level = logging.INFO

    # Force fetching the schemas when verbose so they don't dump on console
    for gql in graphqls.values():
        gql.fetch_schema()
else:
    log_level = logging.WARNING

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=log_level,
    handlers=[
        logging.FileHandler("/var/log/node-status-bot.log"),
        logging.StreamHandler(),
    ],
)

# Anyone commands
dispatcher.add_handler(CommandHandler("chat_id", check_chat))
dispatcher.add_handler(CommandHandler("network", network))
dispatcher.add_handler(CommandHandler("net", network))
dispatcher.add_handler(CommandHandler("ping", status_ping))
dispatcher.add_handler(CommandHandler("start", start))
dispatcher.add_handler(CommandHandler("help", start))
dispatcher.add_handler(CommandHandler("status", status_gql))
dispatcher.add_handler(CommandHandler("subscribe", subscribe))
dispatcher.add_handler(CommandHandler("sub", subscribe))
dispatcher.add_handler(CommandHandler("timeout", timeout))
dispatcher.add_handler(CommandHandler("unsubscribe", unsubscribe))
dispatcher.add_handler(CommandHandler("unsub", unsubscribe))
dispatcher.add_handler(CommandHandler("violations", violations))

if args.test:
    import json

    get_nodes = get_nodes_from_file

initialize_dbs(dispatcher.bot_data)

while True:
    try:
        db = dispatcher.bot_data["db"]
        break
    except ConnectionRefusedError:
        time.sleep(DB_RETRY_WAIT)

# Add random jitter before proceeding with leader logic. If all nodes start
# simultaneously, they might all try setting themselves as the leader and
# proceeding to connect to Telegram. While eventually one will prevail, that
# takes a couple of heartbeat intervals
initial_jitter = random.uniform(0, JITTER_TIME)
time.sleep(initial_jitter)

while True:
    leader_info = get_leader(db)

    if not leader_info or not is_leader_valid(leader_info):
        # Try to become leader
        if update_leader(db):
            break

    # Wait and check again
    time.sleep(args.heartbeat_interval)

# We're now the leader
logging.info(f"Node {args.node_id} is now the leader")
# Flush the logs so we can always see leader changes immediately
for handler in logging.getLogger().handlers:
    handler.flush()

updater.bot.delete_my_commands()
updater.bot.set_my_commands(
    [
        ("help", "Show more details on commands and example usage."),
        (
            "status",
            "Get current status of nodes. With no input, show status for all subscribed nodes.",
        ),
        (
            "violations",
            "Check if node has any farmerbot violations. With no input, shows a report for subscribed nodes.",
        ),
        (
            "subscribe",
            "Start alerts for one or more nodes. With no input, shows currently subscribed nodes.",
        ),
        (
            "unsubscribe",
            'Stop alerts for one or more nodes. Use "/unsubscribe all" to stop all alerts.',
        ),
        ("network", 'Change the network to "dev", "test", or "main"'),
    ]
)

populate_violations(dispatcher.bot_data)
updater.job_queue.run_repeating(check_job, interval=args.poll, first=1)
updater.job_queue.run_repeating(heartbeat_job, interval=args.heartbeat_interval)

updater.start_polling()
updater.idle()
