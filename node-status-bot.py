import logging, argparse, time, sqlite3
from datetime import datetime

import telegram
from telegram import Update, ParseMode
from telegram.ext import Updater, CallbackContext, CommandHandler, PicklePersistence, Defaults

from gql import Client, gql
from gql.transport.requests import RequestsHTTPTransport
from gql.transport.exceptions import TransportServerError

import grid3.graphql
import grid3.minting
from grid3.types import Node

import find_violations

# from grid3.rmb import RmbClient, RmbPeer

# Technically Telegram supports messages up to 4096 characters, beyond which an
# error is returned. However in my experience, messages longer than 3800 chars
# with html formatting don't get formatted past 3800
MAX_TEXT_LENGTH = 3800

NETWORKS = ['main', 'test', 'dev']
DEFAULT_PING_TIMEOUT = 10
BOOT_TOLERANCE = 60 * 40

parser = argparse.ArgumentParser()
parser.add_argument('token', help='Specify a bot token')
parser.add_argument('-s', '--secret', 
                    help='A TF Chain secret for use with RMB', type=str)
parser.add_argument('-v', '--verbose', help='Verbose output', 
                    action="store_true")
parser.add_argument('-p', '--poll', help='Set polling frequency in seconds', 
                    type=int, default=60)
parser.add_argument('-a', '--admin', help='Set the admin chat id', type=int)
parser.add_argument('-t', '--test', help='Enable test feature', 
                    action="store_true")
parser.add_argument('-d', '--dump', help='Dump bot data', action="store_true")
parser.add_argument('-f', '--db_file', 
                    help='Specify file for sqlite db', type=str, default='tfchain.db')
args = parser.parse_args()

pickler = PicklePersistence(filename='bot_data')

defaults = Defaults(parse_mode=ParseMode.HTML)
updater = Updater(token=args.token, persistence=pickler, use_context=True, defaults=defaults)

dispatcher = updater.dispatcher

mainnet_gql = grid3.graphql.GraphQL('https://graphql.grid.tf/graphql')
testnet_gql = grid3.graphql.GraphQL('https://graphql.test.grid.tf/graphql')
devnet_gql = grid3.graphql.GraphQL('https://graphql.dev.grid.tf/graphql')

graphqls = {'main': mainnet_gql,
            'test': testnet_gql,
            'dev': devnet_gql}

# Ping is disabled for now
# if args.secret is None:
#     print('Secret is required for RMB functions. Please specify with -s or --secret')
#     exit()

# rmb_peers = {net: RmbPeer(args.secret, net, net + '-rmb-peer.log',
#                           spawn_redis=True, redis_port=None,
#                           redis_logfile=net + '-redis.log')
#              for net in NETWORKS}

# rmb_clients = {net: RmbClient(rmb_peers[net].redis_port) for net in NETWORKS}

if args.verbose:
    log_level = logging.INFO

    #Force fetching the schemas when verbose so they don't dump on console
    for gql in graphqls.values():
        gql.fetch_schema()
else:
    log_level = logging.WARNING

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=log_level, 
    handlers=[logging.FileHandler('logs'), logging.StreamHandler()])

def check_chat(update: Update, context: CallbackContext):
    chat = update.effective_chat.id
    send_message(context, chat, text='Your chat id is {}'.format(chat))

def check_job(context: CallbackContext):
    """
    The main attraction. This function collects all the node ids that have an active subscription, checks their status, then sends alerts to users whose nodes have a status change.
    """
    con, periods = get_con_and_periods()

    for net in NETWORKS:
        # First gather all actively subscribed nodes and note who is subscribed
        try:
            subbed_nodes = {}

            for chat_id, data in context.bot_data['chats'].items():
                for node_id in data['nodes'][net]:
                    subbed_nodes.setdefault(node_id, []).append(chat_id)
            updates = get_nodes(net, subbed_nodes)

            if net == 'main':
                farmerbot_nodes = [n for n in subbed_nodes]
                all_violations = {}
                for node_id in farmerbot_nodes:
                    all_violations[node_id] = get_violations(con, node_id, periods)

        except:
            logging.exception("Error fetching node data for check")
            continue

        for update in updates:
            try:
                node = context.bot_data['nodes'][net][update.nodeId]

                if node.power['target'] == 'Down' and update.power['target'] == 'Up':
                    for chat_id in subbed_nodes[node.nodeId]:
                        send_message(context, chat_id, text='Node {} wake up initiated \N{hot beverage}'.format(node.nodeId))

                if node.status == 'up' and update.status == 'down':
                    for chat_id in subbed_nodes[node.nodeId]:
                        send_message(context, chat_id, text='Node {} has gone offline \N{warning sign}'.format(node.nodeId))

                elif node.status == 'up' and update.status == 'standby':
                    for chat_id in subbed_nodes[node.nodeId]:
                        send_message(context, chat_id, text='Node {} has gone to sleep \N{last quarter moon with face}'.format(node.nodeId))

                elif node.status == 'standby' and update.status == 'down':
                    for chat_id in subbed_nodes[node.nodeId]:
                        send_message(context, chat_id, text='Node {} did not wake up within 24 hours \N{warning sign}'.format(node.nodeId))

                elif node.status in ('down', 'standby') and update.status == 'up':
                    for chat_id in subbed_nodes[node.nodeId]:
                        send_message(context, chat_id, text='Node {} has come online \N{electric light bulb}'.format(node.nodeId))

                # We track which nodes have ever been managed by farmerbot, since those are the only ones that can get violations and scanning for violations is a relatively expensive operation
                if node.status == 'standby' or update.status == 'standby':
                    node.farmerbot = True

                if node.nodeId in all_violations:
                    violations = all_violations[node.nodeId]
                    for v in violations:
                        if v.boot_requested not in node.violations: 
                            if v.finalized:
                                for chat_id in subbed_nodes[node.nodeId]:
                                    send_message(context, chat_id, text='🚨 Farmerbot violation detected for node {}. Node failed to boot within 30 minutes 🚨\n\n{}'.format(node.nodeId, format_violation(v)))
                            elif v.end_time - v.boot_requested > BOOT_TOLERANCE:
                                for chat_id in subbed_nodes[node.nodeId]:
                                    send_message(context, chat_id, text='🚨 Probable farmerbot violation detected for node {}. Node appears to have not booted within 30 minutes of boot request. Check again with /violations after node boots 🚨\n\n{}'.format(node.nodeId, format_violation(v)))
                        
                        # We do this every time because information about when a node finally booted might become available later. Right now we don't use this info though. Might be effective as a cache for manual violation lookups
                        node.violations[v.boot_requested] = v

            except:
                logging.exception("Error in alert block")

            finally:
                node.status = update.status
                node.updatedAt = update.updatedAt
                node.power = update.power

def format_list(items):
    if len(items) == 1:
        text = ' ' + str(items[0])
    elif len(items) == 2:
        text = 's ' + str(items[0]) + ' and ' + str(items[1])
    else:
        text = 's '
        for i in items[:-1]:
            text = text + str(i) + ', '
        text = text + 'and ' + str(items[-1])
    return text

def format_nodes(up, down, standby):
    up.sort()
    down.sort()
    standby.sort()
    text = ''

    if up:
        text += '<b><u>Up nodes:</u></b>\n'
        text += format_vertical_list(up)
    if down:
        if up:
            text += '\n'
        text += '<b><u>Down nodes:</u></b>\n'
        text += format_vertical_list(down)
    if standby:
        if up or down:
            text += '\n'
        text += '<b><u>Standby nodes:</u></b>\n'
        text += format_vertical_list(standby)

    return text

def format_vertical_list(items):
    text = ''
    for item in items:
        text += str(item) + '\n'
    return text

def format_violation(violation):
    text = ''
    requested = datetime.fromtimestamp(violation.boot_requested)
    text += '<i>Boot requested at:</i>\n'
    text += '{} UTC\n'.format(requested)
    if violation.booted_at:
        booted = datetime.fromtimestamp(violation.booted_at)
        text += '<i>Node booted at:</i>\n'
        text += '{} UTC\n'.format(booted)
    else:
        text += 'Node has not booted\n'
    return text

def format_violations(node_id, violations):
    text = '<b><u>Violations for node {}:</u></b>\n\n'.format(node_id)
    for violation in violations:
        requested = datetime.fromtimestamp(violation.boot_requested)
        text += '<i>Boot requested at:</i>\n'
        text += '{} UTC\n'.format(requested)
        if violation.booted_at:
            booted = datetime.fromtimestamp(violation.booted_at)
            text += '<i>Node booted at:</i>\n'
            text += '{} UTC\n'.format(booted)
        else:
            text += 'Node has not booted\n'
        text += '\n'
    return text

def get_con_and_periods():
    con = sqlite3.connect(args.db_file)
    current_period = grid3.minting.Period()
    last_period = grid3.minting.Period(offset=current_period.offset - 1)
    periods = (current_period, last_period)
    return con, periods

def get_nodes(net, node_ids):
    """
    Query a list of node ids in GraphQL, create Node objects for consistency and easy field access, then assign them a status and return them.
    """
    graphql = graphqls[net]
    nodes = graphql.nodes(['nodeID', 'twinID', 'updatedAt', 'power'], 
                          nodeID_in=node_ids)
    nodes = [Node(node) for node in nodes]

    one_hour_ago = time.time() - 60 * 60

    for node in nodes:
        if node.power is None: 
            node.power = {'state': None, 'target': None}
        node.status = get_node_status(node)

        if node.status == 'standby':
            node.farmerbot = True
        else:
            node.farmerbot = False
        node.violations = {}

    return nodes

def get_nodes_from_file(net, node_ids):
    """
    For use in test mode, to emulate get_nodes using data in a file. The updatedAt value is given in the file as a delta of how many seconds in the past and converted to absolute time here
    """
    if net == 'main':
        text = open('./test/node', 'r').read()
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
    if node.updatedAt > one_hour_ago and node.power['state'] != 'Down':
        return 'up'
    elif node.power['state'] == 'Down' and node.updatedAt > one_day_ago:
        return 'standby'
    else:
        return 'down'

def get_violations(con, node_id, periods):
    violations = []
    for period in periods:
        violations.extend(find_violations.check_node(con, node_id, period))
    return violations

def initialize(bot_data):
    for key in ['chats', 'nodes']:
        bot_data.setdefault(key, {})

    for net in NETWORKS:
        bot_data['nodes'].setdefault(net, {})

    subs = 0
    for chat, data in bot_data['chats'].items():
        for net in NETWORKS:
            if data['nodes'][net]:
                subs += 1
                break
    print('{} chats and {} subscribed users'.format(len(bot_data['chats']), subs))

def migrate_data(bot_data):
    """
    Convert dict based node data to instances of Node class. Only needed when updating a bot that has existing data using the old style.
    """
    for net in NETWORKS:
        nodes = bot_data['nodes'][net]
        for node_id in nodes.keys():
            if type(nodes[node_id]) is dict:
                nodes[node_id]['nodeID'] = node_id
                nodes[node_id] = Node(nodes[node_id])

def network(update: Update, context: CallbackContext):
    chat_id = update.effective_chat.id
    user = context.bot_data['chats'].setdefault(chat_id, new_user())

    user = context.bot_data['chats'].setdefault(chat_id, new_user())
    if context.args:
        net = context.args[0]
        if net in NETWORKS:
            user['net'] = net
            send_message(context, chat_id, text='Set network to {}net'.format(net))
        else:
            send_message(context, chat_id, text='Please specify a valid network: dev, test, or main')
    else:
        net = user['net']
        send_message(context, chat_id, text='Network is set to {}net'.format(net))

def new_user():
    return {'net': 'main', 'nodes': {'main': [], 'test': [], 'dev': []}}

def node_used_farmerbot(con, node_id):
    # Check if the node ever went standby, which is a requirement for it to receive a violation
    result = con.execute("SELECT 1 FROM PowerStateChanged WHERE node_id=? AND state='Down'", (node_id,)).fetchone()
    return result is not None

def ping_rmb(net, nodes, timeout):
    """
    Ping one or more nodes via RMB.
    """
    client = rmb_clients[net]
    twins = [node.twinId for node in nodes]

    # Even with exp set, we can still get replies after the timeout, this means we should flush the queue before starting and/or check timestamps on incoming messages. It also means we can't stop receiving when number of received messages equals number of nodes queried, since replies for other nodes can come in and cause a false failure.
    client.send('zos.statistics.get', twins, exp_delta=timeout)

    finished = time.time() + timeout
    replies = []
    remaining = timeout
    while remaining > 0 and len(replies) < len(nodes):
        if reply := client.receive(remaining):
            replies.append(reply)

        remaining = finished - time.time()

    twins_replied = [int(reply['src']) for reply in replies]
    up_nodes = [node for node in nodes if node.twinId in twins_replied]
    return up_nodes

def populate_violations(bot_data):
    # Since we only want to notify users about _new_ violations, we need to establish a baseline at some point (when the feature is enabled or when a new bot is started for the first time)
    if bot_data.setdefault('violations_populated', False):
        return

    # We only track violations for mainnet
    nodes = bot_data['nodes']['main']

    con, periods = get_con_and_periods()
    
    for node_id, node in nodes.items():
        violations = get_violations(con, node_id, periods)
        # Violations are uniquely identified per node by their first field (time that wake up was initiated). Storing them in this form helps to easily identify new violations later
        node.violations =  {v.boot_requested: v for v in violations}
        try:
            if violations or node.status == 'standby':
                node.farmerbot = True
            else:
                node.farmerbot = False
        # It's possible when migrating from old style bot data that some node objects don't have a status field
        except AttributeError:
            node.farmerbot = False

    bot_data['violations_populated'] = True

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
        logging.exception('Error sending message')

def split_message(text):
    # The only messages that get over length at the time of writing this
    # function are violations reports. Since each node's violations are
    # separated by two blank lines, we can split on those and avoid a message
    # break in the middle of one node's section
    messages = []
    message = ''
    splitter = '\n\n\n'
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
    context.bot_data['chats'].setdefault(chat_id, new_user())
    msg = '''
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
    '''
    send_message(context, chat_id, text=msg)

def status_gql(update: Update, context: CallbackContext):
    chat_id = update.effective_chat.id
    user = context.bot_data['chats'].setdefault(chat_id, new_user())
        
    net = user['net']

    if context.args:
        try:
            node = get_nodes(net, context.args)[0]
            send_message(context, chat_id, text='Node {} is {}'.format(node.nodeId, node.status))
        except IndexError:
            send_message(context, chat_id, text='Node id not valid on {}net'.format(net))
        except:
            logging.exception("Failed to fetch node info")
            send_message(context, chat_id, text='Error fetching node data. Please wait a moment and try again.')

    else:
        subbed_nodes = user['nodes'][net]

        if subbed_nodes:
            up, down, standby = [], [], []
            text = ''
            nodes = get_nodes(net, subbed_nodes)
            for node in nodes:
                if node.status == 'up':
                    up.append(node.nodeId)
                elif node.status == 'down':
                    down.append(node.nodeId)
                elif node.status == 'standby':
                    standby.append(node.nodeId)
            text = format_nodes(up, down, standby)
            send_message(context, chat_id, text=text)
        else:
            send_message(context, chat_id, text='Please specify a node id')

def status_ping(update: Update, context: CallbackContext):
    """
    Get the node status using a ping over RMB.
    """

    chat_id = update.effective_chat.id
    user = context.bot_data['chats'].setdefault(chat_id, new_user())

    send_message(context, chat_id, text='Ping is disabled for now.')
    return

    try:
        timeout = user['timeout']
    except KeyError:
        timeout = DEFAULT_PING_TIMEOUT
    net = user['net']

    if context.args:
        try:
            node_ids = [int(arg) for arg in context.args]
        except ValueError:
            send_message(context, chat_id, text='There was a problem processing your input. This command accepts one or more node ids separated by a space.')
            return
        
        nodes = get_nodes(net, node_ids)
        if not nodes:
            send_message(context, chat_id, text='There was a problem processing your input. No valid node ids for this network.')
            return

        send_message(context, chat_id, 
                                 text='Pinging with {} second timeout...'
                                      .format(timeout))
        up_nodes = ping_rmb(net, nodes, timeout)

        if len(nodes) == 1:
            if up_nodes:
                send_message(context, chat_id, text='Node {} responded successfully.'.format(nodes[0].nodeId))
            else:
                send_message(context, chat_id, text='Node {} did not respond.'.format(nodes[0].nodeId))

        else:
            msg = ''
            if up_nodes:
                msg += '<b><u>Responsive nodes:</u></b>\n'
                msg += format_vertical_list([node.nodeId for node in up_nodes])

            if len(up_nodes) < len(nodes):
                rest = [node.nodeId for node in nodes if node not in up_nodes]
                if up_nodes:
                    msg += '\n'
                msg += '<b><u>Unresponsive nodes:</u></b>\n'
                msg += format_vertical_list(rest)

            send_message(context, chat_id, text=msg)

def subscribe(update: Update, context: CallbackContext):
    chat_id = update.effective_chat.id
    user = context.bot_data['chats'].setdefault(chat_id, new_user())

    net = user['net']
    subbed_nodes = user['nodes'][net]

    node_ids = []
    if context.args:
        try:
            for arg in context.args:
                node_ids.append(int(arg))
        except ValueError:
            send_message(context, chat_id, text='There was a problem processing your input. This command accepts one or more node ids separated by a space.')
            return
    else:
        if subbed_nodes:
            send_message(context, chat_id, text='You are currently subscribed to node' + format_list(subbed_nodes))
            return
        else:
            send_message(context, chat_id, text='You are not subscribed to any nodes')
            return
    
    try:
        new_ids = [n for n in node_ids if n not in subbed_nodes]
        new_nodes = {node.nodeId: node for node in get_nodes(net, new_ids)}
        if new_nodes:
            # If there are nodes we haven't seen before, we need to check if they have been using farmerbot and if so preload any existing violations so they don't trigger alerts
            known_nodes = context.bot_data['nodes'][net]
            unknown_nodes = new_nodes.keys() - known_nodes.keys()
            if unknown_nodes:
                con, periods = get_con_and_periods()
                for node_id in unknown_nodes:
                    if node_used_farmerbot(con, node_id):
                        violations = get_violations(con, node_id, periods)
                        new_nodes[node_id].violations = {v.boot_requested: v for v in violations}
                    
            known_nodes.update(new_nodes)
            # Do this to preserve the order since gql does not
            new_subs = [n for n in node_ids if n in new_nodes]
        else:
            text = 'No valid node ids found to add.'
            if subbed_nodes:
                text += ' You are currently subscribed to node' + format_list(subbed_nodes)
            send_message(context, chat_id, text=text)
            return
    
    except:
        logging.exception("Failed to fetch node info")
        send_message(context, chat_id, text='Error fetching node data. Please wait a moment and try again.')
        return

    msg = 'You have been successfully subscribed to node' + format_list(new_subs)

    if subbed_nodes:
        msg += '\n\nYou are now subscribed to node' + format_list(subbed_nodes + new_subs)
    
    subbed_nodes.extend(new_subs)
    send_message(context, chat_id, text=msg)

def timeout(update: Update, context: CallbackContext):
    """
    Sets a custom ping timeout for the user.
    """
    chat_id = update.effective_chat.id
    user = context.bot_data['chats'].setdefault(chat_id, new_user())

    if context.args:
        try:
            timeout = int(context.args[0])
        except ValueError:
            send_message(context, chat_id, text='There was a problem processing your input. This command accepts a whole number timeout value in seconds')
            return

        user['timeout'] = timeout
        send_message(context, chat_id, text='Ping timeout successfully set to {} seconds.'.format(timeout))

    else:
        try:
            timeout = user['timeout']
        except KeyError:
            timeout = DEFAULT_PING_TIMEOUT
        send_message(context, chat_id, text='Timeout currently set for {} seconds.'.format(timeout))

def unsubscribe(update: Update, context: CallbackContext):
    chat_id = update.effective_chat.id
    user = context.bot_data['chats'].setdefault(chat_id, new_user())

    if len(user['nodes']) == 0:
        send_message(context, chat_id, text="You weren't subscribed to any updates.")
    else:
        if context.args and context.args[0] == 'all':
            for net in NETWORKS:
                user['nodes'][net] = []
            send_message(context, chat_id, text='You have been unsubscribed from all updates')

        elif context.args:
            removed_nodes = []
            net = user['net']
            subbed_nodes = user['nodes'][net]
            for node in context.args:
                try:
                    subbed_nodes.remove(int(node))
                    removed_nodes.append(node)
                except ValueError:
                    pass
            if removed_nodes:
                send_message(context, chat_id, text='You have been unsubscribed from node' + format_list(removed_nodes))
            else:
                send_message(context, chat_id, text='No valid and subscribed node ids found.')

        else:
            send_message(context, chat_id, text='Please write "/unsubscribe all" if you wish to remove all subscribed nodes.')

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
            send_message(context, chat_id, text='There was a problem processing your input. This command accepts one or more node ids separated by a space.')
            return
    else:
        user = context.bot_data['chats'].setdefault(chat_id, new_user())
        subbed_nodes = user['nodes'][user['net']]
        if not subbed_nodes:
            send_message(context, chat_id, text='No input detected and no active subscriptions. Please try again with one or more valid node ids.')
            return
        else:
            node_ids = subbed_nodes
            using_subs = True

    farmerbot_node_ids = []
    for node_id in node_ids:
        con = sqlite3.connect(args.db_file)
        exists = con.execute('SELECT 1 FROM NodeUptimeReported WHERE node_id=?', (node_id,)).fetchone()
        if exists:
            farmerbot_node_ids.append(node_id)

    if not farmerbot_node_ids:
        send_message(context, chat_id, text='None of the nodes to check appear to have used the farmerbot.')
        return
    else:
        if using_subs:
            send_message(context, chat_id, text='Checking for violations...')
        else:
            send_message(context, chat_id, text='Checking node{} for violations...'.format(format_list(farmerbot_node_ids)))

        current_period = grid3.minting.Period()
        text = ''
        for node_id in sorted(farmerbot_node_ids):
            violations = find_violations.check_node(con, node_id, current_period)
            if violations:
                text += format_violations(node_id, violations) + '\n'
        if text:
            send_message(context, chat_id, text=text)
        else:
            send_message(context, chat_id, text='No violations found')

# Anyone commands
dispatcher.add_handler(CommandHandler('chat_id', check_chat))
dispatcher.add_handler(CommandHandler('network', network))
dispatcher.add_handler(CommandHandler('net', network))
dispatcher.add_handler(CommandHandler('ping', status_ping))
dispatcher.add_handler(CommandHandler('start', start))
dispatcher.add_handler(CommandHandler('help', start))
dispatcher.add_handler(CommandHandler('status', status_gql))
dispatcher.add_handler(CommandHandler('subscribe', subscribe))
dispatcher.add_handler(CommandHandler('sub', subscribe))
dispatcher.add_handler(CommandHandler('timeout', timeout))
dispatcher.add_handler(CommandHandler('unsubscribe', unsubscribe))
dispatcher.add_handler(CommandHandler('unsub', unsubscribe))
dispatcher.add_handler(CommandHandler('violations', violations))

updater.bot.delete_my_commands()
updater.bot.set_my_commands([   
    ('help', 'Show more details on commands and example usage.'), 
    ('status', 'Get current status of nodes. With no input, show status for all subscribed nodes.'),
    ('violations', 'Check if node has any farmerbot violations. With no input, shows a report for subscribed nodes.'),
    ('subscribe', 'Start alerts for one or more nodes. With no input, shows currently subscribed nodes.'),
    ('unsubscribe', 'Stop alerts for one or more nodes. Use "/unsubscribe all" to stop all alerts.'),
    ('network', 'Change the network to "dev", "test", or "main"')
    ])

if args.test:
    import json
    get_nodes = get_nodes_from_file

if args.dump:
    print('Bot data:')
    print(dispatcher.bot_data)
    print()

initialize(dispatcher.bot_data)
migrate_data(dispatcher.bot_data)
populate_violations(dispatcher.bot_data)
updater.job_queue.run_repeating(check_job, interval=args.poll, first=1)

updater.start_polling()
updater.idle()

# Ping is disabled for now
# for peer in rmb_peers.values():
#     peer.redis.kill()
#     peer.peer.kill()
