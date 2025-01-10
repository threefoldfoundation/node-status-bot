import argparse
import sqlite3
import time


def wake_node(db_file, node_id):
    """Simulate a node waking up by adding uptime report and power state change"""
    con = sqlite3.connect(db_file)
    cursor = con.cursor()
    
    # Get current time and checkpoint block
    now = time.time()
    cursor.execute("SELECT value FROM kv WHERE key='checkpoint_block'")
    block = int(cursor.fetchone()[0])
    
    # Add uptime report
    block += 1
    con.execute(
        "INSERT INTO NodeUptimeReported (farm_id, node_id, uptime, block, event_index, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
        (1, node_id, 0, block, 0, now),  # Uptime starts at 0 when node boots
    )
    
    # Add power state change to Up
    block += 1
    con.execute(
        "INSERT INTO PowerStateChanged (farm_id, node_id, state, down_block, block, event_index, timestamp) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (1, node_id, "Up", None, block, 0, now),
    )
    
    # Update checkpoint block and time
    con.execute(
        "UPDATE kv SET value=? WHERE key='checkpoint_block'",
        (block,),
    )
    con.execute(
        "UPDATE kv SET value=? WHERE key='checkpoint_time'",
        (now,),
    )
    
    con.commit()
    con.close()
    print(f"Simulated wake up for node {node_id} in {db_file}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("db_file", help="Path to SQLite database file")
    parser.add_argument("node_id", type=int, help="Node ID to wake up")
    args = parser.parse_args()

    wake_node(args.db_file, args.node_id)
