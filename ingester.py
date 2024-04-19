"""
This program is a standalone complement to the node status bot that gathers data from TF Chain to be used by the bot in determining if nodes have incurred minting violations due to use of the farmerbot. The result is an SQLite database that contains all uptime events, power state changes, and power target changes for all nodes during the scanned period. By default, all blocks for the current minting period are fetched and processed, along with all new blocks as they are created.

So far not much of an attempt is made to catch all errors or ensure that the program continues running. Best to launch it from a process manager and ensure it's restarted on exit. All data is written in a transactional way, such that the results of processing any block, along with the fact that the block has been processed will all be written or all not be written on a given attempt.

Some apparently unavoidable errors arise from use of the Python Substrate Interface module. It seems to not handing concurrency well and sometimes gives a "decoding failed error". That in itself is not a big problem, as it tends to only affect one thread and the thread will just exit and respawn. However I've also observed a rare and odd failure state where these errors become chronic along with database locked errors from SQLite. I had already eliminated a database locking issue and I don't see now how it can happen. Weird bug.

TODO: Somehow address bug as just described. One way would be to look for a lot of thread failures and just bail at that point. Since the issue seems to occur only during specific starts of the program and does not tend to reoccur. For now am trying to just reintroduce the timeout of 15 seconds.
"""

import sqlite3, datetime, threading, queue, time, logging, functools, argparse
from threading import Thread
from websocket._exceptions import WebSocketConnectionClosedException
import grid3.network
from grid3 import tfchain, minting

MAX_WORKERS = 25
MIN_WORKERS = 2
SLEEP_TIME = 30
POST_PERIOD = 60 * 60

parser = argparse.ArgumentParser()
parser.add_argument('-f', '--file', help='Specify the database file name.', 
                    type=str, default='tfchain_data.db')
parser.add_argument('-s', '--start', 
                    help='Give a timestamp to start scanning blocks. If ommitted, scanning starts from beginning of current minting period', type=int)
parser.add_argument('-e', '--end', 
                    help='By default, scanning continues to process new blocks as they are generated. When an end timestamp is given, scanning stops at that block height and the program exits', type=str)

args = parser.parse_args()

class SetQueue(queue.Queue):
    def _init(self, maxsize):
        self.queue = set()
    def _put(self, item):
        self.queue.add(item)
    def _get(self):
        return self.queue.pop()

def add_missing_blocks(start_number, current_number, block_queue):
    total_blocks = {i for i in range(start_number, current_number)}
    processed_blocks = get_processed_blocks(con)
    missing_blocks = total_blocks - processed_blocks
    for i in missing_blocks:
        block_queue.put(i)

def fetch_powers(timestamp, block_hash):
    # To emulating minting properly, we need to know the power state and target of each node at the beginning of the minting period
    # Get our own clients so this can run in a thread
    con = sqlite3.connect(args.file, timeout=15)
    client = tfchain.TFChain()   

    max_node = client.get_node_id(block_hash)
    nodes = set(range(1, max_node + 1))
    existing_powers = con.execute("SELECT node FROM powers WHERE timestamp=?", (timestamp,)).fetchall()
    nodes -= {p[0] for p in existing_powers}

    print('Fetching node powers for', len(nodes), 'nodes')
    for node in nodes:
        if node % 500 == 0:
            print('Processed', node, 'initial power states/targets')
        power = client.get_node_power(node, block_hash)
        # I seem to remember there being some None values in here at some point, but it seems now that all nodes get a default of Up, Up
        if power['state'] == 'Up':
            state = 'Up'
            block = None
        else:
            state = 'Down'
            block = power['state']['Down']
        con.execute("INSERT INTO powers VALUES(?, ?, ?, ?, ?)", (node, state, block, power['target'], timestamp))
        con.commit()

def get_processed_blocks(con):
    results = con.execute("SELECT * FROM processed_blocks").fetchall()
    return {r[0] for r in results}

def lookup_node(address, client, con, block_hash):
    result = con.execute('SELECT * FROM nodes WHERE address=?', [address]).fetchone()
    if result:
        return result[0]
    else:
        twin = client.get_twin_by_account(address, block_hash)
        node = client.get_node_by_twin(twin, block_hash)
        # Any account can call the change power state extrinsic, and some users mistakenly do this from their regular accounts apparently. In case there's no node associated with a twin, the chain returns 0
        if node != 0:
            try:
                con.execute("INSERT INTO nodes VALUES(?, ?, ?)", (node, twin, address))
                con.commit()
            except sqlite3.IntegrityError:
                print('Tried to insert duplicate node:', node, twin, address, block_hash)
                print('Existing node:', con.execute('SELECT * FROM nodes WHERE node_id=?', [node]).fetchone())
        else:
            print('No node found on node lookup:', node, twin, address, block_hash)
        return node

def process_block(client, block_number, con):
    block = client.sub.get_block(client.sub.get_block_hash(block_number))
    timestamp = client.get_timestamp(block) / 1000

    # Idea here is that we write data from the block and also note the fact that this block is processed in a single transaction that reverts if any error occurs (great idea?)
    updates = []
    for extrinsic in block['extrinsics']:
        data = extrinsic.value
        if data['call']['call_function'] == 'report_uptime_v2':
            uptime = data['call']['call_args'][0]['value']
            node = lookup_node(data['address'], client, con, block['header']['hash'])
            if node != 0:
                updates.append(("INSERT INTO uptimes VALUES(?, ?, ?)", (node, uptime, timestamp)))
        elif data['call']['call_function'] == 'change_power_target':
            node = data['call']['call_args'][0]['value']
            target = data['call']['call_args'][1]['value']
            updates.append(("INSERT INTO power_target_changes VALUES(?, ?, ?)", (node, target, timestamp)))
        elif data['call']['call_function'] == 'change_power_state':
            state = data['call']['call_args'][0]['value']
            node = lookup_node(data['address'], client, con, block['header']['hash'])
            if node != 0:
                updates.append(("INSERT INTO power_state_changes VALUES(?, ?, ?)", (node, state, timestamp)))

    try:
        with con:
            for update in updates:
                con.execute(*update)
            con.execute("INSERT INTO processed_blocks VALUES(?)", (block_number,))
    except sqlite3.IntegrityError as e:
        # Although we make various attempts to ensure that blocks are only processed once, these are not guarantees and occasionally we will attempt to violate a UNIQUE constraint. In this case, the transaction is reverted by the context manager above and we ignore the error here
        if "UNIQUE" in e.args[0]:
            pass
        else:
            raise

def processor(block_queue):
    # Each processor thread has its own TF Chain and db connections
    con = sqlite3.connect(args.file, timeout=15)
    client = tfchain.TFChain()
    while 1:
        block_number = block_queue.get()
        if block_number < 0:
            return

        exists = con.execute("SELECT 1 FROM processed_blocks WHERE block_number=?", [block_number]).fetchone()

        if exists is None:
            process_block(client, block_number, con)
            block_queue.task_done()

def parallelize(con, start_number, end_number, block_queue):
    processed_blocks = get_processed_blocks(con)
    remaining_blocks = set(range(start_number, end_number + 1)) - processed_blocks

    for b in remaining_blocks:
        block_queue.put(b)

    print('Starting', MAX_WORKERS, 'workers to process', block_queue.qsize(), 'blocks')

    threads = [spawn_worker(block_queue) for i in range(MAX_WORKERS)]
    return threads

def prep_db(con):
    con.execute("CREATE TABLE IF NOT EXISTS uptimes(node, uptime, timestamp, UNIQUE(node, uptime, timestamp)) ")
    con.execute("CREATE TABLE IF NOT EXISTS power_target_changes(node, target, timestamp, UNIQUE(node, target, timestamp))")
    con.execute("CREATE TABLE IF NOT EXISTS power_state_changes(node, state, timestamp, UNIQUE(node, state, timestamp))")
    con.execute("CREATE TABLE IF NOT EXISTS powers(node, state, block, target, timestamp, UNIQUE(node, timestamp))")
    con.execute("CREATE TABLE IF NOT EXISTS processed_blocks(block_number PRIMARY KEY)")
    con.execute("CREATE TABLE IF NOT EXISTS nodes(node_id PRIMARY KEY, twin_id, address)")
    con.commit()

def populate_nodes(con):
    # Cache all known nodes from GraphQL if the nodes table is empty
    if con.execute('SELECT COUNT(*) FROM nodes').fetchone()[0] == 0:
        graphql = grid3.network.GridNetwork().graphql
        nodes = graphql.nodes(['nodeID', 'twinID'])
        twins = graphql.twins(['twinID', 'accountID'])
        twin_to_account = {t['twinID']: t['accountID'] for t in twins}

        for node in nodes:
            twin = node['twinID']
            con.execute("INSERT INTO nodes VALUES(?, ?, ?)", (node['nodeID'], twin, twin_to_account[twin]))

        con.commit()

def scale_workers(threads, block_queue):
    if block_queue.qsize() < 2 and len(threads) > MIN_WORKERS:
        print('Queue cleared, scaling down workers')
        for i in range(len(threads) - MIN_WORKERS):
            block_queue.put(-1 - i)

    if block_queue.qsize() < MAX_WORKERS and len(threads) < MIN_WORKERS:
        print('Queue is small, but fewer than', MIN_WORKERS, 'workers are alive. Spawning more workers')
        for i in range(MIN_WORKERS - len(threads)):
            threads.append(spawn_thread(block_queue))

    if block_queue.qsize() > MAX_WORKERS and len(threads) < MAX_WORKERS:
        print('More than', MAX_WORKERS, 'jobs remaining but fewer threads. Spawning more workers')
        for i in range(MAX_WORKERS - len(threads)):
            threads.append(spawn_worker(block_queue))

def spawn_subsriber(block_queue, client):
    callback = functools.partial(subscription_callback, block_queue)
    sub_thread = Thread(target=client.sub.subscribe_block_headers, args=[callback])
    sub_thread.start()
    return sub_thread

def spawn_worker(block_queue):
    thread = Thread(target=processor, args=[block_queue])
    thread.start()
    return thread

def subscription_callback(block_queue, head, update_nr, subscription_id):
    block_queue.put(head['header']['number'])

# Prep database, populate nodes, and grab already processed blocks
con = sqlite3.connect(args.file, timeout=15)
prep_db(con)
populate_nodes(con)

results = con.execute("SELECT * FROM processed_blocks").fetchall()
processed_blocks = {r[0] for r in results}

# If no start time given, use beginning of current minting period
if args.start is None:
    start = minting.Period().start
else:
    start = args.start
    
client = tfchain.TFChain()
start_number = client.find_block(start)
block = client.sub.get_block(block_number=start_number)
Thread(target=fetch_powers, args=[start, block['header']['hash']]).start()
block_queue = SetQueue()

if args.end:
    end = int(args.end) + POST_PERIOD
    end_number = client.find_block(end)
    threads = parallelize(con, start_number, end_number, block_queue)

    while block_queue.qsize() > 10:
        time.sleep(SLEEP_TIME)
        threads = [t for t in threads if t.is_alive()]
        print(datetime.datetime.now(), block_queue.qsize(), 'blocks remaining', len(threads), 'threads alive')
        scale_workers(threads, block_queue)

    # Wait for remaining jobs to finish. Since our threads aren't daemons, the program will exit after this
    block_queue.join()
else:
    # Since using the subscribe method blocks, we give it a thread
    sub_thread = spawn_subsriber(block_queue, client)

    # We wait to get the first block number back from the subscribe callback, so that we're sure which block is the end of the historic range we want
    block = block_queue.get()
    block_queue.put(block)
    threads = parallelize(con, start_number, block - 1, block_queue)

    while 1:
        time.sleep(SLEEP_TIME)

        # We can periodically get disconnected from the websocket, especially if running on a machine that goes into standby. On each loop we try once to reconnect if needed before trying to use the client below
        if not client.sub.websocket.connected:
            client.sub.connect_websocket()

        try:
            current_number = client.sub.get_block_header()['header']['number']
            add_missing_blocks(start_number, current_number, block_queue)
        except WebSocketConnectionClosedException:
            print("Web socket closed in main loop")

        # We just discard any threads that have died for any reason. They will be replaced by the auto scaling. In fact, we don't try to handle errors at all in the worker threads--the blocks just get retried later
        threads = [t for t in threads if t.is_alive()]
        print(datetime.datetime.now(), block_queue.qsize(), 'blocks remaining', len(threads), 'threads alive')

        scale_workers(threads, block_queue)

        # Also make sure we keep alive our subscription thread. If there's an error in the callback, it propagates up and the thread dies
        if not sub_thread.is_alive():
            print("Subscription thread died, respawning it")
            sub_thread = spawn_subsriber(block_queue, client)