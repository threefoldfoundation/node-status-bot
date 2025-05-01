import argparse
import pickle
from datetime import datetime
from typing import Dict, List, Any

from db import RqliteDB

def migrate_chats(db: RqliteDB, chats: Dict[int, Dict[str, Any]]) -> None:
    """Migrate chat data from pickle to rqlite"""
    for chat_id, chat_data in chats.items():
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
            # Create node with basic data
            db.create_node({
                'nodeId': node_id,
                'power': node_data.power,
                'status': node_data.status,
                'updatedAt': node_data.updatedAt,
                'farmerbot': getattr(node_data, 'farmerbot', False)
            }, net)
            
            # Migrate violations if any
            violations = getattr(node_data, 'violations', {})
            if violations:
                db.add_violations(node_id, net, list(violations.values())

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
        bot_data = pickle.load(f)

    # Initialize rqlite connection
    db = RqliteDB(host=args.rqlite_host, port=args.rqlite_port)

    # Perform migrations
    if 'chats' in bot_data:
        migrate_chats(db, bot_data['chats'])
    if 'nodes' in bot_data:
        migrate_nodes(db, bot_data['nodes'])
    migrate_metadata(db, bot_data)

    print("Migration completed successfully")

if __name__ == '__main__':
    main()
