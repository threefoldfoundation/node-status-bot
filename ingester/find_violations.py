import sys, sqlite3, collections
import grid3.minting

NodeUptimeReported = collections.namedtuple('NodeUptimeReported', 'uptime, timestamp, event_index')
PowerTargetChanged = collections.namedtuple('PowerTargetChanged', 'target, timestamp, event_index')
PowerStateChanged = collections.namedtuple('PowerStateChanged', 'state, timestamp, event_index')

def check_node(con, node, period, verbose=False):
    # Nodes have 30 minutes to wake up, so we need to check enough uptime events to see if they manage to wake up after the period has ended. One hour should be more than enough time. Minting checks blocks for more than a day after period end though, so we will generate more "never booted" violations versus minting where post period violations will more often have a timestamp
    uptimes = con.execute('SELECT uptime, timestamp, event_index FROM NodeUptimeReported WHERE node_id=? AND timestamp>=?  AND timestamp<=?', (node, period.start, period.end + 60 * 60)).fetchall()
    targets = con.execute('SELECT target, timestamp, event_index FROM PowerTargetChanged WHERE node_id=? AND timestamp>=?  AND timestamp<=?', (node, period.start, period.end)).fetchall()
    states = con.execute('SELECT state, timestamp, event_index FROM PowerStateChanged WHERE node_id=? AND timestamp>=? AND timestamp<=?', (node, period.start, period.end)).fetchall()

    # Since we only fetch initial power configs for the beginning of each period, we just need to get the closest one. To get precise, would need to target based on the block number or the timestamp of the block
    initial_power = con.execute('SELECT state, block, target, timestamp FROM PowerState WHERE node_id=? AND timestamp>=?  AND timestamp<=?', [node, (period.start - 30), (period.start + 30)]).fetchone()
    if initial_power is None:
        initial_power = 'Up', None, 'Up', None
    state, down_block, target, timestamp = initial_power

    if state == 'Down':
        # Minting actually looks up the time stamp of the block when the node went to sleep. We don't care because that's only needed to calculate the uptime properly, not detect if the node has a violation 
        power_managed = timestamp
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

    for event in events:
        if verbose:
            print(event)
        if type(event) is NodeUptimeReported:
            if power_managed is not None and power_manage_boot is not None:
                boot_time = event.timestamp - event.uptime
                if boot_time > power_managed:
                    if verbose:
                        print('Node booted at', boot_time, event)
                    if boot_time > power_manage_boot + 60 * 30:
                        if verbose:
                            print('About to return a violation for this uptime event:', event)
                        return 'Power managed node requested to boot at ' + str(int(power_manage_boot)) + ' but only booted at ' + str(int(boot_time))
                        print('Power managed node requested to boot at', int(power_manage_boot), 'but only booted at', int(boot_time))
                    
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

    if power_manage_boot:
        return 'Power managed node requested to boot at ' + str(int(power_manage_boot)) + ' but never booted'
        print('Power managed node requested to boot at', int(power_manage_boot), 'but never booted')

if __name__ == '__main__':
    DB = sys.argv[1]
    NODE = int(sys.argv[2])
    TIME = int(sys.argv[3]) #Pass any timestamp in the period to be checked
    VERBOSE = False

    con = sqlite3.connect(DB)
    period = grid3.minting.Period(TIME)
    print(check_node(con, NODE, grid3.minting.Period(TIME), VERBOSE))