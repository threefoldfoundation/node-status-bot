import sqlite3
import time
import argparse


def create_violation(db_file):
    """Create a violation scenario for node 1 in the database"""
    con = sqlite3.connect(db_file)

    # Get current time
    now = time.time()

    # Create violation scenario:
    # 1. Node 1 is put in standby (Down) 42 minutes ago
    # 2. Boot is requested (Up) 41 minutes ago (the node status bot gives a 10 minute tolerance beyond the 30 minute window)
    # 3. Node has not booted yet

    # Add power target change to Down (standby)
    con.execute(
        "INSERT INTO PowerTargetChanged (farm_id, node_id, target, block, event_index, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
        (1, 1, "Down", 1, 0, now - 2520),  # 42 minutes ago
    )

    # Add power state change to Down (standby)
    con.execute(
        "INSERT INTO PowerStateChanged (farm_id, node_id, state, down_block, block, event_index, timestamp) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (1, 1, "Down", None, 2, 0, now - 2490),  # 41.5 minutes ago
    )

    # Add power target change to Up (boot requested)
    con.execute(
        "INSERT INTO PowerTargetChanged (farm_id, node_id, target, block, event_index, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
        (1, 1, "Up", 3, 0, now - 2460),  # 41 minutes ago
    )

    # Also set the checkpoint_time, otherwise these events won't be processed
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
