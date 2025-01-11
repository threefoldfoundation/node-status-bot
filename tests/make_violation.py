import argparse
import sqlite3
import time


def create_violation(db_file):
    """Create a violation scenario for node 1 in the database"""
    con = sqlite3.connect(db_file)

    # Get current time
    now = time.time()

    # Create violation scenario:
    # 1. Node 1 is put in standby (Down) 42 minutes ago
    # 2. Boot is requested (Up) 41 minutes ago
    # 3. Node boots now and receives a violation

    # Add power target change to Down (standby)
    # Get current checkpoint block
    cursor = con.cursor()
    cursor.execute("SELECT value FROM kv WHERE key='checkpoint_block'")
    block = int(cursor.fetchone()[0])

    # Add power target change to Down (standby)
    block += 1
    con.execute(
        "INSERT INTO PowerTargetChanged (farm_id, node_id, target, block, event_index, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
        (1, 1, "Down", block, 0, now - 2520),  # 42 minutes ago
    )

    # Add power state change to Down (standby)
    block += 1
    con.execute(
        "INSERT INTO PowerStateChanged (farm_id, node_id, state, down_block, block, event_index, timestamp) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (1, 1, "Down", None, block, 0, now - 2490),  # 41.5 minutes ago
    )

    # Add power target change to Up (boot requested)
    block += 1
    con.execute(
        "INSERT INTO PowerTargetChanged (farm_id, node_id, target, block, event_index, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
        (1, 1, "Up", block, 0, now - 2460),  # 41 minutes ago
    )

    # Add uptime report
    block += 1
    con.execute(
        "INSERT INTO NodeUptimeReported (node_id, uptime, timestamp_hint, block, event_index, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
        (1, 1, now, block, 0, now),  # Uptime starts at 1 when node boots
    )

    # Add power state change to Up
    block += 1
    con.execute(
        "INSERT INTO PowerStateChanged (farm_id, node_id, state, down_block, block, event_index, timestamp) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (1, 1, "Up", None, block, 0, now),
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
    print(f"Created violation scenario for node 1 in {db_file}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("db_file", help="Path to SQLite database file")
    args = parser.parse_args()

    create_violation(args.db_file)
