"""
This code is essentially a selective port of the v3 minting code, only including the parts needed to find farmerbot related violations. It reads from a sqlite database as generated by the ingester code, parses the events, and returns a list of any violations for that node.
"""

import sys, sqlite3, collections, time, queue, multiprocessing, logging
import grid3.minting


POST_PERIOD = 60 * 60 * 27
PERIOD_CATCH = 30
MAX_BOOT_TIME = 30 * 60

NodeUptimeReported = collections.namedtuple('NodeUptimeReported', 'uptime, timestamp, event_index')
PowerTargetChanged = collections.namedtuple('PowerTargetChanged', 'target, timestamp, event_index')
PowerStateChanged = collections.namedtuple('PowerStateChanged', 'state, timestamp, event_index')

# Nice idea, but the Telegram bot persistence doesn't play well with these
#Violation = collections.namedtuple('Violation', 'boot_requested, booted_at')

def check_node(con, node, period, verbose=False):
    # Checkpoints indicate the last block number and associated timestamp for which all block data has been ingested and processed. We don't want to assume a node has a violation if block processing is behind current time
    checkpoint_time = con.execute("SELECT value FROM kv WHERE key='checkpoint_time'").fetchone()[0]
    
    # Nodes have 30 minutes to wake up, so we need to check enough uptime events to see if they manage to wake up after the period has ended. Since the boot time and the time of submitting uptime are different events, and the uptime report can come much later, the post period duration is the effective limit on how long a node can spend "booting up" at the end of the period before getting a violation. We (now) use the same value as minting (27 hours) so that we reach the same conclusion as minting about whether to assign a violation or not
    if checkpoint_time > period.end + POST_PERIOD:
        end_time = period.end + POST_PERIOD
    else:
        end_time = checkpoint_time

    uptimes = con.execute('SELECT uptime, timestamp, event_index FROM NodeUptimeReported WHERE node_id=? AND timestamp>=?  AND timestamp<=?', (node, period.start, end_time)).fetchall()
    targets = con.execute('SELECT target, timestamp, event_index FROM PowerTargetChanged WHERE node_id=? AND timestamp>=?  AND timestamp<=?', (node, period.start, end_time)).fetchall()
    states = con.execute('SELECT state, timestamp, event_index FROM PowerStateChanged WHERE node_id=? AND timestamp>=? AND timestamp<=?', (node, period.start, end_time)).fetchall()

    # Since we only fetch initial power configs for the beginning of each period, there's no risk of fetching the wrong one unless we're off by a month. On the other hand, getting the exact timestamp of the block or the block number is relatively expensive, so we use a bit of a hack here. Maybe a better approach is caching the period start/end info inside the db
    initial_power = con.execute('SELECT state, down_time, target, timestamp FROM PowerState WHERE node_id=? AND timestamp>=?  AND timestamp<=?', [node, (period.start - PERIOD_CATCH), (period.start + PERIOD_CATCH)]).fetchone()

    # If there's no entry in the db, it would mean either the node was not created yet at this point in time (thus the default value), or the fetching of this data is not completed. The latter case is potentially problematic, but as long as we get the data eventually, we will catch any associated violations eventually too
    if initial_power is None:
        initial_power = 'Up', None, 'Up', None
    state, down_time, target, timestamp = initial_power

    if state == 'Down':
        # This is now using the same approach as minting (that is, we only care about the actual time the node went to sleep, not when a boot was requested if it happened in the previous minting period). While maybe not immediately obvious, we need the time the node went to sleep here to correctly check if the boot time is greater below
        power_managed = down_time
        if target == 'Up':
            power_manage_boot = timestamp # Block time of first block in period
        else:
            power_manage_boot = None
    else:
        power_managed = None
        power_manage_boot = None

    events = []
    events.extend([NodeUptimeReported(*u) for u in uptimes])
    events.extend([PowerStateChanged(*s) for s in states])
    events.extend([PowerTargetChanged(*t) for t in targets])

    events = sorted(events, key=lambda e: (e.timestamp, e.event_index))

    violations = []
    for event in events:
        if verbose:
            print(event)
        if type(event) is NodeUptimeReported:
            if power_managed is not None and power_manage_boot is not None:
                boot_time = event.timestamp - event.uptime
                if boot_time > power_managed:
                    if verbose:
                        print('Node booted at', boot_time, event)
                    if boot_time > power_manage_boot + MAX_BOOT_TIME:
                        if verbose:
                            print('About to return a violation for this uptime event:', event)
                        violations.append((power_manage_boot, boot_time))
                    
                    power_managed = None
                    power_manage_boot = None

        elif type(event) is PowerTargetChanged:
            if event.target == 'Up' and state == 'Down' and power_manage_boot is None:
                power_manage_boot = event.timestamp
            target = event.target

        elif type(event) is PowerStateChanged:
            if state == 'Up' and target == 'Down' and event.state == 'Down':
                if power_managed is None:
                    power_managed = event.timestamp
            state = event.state

        if verbose:
            print('power_managed:', power_managed, 'power_manage_boot', power_manage_boot)

    # There are two scenarios here. First is that we are scanning a completed minting period that ended longer ago than the POST_PERIOD duration. In that case these will be "never booted" violations. The other is that we are scanning an ongoing minting period (or one that ended very recently) and the node has exceeded the allowed 30 minutes. Either way we set the node's boot time to None to signal it is currently unknown. In the second case, it might become known later. Notice too that we only care about wake ups that were initiated before the period ended--those happening after will get checked with the next period
    # Actually, this is too eager. Minting actually only assigns violations when the node finally wakes up late, or at the end of the period. Nodes can (and do) send their first uptime report after MAX_BOOT_TIME without getting a violation (boot time was in the past). I guess it's up to the caller to decide what to do with "unfinished" violations, though we could differentiate them--assigning the period end + post period timestamp like minting does probably makes sense
    if power_manage_boot and end_time > power_manage_boot + MAX_BOOT_TIME:
        violations.append((power_manage_boot, None))

    return violations

def check_nodes_parallel(db_file, jobs, worker_count):
    # Each job is a node, period tuple
    # I tried using a JoinableQueue for the job queue and joining it before fetching the results. This causes, at least in some circumstances, some workers to exit due to the Empty exception before all jobs are in the queue (apparently) and thus one worker gets stuck with the rest of the work
    job_queue = multiprocessing.Queue()
    result_queue = multiprocessing.Queue()
    for job in jobs:
        job_queue.put(job)

    workers = []
    for i in range(worker_count):
        proc = multiprocessing.Process(target=worker_job, args=(db_file, job_queue, result_queue))
        proc.start()
        workers.append(proc)

    results = []
    result_count = 0
    while result_count < len(jobs):
        result = result_queue.get()
        # None means an error in the worker. We don't retry here, but the caller could detect that fewer results are returned than jobs given
        if result is not None:
            results.append(result)
        result_count += 1

    for _ in range(worker_count):
        job_queue.put(None)
        
    return results

def worker_job(db_file, job_queue, result_queue):
    con = sqlite3.connect(db_file)
    while 1:
        try:
            job = job_queue.get()
            if job is None:
                return
            else:
                node_id, period = job
            result_queue.put((node_id, period, check_node(con, node_id, period)))
        except:
            logging.exception('Error in violation check worker')
            result_queue.put(None)

if __name__ == '__main__':
    DB = sys.argv[1]
    NODE = int(sys.argv[2])
    TIME = int(sys.argv[3]) #Pass any timestamp in the period to be checked
    try:
        sys.argv[4]
        VERBOSE = True
    except IndexError:
        VERBOSE = False

    con = sqlite3.connect(DB)
    period = grid3.minting.Period(TIME)
    print(check_node(con, NODE, grid3.minting.Period(TIME), VERBOSE))