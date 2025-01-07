from typing import Any, Dict

import rqlite


class RqliteDB:
    def __init__(self, host: str = "http://localhost:4001"):
        self.conn = rqlite.connect(host)
        self._init_db()

    def _init_db(self):
        """Initialize database schema"""
        queries = [
            """CREATE TABLE IF NOT EXISTS chats (
                chat_id INTEGER PRIMARY KEY,
                net TEXT NOT NULL DEFAULT 'main'
            )""",
            """CREATE TABLE IF NOT EXISTS subscriptions (
                chat_id INTEGER,
                network TEXT,
                node_id INTEGER,
                PRIMARY KEY (chat_id, network, node_id),
                FOREIGN KEY (chat_id) REFERENCES chats(chat_id),
                FOREIGN KEY (node_id, network) REFERENCES nodes(node_id, network)
            )""",
            """CREATE TABLE IF NOT EXISTS nodes (
                node_id INTEGER,
                network TEXT,
                status TEXT,
                updated_at REAL,
                power_state TEXT,
                power_target TEXT,
                farmerbot BOOLEAN DEFAULT FALSE,
                PRIMARY KEY (node_id, network)
            )""",
            """CREATE TABLE IF NOT EXISTS violations (
                node_id INTEGER,
                network TEXT,
                boot_requested REAL,
                booted_at REAL,
                end_time REAL,
                finalized BOOLEAN,
                PRIMARY KEY (node_id, network, boot_requested),
                FOREIGN KEY (node_id, network) REFERENCES nodes(node_id, network)
            )"""
        ]

        with self.conn.cursor() as cursor:
            for query in queries:
                cursor.execute(query)

    def get_chat(self, chat_id: int) -> Dict[str, Any]:
        """Get chat data or create new if doesn't exist"""
        with self.conn.cursor() as cursor:
            cursor.execute("""
                INSERT OR IGNORE INTO chats (chat_id) VALUES (?)
            """, (chat_id,))

            cursor.execute("""
                SELECT c.chat_id, c.net,
                       GROUP_CONCAT(cn.node_id) AS nodes
                FROM chats c
                LEFT JOIN subscriptions cn ON c.chat_id = cn.chat_id
                WHERE c.chat_id = ?
                GROUP BY c.chat_id
            """, (chat_id,))

            row = cursor.fetchone()
            if row:
                return {
                    'net': row[1],
                    'nodes': {
                        'main': [],
                        'test': [],
                        'dev': []
                    } if not row[2] else {
                        'main': [int(n) for n in row[2].split(',') if n],
                        'test': [],
                        'dev': []
                    }
                }
            return {
                'net': 'main',
                'nodes': {
                    'main': [],
                    'test': [],
                    'dev': []
                }
            }

    def update_chat_network(self, chat_id: int, network: str):
        with self.conn.cursor() as cursor:
            cursor.execute("""
                UPDATE chats SET net = ? WHERE chat_id = ?
            """, (network, chat_id))

    def add_chat_node(self, chat_id: int, network: str, node_id: int):
        with self.conn.cursor() as cursor:
            cursor.execute("""
                INSERT OR IGNORE INTO subscriptions (chat_id, network, node_id)
                VALUES (?, ?, ?)
            """, (chat_id, network, node_id))

    def remove_chat_node(self, chat_id: int, network: str, node_id: int):
        with self.conn.cursor() as cursor:
            cursor.execute("""
                DELETE FROM subscriptions
                WHERE chat_id = ? AND network = ? AND node_id = ?
            """, (chat_id, network, node_id))

    def get_node(self, node_id: int, network: str) -> Dict[str, Any]:
        with self.conn.cursor() as cursor:
            cursor.execute("""
                SELECT node_id, network, status, updated_at,
                       power_state, power_target, farmerbot
                FROM nodes
                WHERE node_id = ? AND network = ?
            """, (node_id, network))

            row = cursor.fetchone()
            if row:
                return {
                    'nodeId': row[0],
                    'status': row[2],
                    'updatedAt': row[3],
                    'power': {
                        'state': row[4],
                        'target': row[5]
                    },
                    'farmerbot': bool(row[6]),
                    'violations': self.get_node_violations(node_id, network)
                }
            return None

    def update_node(self, node: Dict[str, Any], network: str):
        with self.conn.cursor() as cursor:
            cursor.execute("""
                INSERT OR REPLACE INTO nodes
                (node_id, network, status, updated_at,
                 power_state, power_target, farmerbot)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                node['nodeId'],
                network,
                node['status'],
                node['updatedAt'],
                node['power']['state'],
                node['power']['target'],
                node.get('farmerbot', False)
            ))

    def get_node_violations(self, node_id: int, network: str) -> Dict[float, Dict]:
        with self.conn.cursor() as cursor:
            cursor.execute("""
                SELECT boot_requested, booted_at, end_time, finalized
                FROM violations
                WHERE node_id = ? AND network = ?
            """, (node_id, network))

            return {
                row[0]: {
                    'boot_requested': row[0],
                    'booted_at': row[1],
                    'end_time': row[2],
                    'finalized': bool(row[3])
                }
                for row in cursor.fetchall()
            }

    def add_violation(self, node_id: int, network: str, violation: Dict[str, Any]):
        with self.conn.cursor() as cursor:
            cursor.execute("""
                INSERT OR REPLACE INTO violations
                (node_id, network, boot_requested, booted_at, end_time, finalized)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                node_id,
                network,
                violation['boot_requested'],
                violation['booted_at'],
                violation['end_time'],
                violation['finalized']
            ))
