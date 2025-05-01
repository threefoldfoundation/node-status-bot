import argparse
import pickle
from datetime import datetime
from typing import Dict, List, Any

from db import RqliteDB

def migrate_chats(db: RqliteDB, chats: Dict[int, Dict[str, Any]]) -> None:
    """Migrate chat data from pickle to rqlite"""
    for chat_id, chat_data in chats.items():
        print(f"Migrating chat {chat_id}")
        # Create chat record if it doesn't exist
        db.create_chat(chat_id)

        # Set network for chat
        net = chat_data.get('net', 'main')
        db.update_chat_network(chat_id, net)

        # Set timeout if exists
        if 'timeout' in chat_data:
            db.set_chat_timeout(chat_id, chat_data['timeout'])

        # Migrate subscriptions per network
        for net in ['main', 'test', 'dev']:
            node_ids = chat_data['nodes'].get(net, [])
            if node_ids:
                db.add_subscriptions(chat_id, net, node_ids)

def migrate_nodes(db: RqliteDB, nodes: Dict[str, Dict[int, Any]]) -> None:
    """Migrate node data from pickle to rqlite"""
    for net in ['main', 'test', 'dev']:
        for node_id, node_data in nodes[net].items():
            # Create node with basic data from Node object
            node_dict = {
                'nodeId': node_id,
                'power': node_data.power,
                'status': getattr(node_data, 'status', 'down'),
                'updatedAt': node_data.updatedAt,
                'farmerbot': getattr(node_data, 'farmerbot', False)
            }
            db.create_node(node_dict, net)

            # Migrate violations if any
            violations = getattr(node_data, 'violations', {})
            if violations:
                # Convert dict values to list of Violation objects
                violation_list = list(violations.values())
                db.add_violations(node_id, net, violation_list)

def migrate_metadata(db: RqliteDB, bot_data: Dict[str, Any]) -> None:
    """Migrate metadata like violations_populated flag"""
    if bot_data.get('violations_populated', False):
        db.set_metadata('violations_populated', 'true')

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('pickle_file', help='Path to pickle persistence file')
    parser.add_argument('--rqlite-host', default='localhost', help='Rqlite host')
    parser.add_argument('--rqlite-port', type=int, default=4001, help='Rqlite port')
    args = parser.parse_args()

    # Load pickle data
    with open(args.pickle_file, 'rb') as f:
        pickle_data = pickle.load(f)

    # Get the actual bot data from the pickle structure
    bot_data = pickle_data.get('bot_data', {})
    if not bot_data:
        print("Error: No bot_data found in pickle file")
        return None

    # Initialize rqlite connection
    db = RqliteDB(host=args.rqlite_host, port=args.rqlite_port)

    # Perform migrations
    if 'chats' in bot_data:
        migrate_chats(db, bot_data['chats'])
    if 'nodes' in bot_data:
        migrate_nodes(db, bot_data['nodes'])
    migrate_metadata(db, bot_data)

    print("Migration completed successfully")

    return bot_data

if __name__ == '__main__':
    bot_data = main()
