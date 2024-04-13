import sqlite3, datetime, threading, queue, time, logging, functools, argparse
import grid3.network
from grid3 import tfchain, minting

WORKERS = 25
RETRIES = 2

parser = argparse.ArgumentParser()
parser.add_argument('-f', '--file', help='Specify the database file name.', 
                    type=str, default='tfchain_data.db')
parser.add_argument('-s', '--start', 
                    help='Give a timestamp to start scanning blocks. If ommitted, scanning starts from beginning of current minting period', type=int, default=minting.Period().start)
parser.add_argument('-e', '--end', 
                    help='By default, scanning continues to process new blocks as they are generated. When an end timestamp is given, scanning stops at that block height and the program exits', type=str)

args = parser.parse_args()

def lookup_node(address, chain, con, block_hash):
    result = con.execute('SELECT * FROM nodes WHERE address=?', [address]).fetchone()
    if result:
        return result[0]
    else:
        twin = chain.get_twin_by_account(address, block_hash)
        node = chain.get_node_by_twin(twin, block_hash)
        try:
            con.execute("INSERT INTO nodes VALUES(?, ?, ?)", (node, twin, address))
            con.commit()
        except sqlite3.IntegrityError:
            print('Tried to insert duplicate node:', node, twin, address)
            print('Existing node:', con.execute('SELECT * FROM nodes WHERE node_id=?', [node]).fetchone())
        return node

def process_block(chain, block_number, con):
    block = chain.sub.get_block(chain.sub.get_block_hash(block_number))
    timestamp = chain.get_timestamp(block) / 1000

    # Idea here is that we write data from the block and also note the fact that this block is processed in a single transaction that reverts if any error occurs (great idea?)
    # TODO: Some threads throw a timeout error using the default timeout of 5 seconds, while most continue on. Seems maybe some transaction is still open from what we did before starting to process blocks
    # Maybe (probably) this is caused because we are looking up node info on TF Chain and the client takes some time to get ready. That's why timeout only happen in the beginning. What we can do is prepare all data and then do the db operations in a separate dedicated step. Also, increasing the timeout to 15 seconds seemed to work fine
    with con:
        for extrinsic in block['extrinsics']:
            data = extrinsic.value
            if data['call']['call_function'] == 'report_uptime_v2':
                uptime = data['call']['call_args'][0]['value']
                node = lookup_node(data['address'], chain, con, block['header']['hash'])
                con.execute("INSERT INTO uptimes VALUES(?, ?, ?)", (node, uptime, timestamp))
            elif data['call']['call_function'] == 'change_power_target':
                target = data['call']['call_args'][1]['value']
                node = data['call']['call_args'][0]['value']
                con.execute("INSERT INTO power_target_changes VALUES(?, ?, ?)", (node, target, timestamp))

        con.execute("INSERT INTO processed_blocks VALUES(?)", (block_number,))

def processor(block_queue):
    # Each processor thread has its own TF Chain and db connections
    con = sqlite3.connect(args.file, timeout=15)
    main = tfchain.TFChain()
    while 1:
        block_number = block_queue.get()
        if block_number is None:
            return

        process_block(main, block_number, con)
        block_queue.task_done()

        # Seems like a nice idea as part of some retry scheme, but generates huge log spam about AttributeError: 'NoneType' object has no attribute 'portable_registry'
        # try:
        #     process_block(main, block_number, con)
        # except:
        #     logging.exception('Error while processing block')

def parallelize(start_block, end_block, block_queue):
    con = sqlite3.connect(args.file)
    results = con.execute("SELECT * from processed_blocks").fetchall()
    con.close()
    
    processed_blocks = {r[0] for r in results}
    remaining_blocks = set(range(start_block, end_block + 1)) - processed_blocks

    for b in remaining_blocks:
        block_queue.put(b)

    print('Starting', WORKERS, 'workers to process', block_queue.qsize(), 'blocks')

    threads = [spawn_worker(block_queue) for i in range(WORKERS)]
    return threads

def prep_db(con):
    #TODO: all tables should have a UNIQUE contraint to avoid duplicates
    con.execute("CREATE TABLE IF NOT EXISTS uptimes(node, uptime, timestamp, UNIQUE(node, uptime, timestamp)) ")
    con.execute("CREATE TABLE IF NOT EXISTS power_target_changes(node, target, timestamp, UNIQUE(node, target, timestamp))")
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

def spawn_worker(block_queue):
    thread = threading.Thread(target=processor, args=[block_queue])
    thread.start()
    return thread

def subscription_callback(block_queue, head, update_nr, subscription_id):
    block_queue.put(head['header']['number'])

# Open DB connection and make a set of all blocks already processed
con = sqlite3.connect(args.file)
prep_db(con)
populate_nodes(con)
results = con.execute("SELECT * from processed_blocks").fetchall()
processed_blocks = {r[0] for r in results}

main = tfchain.TFChain()
block_queue = queue.Queue()
start_block = main.find_block(args.start)['header']['number']

if args.end:
    end_block = main.find_block(args.end)['header']['number']
    parallelize(start_block, end_block, block_queue)
    # Wait for all jobs to finish. Since our threads aren't daemons, the program will exit after this
    block_queue.join()
else:
    # Since using the subscribe method blocks, we give it a thread
    callback = functools.partial(subscription_callback, block_queue)
    sub_thread = threading.Thread(target=main.sub.subscribe_block_headers, args=[callback])
    sub_thread.start()

    # We wait to get the first block number back from the subscribe callback, so that we're sure which block is the end of the historic range we want
    block = block_queue.get()
    block_queue.put(block)
    threads = parallelize(start_block, block - 1, block_queue)

    while 1:
        time.sleep(10)
        alive_threads = len([t for t in threads if t.is_alive()])
        print(datetime.datetime.now(), block_queue.qsize(), 'blocks remaining', alive_threads, 'threads alive')

        if block_queue.qsize() < WORKERS and alive_threads > 2:
            print('Less than',  WORKERS, 'jobs remaining, scaling down workers')
            for i in range(alive_threads - 2):
                block_queue.put(None)

        if block_queue.qsize() < WORKERS and alive_threads < 2:
            print('Less than', WORKERS, 'jobs remaining, but fewer than 2 workers. Spawning more workers')
            for i in range(2 - alive_threads):
                threads.append(spawn_thread(block_queue))

        if block_queue.qsize() > 5 and alive_threads < WORKERS:
            print('More than', WORKERS, 'jobs remaining but fewer threads. Spawning more workers')
            for i in range(WORKERS - alive_threads):
                threads.append(spawn_thread(block_queue))

        #TODO: Periodically check for missed blocks and retry them. Also, error handling inside the processor in general