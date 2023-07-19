import logging, requests, subprocess, argparse, time

from telegram import Update, ParseMode
from telegram.ext import Updater, CallbackContext, CommandHandler, PicklePersistence, Defaults

from gql import Client, gql
from gql.transport.requests import RequestsHTTPTransport
from gql.transport.exceptions import TransportServerError

import grid_graphql
from grid_types import Node

GRID_NETWORKS = ['main', 'test', 'dev']

parser = argparse.ArgumentParser()
parser.add_argument('token', help='Specify a bot token')
parser.add_argument('-v', '--verbose', help='Verbose output', action="store_true")
parser.add_argument('-p', '--poll', help='Set polling frequency in seconds', type=int, default=300)
parser.add_argument('-l', '--logs', help='Specify how many lines the log file must grow before a notification is sent to the admin', type=int, default=10)
parser.add_argument('-a', '--admin', help='Set the admin chat id', type=int)
parser.add_argument('-t', '--test', help='Enable test feature', action="store_true")
parser.add_argument('-d', '--dump', help='Dump bot data', action="store_true")
args = parser.parse_args()

pickler = PicklePersistence(filename='bot_data')

defaults = Defaults(parse_mode=ParseMode.HTML)
updater = Updater(token=args.token, persistence=pickler, use_context=True, defaults=defaults)

dispatcher = updater.dispatcher

mainnet_gql = grid_graphql.GraphQL('https://graphql.grid.tf/graphql')
testnet_gql = grid_graphql.GraphQL('https://graphql.test.grid.tf/graphql')
devnet_gql = grid_graphql.GraphQL('https://graphql.dev.grid.tf/graphql')

graphqls = {'main': mainnet_gql,
            'test': testnet_gql,
            'dev': devnet_gql}

if args.verbose:
    log_level = logging.INFO

    #Force fetching the schemas when verbose so they don't dump on console
    for gql in graphqls.values():
        gql.fetch_schema()
else:
    log_level = logging.WARNING

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=log_level)

def check_job(context: CallbackContext):
    """
    The main attraction, when it's working properly. This function collects all the node ids that have an active subscription, checks their status via both proxy and ping, then sends alerts to users whose nodes have a status change.
    """
    for net in GRID_NETWORKS:
        # First gather all actively subscribed nodes and note who is subscribed
        try:
            subbed_nodes = {}

            for chat_id, data in context.bot_data['chats'].items():
                for node_id in data['nodes'][net]:
                    subbed_nodes.setdefault(node_id, []).append(chat_id)
            nodes = get_nodes(net, subbed_nodes)
        except:
            logging.exception("Error fetching node data for check")
            continue

        for node in nodes:
            try:
                previous = context.bot_data['nodes'][net][node.nodeId]

                if previous.status == 'up' and node.status == 'down':
                    for chat_id in subbed_nodes[node.nodeId]:
                        context.bot.send_message(chat_id=chat_id, text='Node {} has gone offline'.format(node.nodeId))

                elif previous.status == 'up' and node.status == 'standby':
                    for chat_id in subbed_nodes[node.nodeId]:
                        context.bot.send_message(chat_id=chat_id, text='Node {} has gone to sleep'.format(node.nodeId))

                elif previous.status == 'standby' and node.status == 'down':
                    for chat_id in subbed_nodes[node.nodeId]:
                        context.bot.send_message(chat_id=chat_id, text='Node {} did not wake up within 24 hours'.format(node.nodeId))

                elif previous.status in ('down', 'standby') and node.status == 'up':
                    for chat_id in subbed_nodes[node.nodeId]:
                        context.bot.send_message(chat_id=chat_id, text='Node {} has come online'.format(node.nodeId))

                if previous.power['target'] == 'Down' and node.power['target'] == 'Up':
                    for chat_id in subbed_nodes[node.nodeId]:
                        context.bot.send_message(chat_id=chat_id, text='Node {} wake up initiated'.format(node.nodeId))
            except:
                logging.exception("Error in alert block")

            finally:
                context.bot_data['nodes'][net][node.nodeId] = node

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
        for node in up:
            text += str(node) + '\n'
    if down:
        if up:
            text += '\n'
        text += '<b><u>Down nodes:</u></b>\n'
        for node in down:
            text += str(node) + '\n'

    if standby:
        if up or down:
            text += '\n'
        text += '<b><u>Standby nodes:</u></b>\n'
        for node in standby:
            text += str(node) + '\n'

    return text


def get_nodes(net, node_ids):
    """
    Query a list of node ids in GraphQL, create Node objects for consistency and easy field acces, then assign them a status and return them.
    """
    graphql = graphqls[net]
    nodes = graphql.nodes(['nodeID', 'updatedAt', 'power'], nodeID_in=node_ids)
    nodes = [Node(node) for node in nodes]

    one_hour_ago = time.time() - 60 * 60

    for node in nodes:
        node.status = get_node_status(node)

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

def initialize(context: CallbackContext):
    for key in ['chats', 'nodes']:
        context.bot_data.setdefault(key, {})

    for net in GRID_NETWORKS:
        context.bot_data['nodes'].setdefault(net, {})

    subs = 0
    for chat, data in context.bot_data['chats'].items():
        for net in GRID_NETWORKS:
            if data['nodes'][net]:
                subs += 1
                break
    print('{} chats and {} subscribed users'.format(len(context.bot_data['chats']), subs))

def new_user():
    return {'net': 'main', 'nodes': {'main': [], 'test': [], 'dev': []}}

def migrate_data(context: CallbackContext):
    """
    Convert dict based node data to instances of Node class. Only needed when updating a bot that has existing data using the old style.
    """
    for net in GRID_NETWORKS:
        nodes = context.bot_data['nodes'][net]
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
        if net in GRID_NETWORKS:
            user['net'] = net
            context.bot.send_message(chat_id=chat_id, text='Set network to {}net'.format(net))
        else:
            context.bot.send_message(chat_id=chat_id, text='Please specify a valid network: dev, test, or main')
    else:
        net = user['net']
        context.bot.send_message(chat_id=chat_id, text='Network is set to {}net'.format(net))

def start(update: Update, context: CallbackContext):
    chat_id = update.effective_chat.id
    context.bot_data['chats'].setdefault(chat_id, new_user())
    msg = '''
Hey there, I'm the ThreeFold Grid 3 node status bot. Beep boop.

I can give you information about whether a node is up or down right now and also notify you if its state changes in the future. Here are the commands I support:

/network (/net) - change the network to "dev", "test", or "main" (default is main). If you don't provide an input, the currently selected network is shown. 
Example: /network main

/status - check the current status of one node. This is based on Grid proxy and should match what's reported by the explorer which updates relatively slowly.
Example: /status 1

/ping - check the current status of a node via a ping over Yggdrasil. This provides more responsive output than /status, but can misreport nodes as down if there's an issue with Yggdrasil.
Example: /ping 42

/subscribe (/sub) - subscribe to updates about one or more nodes. If you don't provide an input, the nodes you are currently subscribed to will be shown. 
Example: /sub 1 2 3

/unsubscribe (/unsub) - unsubscribe from updates about one or more nodes. If you don't give an input, you'll be unsubscribed from all updates.

To report bugs, request features, or just say hi, please contact @scottyeager. Please also subscribe to the updates channel here for news on the bot: t.me/node_bot_updates

This bot is experimental and probably has bugs. Only you are responsible for your node's uptime and your farming rewards.
    '''
    context.bot.send_message(chat_id=chat_id, text=msg)

def status_gql(update: Update, context: CallbackContext):
    chat_id = update.effective_chat.id
    user = context.bot_data['chats'].setdefault(chat_id, new_user())

        
    net = user['net']

    if context.args:
        try:
            node = get_nodes(net, context.args)[0]
            context.bot.send_message(chat_id=chat_id, text='Node {} is {}'.format(node.nodeId, node.status))
        except IndexError:
            context.bot.send_message(chat_id=chat_id, text='Node id not valid on {}net'.format(net))
        except:
            logging.exception("Failed to fetch node info")
            context.bot.send_message(chat_id=chat_id, text='Error fetching node data. Please wait a moment and try again.')

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
            context.bot.send_message(chat_id=chat_id, text=text)
        else:
            context.bot.send_message(chat_id=chat_id, text='Please specify a node id')

def status_ping(update: Update, context: CallbackContext):
    """
    Get the node status using a ping over Yggdrasil, rather than checking grid proxy. This gives more real time data, but can fail if Yggdrasil is having issues. Refresh the node on each check, because they can sometimes change.
    """

    chat_id = update.effective_chat.id

    context.bot.send_message(chat_id=chat_id, text="/ping is disabled for now, until an implementation based on the new RMB is ready. Please stay tuned for an announcement on the bot's channel: https://t.me/node_bot_updates")

    return

    ###################################################################
    # DISABLED FOR NOW, WE CAN'T GET THE YGGDRASIL IPS FROM GQL ANYMORE
    ###################################################################

    user = context.bot_data['chats'].setdefault(chat_id, new_user())


    net = user['net']
    if context.args:
        node = context.args[0]
        if not check_valid(net, node):
            context.bot.send_message(chat_id=chat_id, text='Node with that id not found on this network. Please double check the node id and try again')
            return

        try:
            update_node_ip(net, node, context)
            ip = (int(node), context.bot_data['nodes'][net][int(node)]['ip'])
        # With update_node_ip, we'll only hit this path if grid proxy is down
        except KeyError:
            context.bot.send_message(chat_id=chat_id, text='Fetching node details...')
            try:
                ip = get_node_ips(net, [node])[0]
            except:
                context.bot.send_message(chat_id=chat_id, text='Error fetching node details. If this issue persists, please notify @scottyeager')
                raise
            if ip:
                context.bot_data['nodes'][net][ip[0]] = {'ip': ip[1]}

        context.bot.send_message(chat_id=chat_id, text='Pinging node {}. One moment...'.format(ip[0]))
        stat = ping(ip[1])
        # context.bot_data['nodes'][net][ip[0]]['status'] = stat
        context.bot.send_message(chat_id=chat_id, text='Node {} is {}'.format(node, stat))

    else:
        subbed_nodes = user['nodes'][net]
        if subbed_nodes:
            if len(subbed_nodes) == 1:
                context.bot.send_message(chat_id=chat_id, text='Pinging node {}. One moment...'.format(subbed_nodes[0]))
            else:
                context.bot.send_message(chat_id=chat_id, text='Pinging {} nodes. One moment...'.format(len(subbed_nodes)))
            up, down = [], []
            for node in subbed_nodes:
                update_node_ip(net, node, context)
                stat = ping(context.bot_data['nodes'][net][int(node)]['ip'])
                if stat == 'up':
                    up.append(node)
                elif stat == 'down':
                    down.append(node)

            text = format_nodes(up, down)
            context.bot.send_message(chat_id=chat_id, text=text)
        else:
            context.bot.send_message(chat_id=chat_id, text='Please specify a node id')

def subscribe(update: Update, context: CallbackContext):
    chat_id = update.effective_chat.id
    user = context.bot_data['chats'].setdefault(chat_id, new_user())

    net = user['net']
    subbed_nodes = user['nodes'][net]

    node_ids = []
    if context.args:
        try:
            for arg in context.args:
                nodes.append(int(arg))
        except:
            context.bot.send_message(chat_id=chat_id, text='There was a problem processing your input. This command accepts one or more node ids separated by a space.')
            return
    else:
        if subbed_nodes:
            context.bot.send_message(chat_id=chat_id, text='You are currently subscribed to node' + format_list(subbed_nodes))
            return
        else:
            context.bot.send_message(chat_id=chat_id, text='You are not subscribed to any nodes')
            return
    
    try:
        new_ids = [n for n in node_ids if n not in subbed_nodes]
        new_nodes = get_nodes(net, new_ids)
    
    except:
        logging.exception("Failed to fetch node info")
        context.bot.send_message(chat_id=chat_id, text='Error fetching node data. Please wait a moment and try again.')
        return


    msg = 'You have been successfully subscribed to node' + format_list(new_subs)

    if subbed_nodes:
        msg += '\n\nYou are now subscribed to node' + format_list(subbed_nodes + new_nodes)
    
    subbed_nodes.extend(new_nodes)
    context.bot.send_message(chat_id=chat_id, text=msg)

def unsubscribe(update: Update, context: CallbackContext):
    chat_id = update.effective_chat.id
    user = context.bot_data['chats'].setdefault(chat_id, new_user())

    if len(user['nodes']) == 0:
        context.bot.send_message(chat_id=chat_id, text="You weren't subscribed to any updates.")
    else:
        if context.args:
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
                context.bot.send_message(chat_id=chat_id, text='You have been unsubscribed from node' + format_list(removed_nodes))
            else:
                context.bot.send_message(chat_id=chat_id, text='No valid and subscribed node ids found.')
        elif context.args[0] == 'all':
            for net in GRID_NETWORKS:
                user['nodes'][net] = []
            context.bot.send_message(chat_id=chat_id, text='You have been unsubscribed from all updates')
        else:
            context.bot.send_message(chat_id=chat_id, text='Please write "/unsubscribe all" if you wish to remove all subscribed nodes.')

def check_chat(update: Update, context: CallbackContext):
    chat = update.effective_chat.id
    context.bot.send_message(chat_id=chat, text='Your chat id is {}'.format(chat))

def send_logs(update: Update, context: CallbackContext):
    if update.effective_chat.id != args.admin:
        return

    if context.args:
        lines = context.args[0]
    else:
        lines = 50

    with open('logs', 'r') as logs:
        log_lines = [line for line in logs]
        text = ''
        for line in log_lines:
            text += line
        if text:
            context.bot.send_message(chat_id=args.admin, text=text)
        else:
            context.bot.send_message(chat_id=args.admin, text='Log file empty')

def log_job(context: CallbackContext):
    with open('logs', 'r') as logs:
        log_length = sum(1 for line in logs)

    last_length = context.bot_data.set_default('last_log_length', log_length)

    if log_length - last_length > args.logs and args.admin:
        context.bot.send_message(chat_id=args.admin, text='Log file has grown by {} lines. Houston, we have a ...?'.format(args.logs))


# Anyone commands
dispatcher.add_handler(CommandHandler('chat_id', check_chat))
dispatcher.add_handler(CommandHandler('network', network))
dispatcher.add_handler(CommandHandler('net', network))
dispatcher.add_handler(CommandHandler('ping', status_ping))
dispatcher.add_handler(CommandHandler('start', start))
dispatcher.add_handler(CommandHandler('status', status_gql))
dispatcher.add_handler(CommandHandler('subscribe', subscribe))
dispatcher.add_handler(CommandHandler('sub', subscribe))
dispatcher.add_handler(CommandHandler('unsubscribe', unsubscribe))
dispatcher.add_handler(CommandHandler('unsub', unsubscribe))

# Admin commands
dispatcher.add_handler(CommandHandler('logs', send_logs))

if args.test:
    import json
    get_nodes = get_nodes_from_file
    dispatcher.add_handler(CommandHandler('test', test))

if args.dump:
    print('Bot data:')
    print(dispatcher.bot_data)
    print()

updater.job_queue.run_once(initialize, when=0)
updater.job_queue.run_once(migrate_data, when=0) #Can remove after use
updater.job_queue.run_repeating(check_job, interval=args.poll, first=0)
updater.job_queue.run_repeating(log_job, interval=3600, first=0)

updater.start_polling()
updater.idle()