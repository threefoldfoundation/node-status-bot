"""
This program is a standalone complement to the node status bot that gathers data from TF Chain to be used by the bot in determining if nodes have incurred minting violations due to use of the farmerbot. The result is a SQLite database that contains all uptime events, power state changes, and power target changes for all nodes during the scanned period. By default, all blocks for the current minting period are fetched and processed, along with all new blocks as they are created.

So far not much of an attempt is made to catch all errors or ensure that the program continues running. Best to launch it from a process manager and ensure it's restarted on exit. All data is written in a transactional way, such that the results of processing any block, along with the fact that the block has been processed will all be written or all not be written on a given attempt.

Some apparently unavoidable errors arise from use of the Python Substrate Interface module. It seems to not handing concurrency well and sometimes gives a "decoding failed error". That in itself is not a big problem, as it tends to only affect one thread and the thread will just exit and respawn. However I've also observed a rare and odd failure state where these errors become chronic along with database locked errors from SQLite. I had already eliminated a database locking issue and I don't see now how it can happen. Weird bug.

TODO: Somehow address bug as just described. One way would be to look for a lot of thread failures and just bail at that point. Since the issue seems to occur only during specific starts of the program and does not tend to reoccur.

Still getting db locked errors sometimes. There's now a dedicated writer process, but the fetch_powers also writes to the db on its own when it's running. Even when just the writer process is running, it's still possible to timeout with locked db, for reasons I don't understand. For now I increased the db timeout to 30 seconds and that helps.
"""

import sqlite3, datetime, time, logging, functools, argparse
from threading import Thread
from multiprocessing import Process, JoinableQueue
from websocket._exceptions import WebSocketConnectionClosedException
import grid3.network
from grid3 import tfchain, minting

MAX_WORKERS = 50
MIN_WORKERS = 2
SLEEP_TIME = 30
DB_TIMEOUT = 30
WRITE_BATCH = 100
POST_PERIOD = 60 * 60

# When querying a fixed period of blocks, how many times to retry missed blocks
RETRIES = 3

parser = argparse.ArgumentParser()
parser.add_argument('-f', '--file', help='Specify the database file name.', 
                    type=str, default='tfchain_data.db')
parser.add_argument('-s', '--start', 
                    help='Give a timestamp to start scanning blocks. If ommitted, scanning starts from beginning of current minting period', type=int)
parser.add_argument('--start-block', 
                    help='Give a block number to start scanning blocks', type=int)
parser.add_argument('-e', '--end', 
                    help='By default, scanning continues to process new blocks as they are generated. When an end timestamp is given, scanning stops at that block height and the program exits', type=int)
parser.add_argument('--end-block', 
                    help='Specify end by block number rather than timestamp', type=int)

args = parser.parse_args()


def load_queue(start_number, end_number, block_queue):
    total_blocks = set(range(start_number, end_number + 1))
    processed_blocks = get_processed_blocks(con)
    missing_blocks = total_blocks - processed_blocks
    for i in missing_blocks:
        block_queue.put(i)
    return len(missing_blocks)

def db_writer(write_queue):
    con = sqlite3.connect(args.file, timeout=DB_TIMEOUT)
    processed_blocks = get_processed_blocks(con)

    while 1:
        job = write_queue.get()
        if job is None:
            return

        try: 
            with con:
                block_number = job[0]
                if block_number not in processed_blocks:
                    updates = job[1]
                    for update in updates:
                        con.execute(*update)
                    con.execute("INSERT INTO processed_blocks VALUES(?)", (block_number,))
                    processed_blocks.add(block_number)
        except Exception as e:
            print("Got an exception in write loop:", e)
            print("While processing job:", job)
        finally:
            write_queue.task_done()

def fetch_powers(block_number, writer_queue):
    # To emulating minting properly, we need to know the power state and target of each node at the beginning of the minting period
    # Get our own clients so this can run in a thread
    con = sqlite3.connect(args.file, timeout=DB_TIMEOUT)
    client = tfchain.TFChain()

    block = client.sub.get_block(block_number=block_number)
    block_hash = block['header']['hash']
    timestamp = client.get_timestamp(block) // 1000

    max_node = client.get_node_id(block_hash)
    nodes = set(range(1, max_node + 1))
    existing_powers = con.execute("SELECT node_id FROM PowerState WHERE block=?", (block_number,)).fetchall()
    nodes -= {p[0] for p in existing_powers}

    print('Fetching node powers for', len(nodes), 'nodes')
    for node in nodes:
        if node % 500 == 0:
            print('Processed', node, 'initial power states/targets')
        power = client.get_node_power(node, block_hash)
        # I seem to remember there being some None values in here at some point, but it seems now that all nodes get a default of Up, Up
        if power['state'] == 'Up':
            state = 'Up'
            down_block = None
        else:
            state = 'Down'
            down_block = power['state']['Down']
        con.execute("INSERT INTO PowerState VALUES(?, ?, ?, ?, ?, ?)", (node, state, down_block, power['target'], block_number, timestamp))
        con.commit()

def get_block(client, block_number):
    block = client.sub.get_block(block_number=block_number)
    events = client.sub.get_events(block['header']['hash'])
    return block, events

def get_processed_blocks(con):
    results = con.execute("SELECT * FROM processed_blocks").fetchall()
    return {r[0] for r in results}

def process_block(block, events):
    block_number = block['header']['number']
    timestamp = block['extrinsics'][0].value['call']['call_args'][0]['value'] // 1000

    updates = []
    for i, event in enumerate(events):
        event = event.value
        event_id  = event['event_id']
        attributes = event['attributes']
        # TODO: pass these more efficiently than writing the INSERT string for each one
        if event_id == 'NodeUptimeReported':
            updates.append(("INSERT INTO NodeUptimeReported VALUES(?, ?, ?, ?, ?, ?)", (attributes[0], attributes[2], attributes[1], block_number, i, timestamp)))
        elif event_id == 'PowerTargetChanged':
            updates.append(("INSERT INTO PowerTargetChanged VALUES(?, ?, ?, ?, ?, ?)", (attributes['farm_id'], attributes['node_id'], attributes['power_target'], block_number, i, timestamp)))
        elif event_id == 'PowerStateChanged':
            if attributes['power_state'] == 'Up':
                state = 'Up'
                down_block = None
            else:
                state = 'Down'
                down_block = attributes['power_state']['Down']
            updates.append(("INSERT INTO PowerStateChanged VALUES(?, ?, ?, ?, ?, ?, ?)", (attributes['farm_id'], attributes['node_id'], state, down_block, block_number, i, timestamp)))

    return updates

def processor(block_queue, write_queue):
    # Each processor has its own TF Chain and db connections
    con = sqlite3.connect(args.file, timeout=DB_TIMEOUT)
    client = tfchain.TFChain()
    while 1:
        block_number = block_queue.get()
        if block_number < 0:
            block_queue.task_done()
            return

        exists = con.execute("SELECT 1 FROM processed_blocks WHERE block_number=?", [block_number]).fetchone()

        try:
            if exists is None:
                block, events = get_block(client, block_number)
                updates = process_block(block, events)
                write_queue.put((block_number, updates))

        finally:
            # This allows us to join() the queue later to determine when all queued blocks have been attempted, even if processing failed
            block_queue.task_done()

def parallelize(con, start_number, end_number, block_queue, write_queue):
    load_queue(start_number, end_number, block_queue)

    print('Starting', MAX_WORKERS, 'workers to process', block_queue.qsize(), 'blocks, with starting block number', start_number, 'and ending block number', end_number)

    processes = [spawn_worker(block_queue, write_queue) for i in range(MAX_WORKERS)]
    return processes

def prep_db(con):
    # While block number and timestamp of the block are 1-1, converting between them later is not trivial, so it can be helpful to have both. We also store the event index, because the ordering of events within a block can be important from the perspective of minting (in rare cases). For uptime_hint, this is as far as I know always equal to the block time stamp // 1000
    # Each event should be uniquely identified by its block and event numbers
    con.execute("CREATE TABLE IF NOT EXISTS NodeUptimeReported(node_id, uptime, timestamp_hint, block, event_index, timestamp, UNIQUE(event_index, block))")

    con.execute("CREATE TABLE IF NOT EXISTS PowerTargetChanged(farm_id, node_id, target, block, event_index, timestamp, UNIQUE(event_index, block))")

    con.execute("CREATE TABLE IF NOT EXISTS PowerStateChanged(farm_id, node_id, state, down_block, block, event_index, timestamp, UNIQUE(event_index, block))")

    con.execute("CREATE TABLE IF NOT EXISTS PowerState(node_id, state, down_block, target, block, timestamp, UNIQUE(node_id, block))")

    con.execute("CREATE TABLE IF NOT EXISTS processed_blocks(block_number PRIMARY KEY)")

    con.commit()

def scale_workers(processes, block_queue, write_queue):
    if block_queue.qsize() < 2 and len(processes) > MIN_WORKERS:
        print('Queue cleared, scaling down workers')
        for i in range(len(processes) - MIN_WORKERS):
            block_queue.put(-1 - i)

    if block_queue.qsize() < MAX_WORKERS and len(processes) < MIN_WORKERS:
        print('Queue is small, but fewer than', MIN_WORKERS, 'workers are alive. Spawning more workers')
        for i in range(MIN_WORKERS - len(processes)):
            processes.append(spawn_worker(block_queue))

    if block_queue.qsize() > MAX_WORKERS and len(processes) < MAX_WORKERS:
        print('More than', MAX_WORKERS, 'jobs remaining but fewer processes. Spawning more workers')
        for i in range(MAX_WORKERS - len(processes)):
            processes.append(spawn_worker(block_queue, write_queue))

def spawn_subsriber(block_queue, client):
    callback = functools.partial(subscription_callback, block_queue)
    sub_thread = Thread(target=client.sub.subscribe_block_headers, args=[callback])
    sub_thread.start()
    return sub_thread

def spawn_worker(block_queue, write_queue):
    process = Process(target=processor, args=[block_queue, write_queue])
    process.start()
    return process

def subscription_callback(block_queue, head, update_nr, subscription_id):
    block_queue.put(head['header']['number'])

print('Staring up, preparing to ingest some blocks, nom nom')

# Prep database and grab already processed blocks
con = sqlite3.connect(args.file, timeout=DB_TIMEOUT)
prep_db(con)

results = con.execute("SELECT * FROM processed_blocks").fetchall()
processed_blocks = {r[0] for r in results}

# Start tfchain client
client = tfchain.TFChain()

if args.start_block:
    start_number = args.start_block
elif args.start:  
    start_number = client.find_block_minting(args.start)
else:
    # By default, use beginning of current minting period
    start_number = client.find_block_minting(minting.Period().start)

block_queue = JoinableQueue()
write_queue = JoinableQueue()

writer_proc = Process(target=db_writer, args=[write_queue])
writer_proc.start()

powers_thread = Thread(target=fetch_powers, args=[start_number, write_queue])
powers_thread.start()

if args.end or args.end_block:
    if args.end_block:
        end_number = args.end_block
    else:
        end_number = client.find_block_minting(args.end + POST_PERIOD)

    processes = parallelize(con, start_number, end_number, block_queue, write_queue)

    while (block_qsize := block_queue.qsize()) > 0:
        time.sleep(SLEEP_TIME)
        processes = [t for t in processes if t.is_alive()]
        print(datetime.datetime.now(), 'processed', block_qsize - block_queue.qsize(), 'blocks in', SLEEP_TIME, 'seconds', block_queue.qsize(), 'blocks remaining', len(processes), 'processes alive', write_queue.qsize(), 'write jobs')
        scale_workers(processes, block_queue, write_queue)

    print('Joining blocks queue')
    block_queue.join()
    print('Joining write queue')
    write_queue.join()
    # Retry any missed blocks three times. Since we don't handle errors in the when fetching and processing blocks, it's normal to miss a few
    while missing_count := load_queue(start_number, end_number, block_queue):
        print(datetime.datetime.now(), missing_count, 'blocks to retry', len(processes), 'processes alive')
        block_queue.join()
        write_queue.join()

    # Finally wait for any remaining jobs to complete
    block_queue.join()
    write_queue.join()
    # Signal remaining processes to exit
    [block_queue.put(-1) for t in processes if t.is_alive()]
    write_queue.put(None)

else:
    # Since using the subscribe method blocks, we give it a thread
    sub_thread = spawn_subsriber(block_queue, client)

    # We wait to get the first block number back from the subscribe callback, so that we're sure which block is the end of the historic range we want
    block = block_queue.get()
    block_queue.put(block)
    processes = parallelize(con, start_number, block - 1, block_queue, write_queue)

    while 1:
        time.sleep(SLEEP_TIME)

        # We can periodically get disconnected from the websocket, especially if running on a machine that goes into standby. On each loop we try once to reconnect if needed before trying to use the client below
        if not client.sub.websocket.connected:
            client.sub.connect_websocket()

        try:
            current_number = client.sub.get_block_header()['header']['number']
            load_queue(start_number, current_number, block_queue)
        except WebSocketConnectionClosedException:
            print("Web socket closed in main loop")

        # We just discard any processes that have died for any reason. They will be replaced by the auto scaling. In fact, we don't try to handle errors at all in the worker processes--the blocks just get retried later
        processes = [t for t in processes if t.is_alive()]
        print(datetime.datetime.now(), block_queue.qsize(), 'blocks remaining', len(processes), 'processes alive', write_queue.qsize(), 'write jobs')

        scale_workers(processes, block_queue, write_queue)

        # Also make sure we keep alive our subscription thread. If there's an error in the callback, it propagates up and the thread dies
        if not sub_thread.is_alive():
            print("Subscription thread died, respawning it")
            sub_thread = spawn_subsriber(block_queue, client)