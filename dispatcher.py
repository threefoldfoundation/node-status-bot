import logging, requests, subprocess, argparse

from telegram import Update
from telegram.ext import Updater, CallbackContext, CommandHandler, PicklePersistence

from gql import Client, gql
from gql.transport.requests import RequestsHTTPTransport
from gql.transport.exceptions import TransportServerError

parser = argparse.ArgumentParser()
parser.add_argument('token', help='Specify a bot token')
parser.add_argument('-v', '--verbose', help='Verbose output', action="store_true")
parser.add_argument('-p', '--poll', help='Set polling frequency in seconds', type=int, default=300)
args = parser.parse_args()

pickler = PicklePersistence(filename='bot_data')
updater = Updater(token=args.token, persistence=pickler, use_context=True)

dispatcher = updater.dispatcher

if args.verbose:
    log_level = logging.INFO
else:
    log_level = logging.WARNING

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=log_level)

def check(net, node):
    r = requests.get(get_proxy(net) + str(node) + '/status', timeout=5)
    return r.json()

def check_valid(net, node):
    requests.get(get_proxy(net) + node).ok

def check_job(context: CallbackContext):
    for net in ['dev', 'test', 'main']:
        node_ids = []
        for chat_id, data in context.bot_data['chats'].items():
            node_ids += data['nodes'][net]
        nodes = []
        for n in node_ids:
            nodes.append((n, context.bot_data['nodes'][net][n]['ip']))
        pings = ping_many(nodes)

        for chat_id, data in context.bot_data['chats'].items():
            for node in data['nodes'][net]:
                for ping in pings:
                    if ping[0] == node:
                        # If node has only been checked once, skip notifications
                        try:
                            first = context.bot_data['nodes'][net][node]['previous_status']
                        except KeyError:
                            context.bot_data['nodes'][net][node]['previous_status'] = context.bot_data['nodes'][net][node]['status']
                            context.bot_data['nodes'][net][node]['status'] = ping[1]
                            continue

                        second = context.bot_data['nodes'][net][node]['status']
                        latest = ping[1]

                        context.bot_data['nodes'][net][node]['status'] = latest
                        context.bot_data['nodes'][net][node]['previous_status'] = second
                        
                        if first == 'up' and second == 'down' and latest == 'down':
                            context.bot.send_message(chat_id=chat_id, text='Node {} has gone offline :('.format(node))
                        elif first == 'down' and second == 'down' and latest == 'up':
                            context.bot.send_message(chat_id=chat_id, text='Node {} has come back online :)'.format(node))

                        # For debug, send a message on every check
                        #context.bot.send_message(chat_id=chat_id, text='Node {} is {}'.format(node, ping[1]))

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

def format_url(net, base):
    if net == 'main':
        net = ''
    else:
        net = '.' + net

    return base.format(net)

def get_node_ips(net, nodes):
    if net == 'main':
        gql_url = 'https://graph.grid.tf/graphql'
    else:
        gql_url = format_url(net, 'https://graphql{}.grid.tf/graphql')


    transport = RequestsHTTPTransport(url=gql_url, verify=True, retries=3)
    client = Client(transport=transport, fetch_schema_from_transport=True)

    query = """
    query getNode {{
    nodes(where: {{nodeId_in: {}}}) {{
        nodeId
        twinId
    }}
    }}
    """
    nodes = [int(node) for node in nodes if node.isnumeric()]
    if not nodes:
        return []

    valid_nodes = client.execute(gql(query.format(nodes)))['nodes']
    ips = []
    if valid_nodes:
        for node in valid_nodes:
            twin = node['twinId']

            # TODO, also query twins in parallel
            query = """
            query getTwin {{
            twins(where: {{twinId_eq: {}}}) {{
                ip
            }}
            }}
            """

            ip = client.execute(gql(query.format(twin)))["twins"][0]['ip']
            ips.append((int(node['nodeId']), ip))

        return ips
    else:
        return []

def get_proxy(net):
    base = 'https://gridproxy{}.grid.tf/nodes/'
    if net == 'main':
        net = ''
    else:
        net = '.' + net
    return base.format(net)

def initialize(context: CallbackContext):
    for key in ['chats', 'nodes']:
        try:
            context.bot_data[key]
        except KeyError:
            context.bot_data[key] = {}

    for net in ['dev', 'test', 'main']:
        try:
            context.bot_data['nodes'][net]
        except KeyError:
            context.bot_data['nodes'][net] = {}

    subs = 0
    for chat, data in context.bot_data['chats'].items():
        for net in ['dev', 'test', 'main']:
            if data['nodes'][net]:
                subs += 1
                break
    print('{} chats and {} subscribed users'.format(len(context.bot_data['chats']), subs))

def initialize_chat(chat_id, context):
    context.bot_data['chats'][chat_id] = {}
    context.bot_data['chats'][chat_id]['net'] = 'main'
    context.bot_data['chats'][chat_id]['nodes'] = {}
    for net in ['dev', 'test', 'main']:
        context.bot_data['chats'][chat_id]['nodes'][net] = []

def network(update: Update, context: CallbackContext):
    chat_id = update.effective_chat.id
    try:
        context.bot_data['chats'][chat_id]
    except KeyError:
        initialize_chat(chat_id, context)

    if context.args:
        net = context.args[0]
        if net in ['dev', 'test', 'main']:
            context.bot_data['chats'][chat_id]['net'] = net
            context.bot.send_message(chat_id=chat_id, text='Set network to {}net'.format(net))
        else:
            context.bot.send_message(chat_id=chat_id, text='Please specify a valid network: dev, test, or main')
    else:
        net = context.bot_data['chats'][chat_id]['net']
        context.bot.send_message(chat_id=chat_id, text='Network is set to {}net'.format(net))

def ping(host):
    out = subprocess.run(['fping', '-t 1000', host], stdout=subprocess.PIPE).stdout.decode('utf-8')
    return {True: 'up', False: 'down'}['alive' in out]

def ping_many(nodes):
    if nodes:
        ips = [node[1] for node in nodes]
        out = subprocess.run(['fping', '-t 1000'] + ips, stdout=subprocess.PIPE).stdout.decode('utf-8')
        result = []
        for line in out.split('\n')[:-1]:
            for node in nodes:
                if node[1] == line.split()[0]:
                    stat = {True: 'up', False: 'down'}['alive' in line]
                    result.append((int(node[0]), stat))
        return result
    else:
        return []

def start(update: Update, context: CallbackContext):
    chat_id = update.effective_chat.id
    initialize_chat(chat_id, context)
    msg = '''
Hey there, I'm the ThreeFold Grid 3 node status bot. Beep boop.

I can give you information about whether a node is up or down right now and also notify you if its state changes in the future. Here are the commands I support:

/network (/net) - change the network to "dev", "test", or "main" (default is main). If you don't provide an input, the currently selected network is shown. 
Example: /network main

/status - check the current status of one node. 
Example: /status 1

/subscribe (/sub) - subscribe to updates about one or more nodes. If you don't provide an input, the nodes you are currently subscribed to will be shown. 
Example: /sub 1 2 3

/unsubscribe (/unsub) - unsubscribe from updates about one or more nodes. If you don't give an input, you'll be unsubscribed from all updates.

To report bugs, request features, or just say hi, please contact @scottyeager. Please also subscribe to the updates channel here for news on the bot: t.me/node_bot_updates

This bot is experimental and probably has bugs. Only you are responsible for your node's uptime and your farming rewards.
    '''
    context.bot.send_message(chat_id=chat_id, text=msg)

def status_proxy(update: Update, context: CallbackContext):
    chat_id = update.effective_chat.id
    if context.args:
        net = context.bot_data['chats'][chat_id]['net']
        node = context.args[0]
        online = check(net, node)['status']
        context.bot.send_message(chat_id=chat_id, text='Node {} is {}'.format(node, online))
    else:
        pass

def status_ping(update: Update, context: CallbackContext):
    chat_id = update.effective_chat.id
    try:
        context.bot_data['chats'][chat_id]
    except KeyError:
        initialize_chat(chat_id, context)

    if context.args:
        net = context.bot_data['chats'][chat_id]['net']
        node = context.args[0]

        try:
            ip = (int(node), context.bot_data['nodes'][net][int(node)]['ip'])
        except KeyError:
            context.bot.send_message(chat_id=chat_id, text='Fetching node details...')
            try:
                ip = get_node_ips(net, [node])[0]
            except (TransportServerError, requests.exceptions.ConnectionError) as e:
                context.bot.send_message(chat_id=chat_id, text='Error fetching node details. If this issue persists, please notify @scottyeager')
                raise e
            if ip:
                context.bot_data['nodes'][net][ip[0]] = {'ip': ip[1]}
            else:
                context.bot.send_message(chat_id=chat_id, text='Node with that id not found on this network')

        stat = ping(ip[1])
        context.bot_data['nodes'][net][ip[0]]['status'] = stat
        context.bot.send_message(chat_id=chat_id, text='Node {} is {}'.format(node, stat))

    else:
        context.bot.send_message(chat_id=chat_id, text='Please specify a node id')

def subscribe(update: Update, context: CallbackContext):
    chat_id = update.effective_chat.id
    try:
        context.bot_data['chats'][chat_id]
    except KeyError:
        initialize_chat(chat_id, context)

    net = context.bot_data['chats'][chat_id]['net']
    subbed_nodes = context.bot_data['chats'][chat_id]['nodes'][net]
    
    if context.args:
        valid_nodes = []
        unknown_nodes = []
        for node in context.args:
            # Check first if they're already subscribed
            if not int(node) in subbed_nodes:
                try:
                    ip = context.bot_data['nodes'][net][int(node)]['ip']
                    valid_nodes.append((node, ip))
                except KeyError:
                    unknown_nodes.append(node)

        if unknown_nodes:
            context.bot.send_message(chat_id=chat_id, text='Fetching node details...')
            try:
                ips = get_node_ips(net, unknown_nodes)
            except (TransportServerError, requests.exceptions.ConnectionError) as e:
                context.bot.send_message(chat_id=chat_id, text='Error fetching node details. If this issue persists, please notify @scottyeager')
                raise(e)
            for ip in ips:
                context.bot_data['nodes'][net][ip[0]] = {'ip': ip[1]}
            valid_nodes += ips
        
        if valid_nodes:
            pings = ping_many(valid_nodes)

            for stat in pings:
                node = stat[0]
                context.bot_data['nodes'][net][node]['status'] = stat[1]
                subbed_nodes.append(node)

            context.bot.send_message(chat_id=chat_id, text='Success! You are now subscribed to node' + format_list(subbed_nodes))

        else:
            context.bot.send_message(chat_id=chat_id, text='Sorry, no valid node ids found.')

    else:
        if subbed_nodes:
            context.bot.send_message(chat_id=chat_id, text='You are currently subscribed to node' + format_list(subbed_nodes))
        else:
            context.bot.send_message(chat_id=chat_id, text='You are not subscribed to any nodes')

def unsubscribe(update: Update, context: CallbackContext):
    chat_id = update.effective_chat.id
    try:
        context.bot_data['chats'][chat_id]
    except KeyError:
        initialize_chat(chat_id, context)

    if len(context.bot_data['chats'][chat_id]['nodes']) == 0:
        context.bot.send_message(chat_id=chat_id, text="You weren't subscribed to any updates.")
    else:
        if context.args:
            removed_nodes = []
            net = context.bot_data['chats'][chat_id]['net']
            subbed_nodes = context.bot_data['chats'][chat_id]['nodes'][net]
            for node in context.args:
                try:
                    subbed_nodes.pop(subbed_nodes.index(int(node)))
                    removed_nodes.append(node)
                except ValueError:
                    pass
            if removed_nodes:
                context.bot.send_message(chat_id=chat_id, text='You have been unsubscribed from node' + format_list(removed_nodes))
            else:
                context.bot.send_message(chat_id=chat_id, text='No valid and subscribed node ids found.')
        else:
            for net in ['dev', 'test', 'main']:
                context.bot_data['chats'][chat_id]['nodes'][net] = []
            context.bot.send_message(chat_id=chat_id, text='You have been unsubscribed from all updates')


dispatcher.add_handler(CommandHandler('start', start))
dispatcher.add_handler(CommandHandler('status', status_ping))
dispatcher.add_handler(CommandHandler('subscribe', subscribe))
dispatcher.add_handler(CommandHandler('sub', subscribe))
dispatcher.add_handler(CommandHandler('unsubscribe', unsubscribe))
dispatcher.add_handler(CommandHandler('unsub', unsubscribe))
dispatcher.add_handler(CommandHandler('network', network))
dispatcher.add_handler(CommandHandler('net', network))


updater.job_queue.run_once(initialize, when=0)
updater.job_queue.run_repeating(check_job, interval=args.poll, first=0)

updater.start_polling()
updater.idle()