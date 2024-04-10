import sqlite3, datetime, threading, queue, datetime, sys
import grid3.network
from grid3 import tfchain

WORKERS = 25

if len(sys.argv) < 4:
    print('Three args are required: db file, start time, and end time.')
    exit()

DBFILE = sys.argv[1]
START = int(sys.argv[2])
END = int(sys.argv[3])

# First build some maps to help us figure out which node is connected with which account. We grab the full list of twins here, because some nodes are deleted and we'll need to look them up from the chain later
graphql = grid3.network.GridNetwork().graphql
nodes = graphql.nodes(['nodeID', 'twinID'])
twins = graphql.twins(['twinID', 'accountID'])
twin_to_node = {n['twinID']: n['nodeID'] for n in nodes}
account_to_twin = {t['accountID']: t['twinID'] for t in twins}
twin_to_account = {v: k for k, v in account_to_twin.items()}
account_to_node = {twin_to_account[n['twinID']]: n['nodeID'] for n in nodes}

def lookup_node(address, chain):
    try:
        return account_to_node[address]
    except KeyError:
        node = chain.get_node_by_twin(account_to_twin[address])
        account_to_node[address] = node
        return node

# This is the call signature we'd need for the substrate client's subscription to new blocks feature. Probably just make a wrapper function that throws away the extra args
#def process_block(head, update_nr, subscription_id):
#    block = main.sub.get_block(head['hash'])

def process_block(chain, block, con, timestamp):
    # Grab data for all nodes 
    for extrinsic in block['extrinsics']:
        data = extrinsic.value
        if data['call']['call_function'] == 'report_uptime_v2':
            uptime = data['call']['call_args'][0]['value']
            node = lookup_node(data['address'], chain)
            con.execute("INSERT INTO uptimes VALUES(?, ?, ?)", (node, uptime, timestamp))
        elif data['call']['call_function'] == 'change_power_target':
            target = data['call']['call_args'][1]['value']
            node = data['call']['call_args'][0]['value']
            con.execute("INSERT INTO power_target_changes VALUES(?, ?, ?)", (node, target, timestamp))

def processor(blocks):
    # Each processor thread has its own TF Chain and db connections
    con = sqlite3.connect(DBFILE)
    main = tfchain.TFChain()
    while 1:
        try:
            block_number = blocks.get(block=False)
        except queue.Empty:
            return

        if block_number % 100 == 0:
            print('Processing block', block_number, 'at', datetime.datetime.now(), 'Blocks remaining:', blocks.qsize())
        block = main.sub.get_block(main.sub.get_block_hash(block_number))
        timestamp = main.get_timestamp(block) / 1000

        # Idea here is that we write data from the block and also note the fact that this block is processed in a single transaction that reverts if any error occurs (great idea?)
        # TODO: Some threads throw a timeout error using the default timeout of 5 seconds, while most continue on. Seems maybe some transaction is still open from what we did before starting to process blocks
        with con:
            process_block(main, block, con, timestamp)
            con.execute("INSERT INTO processed_blocks VALUES(?)", (block_number,))

def parallelize(start, end):
    con = sqlite3.connect(DBFILE)
    main = tfchain.TFChain()
    blocks = queue.Queue()
    results = con.execute("SELECT * from processed_blocks").fetchall()
    processed_blocks = {r[0] for r in results}
    for i in range(main.find_block(start)['header']['number'], main.find_block(end)['header']['number']):
        if i not in processed_blocks:
            blocks.put(i)

    print('Starting', WORKERS, 'workers to process', blocks.qsize(), 'blocks')
    for i in range(WORKERS):
        threading.Thread(target=processor, args=[blocks]).start()

    blocks.join()

def prep_db():
    con = sqlite3.connect(DBFILE)
    con.execute("CREATE TABLE IF NOT EXISTS uptimes(node, uptime, timestamp)")
    con.execute("CREATE TABLE IF NOT EXISTS power_target_changes(node, target, timestamp)")
    con.execute("CREATE TABLE IF NOT EXISTS processed_blocks(block_number)")
    # So far we keep all the node data in memory, but storing in locally in this table would speed up the start time greatly
    con.execute("CREATE TABLE IF NOT EXISTS nodes(node_id, twin_id, address)")
    con.commit()

prep_db()
parallelize(START, END)