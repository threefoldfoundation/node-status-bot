import sqlite3
import time
import argparse

def create_violation(db_file):
    """Create a violation scenario for node 1 in the database"""
    con = sqlite3.connect(db_file)
    
    # Create tables if they don't exist
    con.execute("""
        CREATE TABLE IF NOT EXISTS PowerTargetChanged (
            id INTEGER PRIMARY KEY,
            node_id INTEGER NOT NULL,
            timestamp REAL NOT NULL,
            old_state TEXT,
            new_state TEXT
        )
    """)
    
    # Get current time
    now = time.time()
    
    # Create violation scenario:
    # 1. Node 1 is put in standby (Down) 40 minutes ago
    # 2. Boot is requested (Up) 30 minutes ago
    # 3. Node has not booted yet
    
    # Add power target change to Down (standby)
    con.execute(
        "INSERT INTO PowerTargetChanged (node_id, timestamp, old_state, new_state) VALUES (?, ?, ?, ?)",
        (1, now - 2400, 'Up', 'Down')  # 40 minutes ago
    )
    
    # Add power target change to Up (boot requested)
    con.execute(
        "INSERT INTO PowerTargetChanged (node_id, timestamp, old_state, new_state) VALUES (?, ?, ?, ?)",
        (1, now - 1800, 'Down', 'Up')  # 30 minutes ago
    )
    
    con.commit()
    con.close()
    print(f"Created violation scenario for node 1 in {db_file}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("db_file", help="Path to SQLite database file")
    args = parser.parse_args()
    
    create_violation(args.db_file)
