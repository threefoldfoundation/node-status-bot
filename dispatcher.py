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

def check(net, node):
    tries = 3
    while tries:
        try:
            r = requests.get(get_proxy(net) + 'nodes/' + str(node) + '/status', timeout=2)
            if r.status_code != 200:
                logging.warning('Got a non 200 status from grid proxy request, status was: {}'.format(r.status_code))
            return r.json()['status']
        except requests.Timeout:
            logging.exception('Timed out trying to access grid proxy')
            raise
        except:
            logging.exception('Error checking grid proxy.')
            
        tries -= 1
        time.sleep(.5)
    raise ConnectionError('Failed to connect to grid proxy after 3 tries')

def check_valid(net, node):
    return requests.get(get_proxy(net) + 'nodes/' + str(node)).ok

def check_job(context: CallbackContext):
    """
    The main attraction, when it's working properly. This function collects all the node ids that have an active subscription, checks their status via both proxy and ping, then sends alerts to users whose nodes have a status change.
    """
    for net in GRID_NETWORKS:
        # Multiple users could sub to the same node, find the unique set of actively subscribed nodes
        node_ids = set()
        for chat_id, data in context.bot_data['chats'].items():
            node_ids |= set(data['nodes'][net])
            
        for n in node_ids:
            try:
                node = context.bot_data['nodes'][net][n]
            except:
                logging.exception('Error retreiving node')
                # TODO: This shouldn't happen, but if it did, we should probably create the node
                continue

            # We already tried 3 times, so move on and do again next loop
            try:
                proxy_status = check(net, n)
            except:
                logging.exception("Error checking grid proxy")
                continue

            try:
                previous_status = node.status
                node.status = proxy_status

                # Yikes! We're 7 indents deep looping over all subscribers again to figure out who to alert. We have to do this because multiple users might be subbed to the same node. TODO: subbed users should be a property of the node. Then we can avoid the part about making set of nodes with active subs above too
                if previous_status == 'up' and node.status == 'down':
                    for chat_id, data in context.bot_data['chats'].items():
                        if n in data['nodes'][net]:
                            context.bot.send_message(chat_id=chat_id, text='Node {} has gone offline'.format(n))

                elif previous_status == 'up' and node.status == 'standby':
                    for chat_id, data in context.bot_data['chats'].items():
                        if n in data['nodes'][net]:
                            context.bot.send_message(chat_id=chat_id, text='Node {} has gone to sleep'.format(n))

                elif previous_status in ('down', 'standby') and node.status == 'up':
                    for chat_id, data in context.bot_data['chats'].items():
                        if n in data['nodes'][net]:
                            context.bot.send_message(chat_id=chat_id, text='Node {} has come back online'.format(n))
            
            except:
                node.status = proxy_status #Set status for next time
                logging.exception("Error in alert block")
                continue


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

def update_node_ip(net, node_id, context):
    ip = get_node_ip_proxy(net, node_id)
    if ip:
        try:
            context.bot_data['nodes'][net][int(node_id)]['ip'] = ip
        except KeyError:
            context.bot_data['nodes'][net][int(node_id)] = {'ip': ip}

def get_power_state(net, node_id):
    if net == 'main':
        endpoint = mainnet_gql
    elif net == 'test':
        endpoint = mainnet_gql
    elif net == 'dev':
        endpoint = devnet_gql

    return endpoint.nodes(['power'], nodeID_eq=node_id)[0]['power']

def get_nodes(net, node_ids):
    """
    Query a list of node ids in GraphQL, create Node objects for consistency and easy field acces, then assign them a status in the same way the Grid Proxy does and return them.
    """
    graphql = graphqls[net]
    nodes = graphql.nodes(['nodeID', 'updatedAt', 'power'], nodeID_in=node_ids)
    nodes = [Node(node) for node in nodes]

    one_hour_ago = time.time() - 60 * 60

    for node in nodes:
        if node.updatedAt > one_hour_ago and node.power['state'] == 'Up':
            node.status = 'up'
        elif node.updatedAt < one_hour_ago and node.power['state'] == 'Down':
            node.status = 'standby'
        else:
            node.status = 'down'

    return nodes

def get_node_ip_proxy(net, node_id):
    tries = 3
    while tries:
        try:
            proxy = get_proxy(net)
            node = requests.get(proxy + 'nodes/' + str(node_id)).json()
            twin = requests.get(proxy + 'twins/?twin_id=' + str(node['twinId'])).json()
            return twin[0]['ip']
        except:
            logging.exception('Error while looking up node ip')

        tries -= 1
        time.sleep(.5)
    raise ConnectionError('Failed to connect to grid proxy after 3 tries')

def get_node_ips(net, nodes):
    if net == 'main':
        gql_url = 'https://graph.grid.tf/graphql'
    else:
        gql_url = format_url(net, 'https://graphql{}.grid.tf/graphql')


    transport = RequestsHTTPTransport(url=gql_url, verify=True, retries=3)
    client = Client(transport=transport, fetch_schema_from_transport=True)

    # There's a better way... use graphql vars
    query = """
    query getNode {{
    nodes(where: {{nodeID_in: {}}}) {{
        nodeID
        twinID
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
            twin = node['twinID']

            # TODO, also query twins in parallel
            query = """
            query getTwin {{
            twins(where: {{twinID_eq: {}}}) {{
                ip
            }}
            }}
            """

            ip = client.execute(gql(query.format(twin)))["twins"][0]['ip']
            ips.append((int(node['nodeID']), ip))

        return ips
    else:
        return []

def get_proxy(net):
    base = 'https://gridproxy{}.grid.tf/'
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

    for net in GRID_NETWORKS:
        try:
            context.bot_data['nodes'][net]
        except KeyError:
            context.bot_data['nodes'][net] = {}

    subs = 0
    for chat, data in context.bot_data['chats'].items():
        for net in GRID_NETWORKS:
            if data['nodes'][net]:
                subs += 1
                break
    print('{} chats and {} subscribed users'.format(len(context.bot_data['chats']), subs))

def initialize_chat(chat_id, context):
    context.bot_data['chats'][chat_id] = {}
    context.bot_data['chats'][chat_id]['net'] = 'main'
    context.bot_data['chats'][chat_id]['nodes'] = {}
    for net in GRID_NETWORKS:
        context.bot_data['chats'][chat_id]['nodes'][net] = []

def migrate_nodes(context: CallbackContext):
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
    try:
        context.bot_data['chats'][chat_id]
    except KeyError:
        initialize_chat(chat_id, context)

    if context.args:
        net = context.args[0]
        if net in GRID_NETWORKS:
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

def ping_many(nodes, timeout=1000):
    if nodes:
        ips = [node[1] for node in nodes]
        out = subprocess.run(['fping', '-t ' + str(timeout)] + ips, stdout=subprocess.PIPE).stdout.decode('utf-8')
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
    try:
        context.bot_data['chats'][chat_id]
    except KeyError:
        initialize_chat(chat_id, context)
        
    net = context.bot_data['chats'][chat_id]['net']

    if context.args:
        node = get_nodes(net, context.args)[0]
        context.bot.send_message(chat_id=chat_id, text='Node {} is {}'.format(node.nodeId, node.status))

    else:
        subbed_nodes = context.bot_data['chats'][chat_id]['nodes'][net]

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

def status_proxy(update: Update, context: CallbackContext):
    chat_id = update.effective_chat.id
    try:
        context.bot_data['chats'][chat_id]
    except KeyError:
        initialize_chat(chat_id, context)
        
    net = context.bot_data['chats'][chat_id]['net']

    if context.args:
        node = context.args[0]
        try:
            online = check(net, node)
            context.bot.send_message(chat_id=chat_id, text='Node {} is {}'.format(node, online))
        # TODO: better error checking and handling
        except (KeyError, ConnectionError):
            context.bot.send_message(chat_id=chat_id, text='Error fetching node status. Please check that the node id is valid for this network.')
    else:
        subbed_nodes = context.bot_data['chats'][chat_id]['nodes'][net]

        if subbed_nodes:
            up, down, standby = [], [], []
            text = ''
            for node in subbed_nodes:
                status = check(net, node)
                if status == 'up':
                    up.append(node)
                elif status == 'down':
                    down.append(node)
                elif status == 'standby':
                    standby.append(node)
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

    try:
        context.bot_data['chats'][chat_id]
    except KeyError:
        initialize_chat(chat_id, context)

    net = context.bot_data['chats'][chat_id]['net']
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
        subbed_nodes = context.bot_data['chats'][chat_id]['nodes'][net]
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
        duplicate_nodes = []
        for node_id in context.args:
            # Check first if they're already subscribed
            if not int(node_id) in subbed_nodes:
                try:
                    context.bot_data['nodes'][net][int(node_id)]
                    valid_nodes.append(node_id)
                except KeyError:
                    unknown_nodes.append(node_id)
            else:
                duplicate_nodes.append(node_id)

        if unknown_nodes:
            for node_id in unknown_nodes:
                try:
                    node = get_nodes(net, [node_id])[0]
                    valid_nodes.append(node_id)
                    context.bot_data['nodes'][net][int(node_id)] = node

                # (requests.Timeout, requests.exceptions.ReadTimeout)
                except:
                    logging.exception("Failed to fetch node info")
                    context.bot.send_message(chat_id=chat_id, text='Something went wrong, please try again or wait a while if the issue persists.')
                    
        #     context.bot.send_message(chat_id=chat_id, text='Fetching node details...')
        #     try:
        #         ips = get_node_ips(net, unknown_nodes)

        #         for ip in ips:
        #             context.bot_data['nodes'][net][ip[0]] = {'ip': ip[1]}
        #     except:
        #         context.bot.send_message(chat_id=chat_id, text='Error fetching node details. If this issue persists, please notify @scottyeager')
        #         raise

        #     valid_nodes += ips
        
        if valid_nodes:
            new_subs = []
            try:
                for node in valid_nodes:
                    node_id = int(node)
                    # context.bot_data['nodes'][net][node_id]['status'] = check(net, node_id)
                    subbed_nodes.append(node_id)
                    new_subs.append(node_id)

                
                # pings = ping_many(valid_nodes)

                # new_subs = []
                # for stat in pings:
                #     node = stat[0]
                #     context.bot_data['nodes'][net][node]['status'] = stat[1]
                #     subbed_nodes.append(node)
                #     new_subs.append(node)

                context.bot.send_message(chat_id=chat_id, text='You have been successfully subscribed to node' + format_list(new_subs))
            except:
                logging.exception("Failed status check during sub")
                context.bot.send_message(chat_id=chat_id, text='Something went wrong, please try again or wait a while if the issue persists.')
                raise

        elif duplicate_nodes:
            context.bot.send_message(chat_id=chat_id, text='You were already subscribed to node' + format_list(duplicate_nodes))

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
            for net in GRID_NETWORKS:
                context.bot_data['chats'][chat_id]['nodes'][net] = []
            context.bot.send_message(chat_id=chat_id, text='You have been unsubscribed from all updates')

def test(update: Update, context: CallbackContext):
    import time

    mainnet_gql = grid_graphql.GraphQL('https://graphql.grid.tf/graphql')
    print("entering loop")
    while 1:
        node = {}
        previous_status = open('test/previous_status', 'r').read().rstrip('\n')
        node.status = open('test/proxy_status', 'r').read().rstrip('\n')
        n = int(open('test/node_id', 'r').read().rstrip('\n'))
        net = open('test/net', 'r').read().rstrip('\n')

        # breakpoint()
        if previous_status == 'up' and node.status == 'down':
            for chat_id, data in context.bot_data['chats'].items():
                if n in data['nodes'][net]:
                    context.bot.send_message(chat_id=chat_id, text='Node {} has gone offline'.format(n))
                print('Node {} has gone offline'.format(n))

        elif previous_status == 'up' and node.status == 'standby':
            for chat_id, data in context.bot_data['chats'].items():
                if n in data['nodes'][net]:
                    context.bot.send_message(chat_id=chat_id, text='Node {} has gone to sleep'.format(n))
                print('Node {} has gone to sleep'.format(n))

        elif previous_status in ('down', 'standby') and node.status == 'up':
            for chat_id, data in context.bot_data['chats'].items():
                if n in data['nodes'][net]:
                    context.bot.send_message(chat_id=chat_id, text='Node {} has come back online'.format(n))
                print('Node {} has come back online'.format(n))
        time.sleep(5)

def check_chat(update: Update, context: CallbackContext):
    chat = update.effective_chat.id
    context.bot.send_message(chat_id=chat, text='Your chat id is {}'.format(chat))

def node_ip(update: Update, context: CallbackContext):
    chat_id = update.effective_chat.id
    if context.args:
        node = context.args[0]
        net = context.bot_data['chats'][chat_id]['net']
        ip = get_node_ip_proxy(net, node)
        context.bot.send_message(chat_id=chat_id, text=ip)
    else:
        context.bot.send_message(chat_id=chat_id, text='Please enter a node id')

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
dispatcher.add_handler(CommandHandler('node_ip', node_ip))
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
    dispatcher.add_handler(CommandHandler('test', test))

if args.dump:
    print('Bot data:')
    print(dispatcher.bot_data)
    print()

updater.job_queue.run_once(initialize, when=0)
updater.job_queue.run_once(migrate_nodes, when=0)
updater.job_queue.run_repeating(check_job, interval=args.poll, first=0)
updater.job_queue.run_repeating(log_job, interval=3600, first=0)

updater.start_polling()
updater.idle()