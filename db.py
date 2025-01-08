from typing import Any, Dict, List, Tuple

import pyrqlite.dbapi2 as dbapi2


class RqliteDB:
    def __init__(self, host: str = "localhost", port: int = 4001):
        self.conn = dbapi2.connect(
            host=host,
            port=port,
        )
        self._enable_foreign_keys()
        self._init_db()

    def _enable_foreign_keys(self):
        """Enable foreign key constraints for SQLite"""
        with self.conn.cursor() as cursor:
            cursor.execute("PRAGMA foreign_keys = ON")

    def _init_db(self):
        """Initialize database schema"""
        queries = [
            """CREATE TABLE IF NOT EXISTS chats (
                chat_id INTEGER PRIMARY KEY,
                net TEXT NOT NULL DEFAULT 'main',
                timeout INTEGER DEFAULT 10
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
            """CREATE TABLE IF NOT EXISTS subscriptions (
                chat_id INTEGER,
                network TEXT,
                node_id INTEGER,
                PRIMARY KEY (chat_id, network, node_id),
                FOREIGN KEY (chat_id) REFERENCES chats(chat_id),
                FOREIGN KEY (node_id, network) REFERENCES nodes(node_id, network)
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
            )""",
            """CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT
            )""",
        ]

        with self.conn.cursor() as cursor:
            for query in queries:
                cursor.execute(query)

    def create_chat(self, chat_id: int) -> None:
        """Create a new chat with default settings if it doesn't exist"""
        with self.conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT OR IGNORE INTO chats (chat_id) VALUES (?)
            """,
                (chat_id,),
            )

    def get_subscribed_nodes(self, chat_id: int, network: str) -> List[int]:
        """Get list of node IDs that a chat is subscribed to for a specific network"""
        with self.conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT s.node_id
                FROM subscriptions s
                WHERE s.chat_id = ? AND s.network = ?
            """,
                (chat_id, network),
            )

            return [row[0] for row in cursor.fetchall() if row[0] is not None]

    def update_chat_network(self, chat_id: int, network: str):
        with self.conn.cursor() as cursor:
            cursor.execute(
                """
                UPDATE chats SET net = ? WHERE chat_id = ?
            """,
                (network, chat_id),
            )

    def add_subscription(self, chat_id: int, network: str, node_id: int):
        """Add a single subscription (kept for backward compatibility)"""
        self.add_subscriptions(chat_id, network, [node_id])

    def add_subscriptions(self, chat_id: int, network: str, node_ids: List[int]):
        """Add multiple subscriptions in a single request"""
        with self.conn.cursor() as cursor:
            cursor.executemany(
                """
                INSERT OR IGNORE INTO subscriptions (chat_id, network, node_id)
                VALUES (?, ?, ?)
            """,
                [(chat_id, network, node_id) for node_id in node_ids],
            )

    def remove_subscription(self, chat_id: int, network: str, node_id: int):
        """Remove a single subscription (kept for backward compatibility)"""
        self.remove_subscriptions(chat_id, network, [node_id])

    def remove_subscriptions(self, chat_id: int, network: str, node_ids: List[int]):
        """Remove multiple subscriptions in a single request"""
        with self.conn.cursor() as cursor:
            cursor.executemany(
                """
                DELETE FROM subscriptions
                WHERE chat_id = ? AND network = ? AND node_id = ?
            """,
                [(chat_id, network, node_id) for node_id in node_ids],
            )

    def get_node(self, node_id: int, network: str) -> Dict[str, Any]:
        with self.conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT node_id, network, status, updated_at,
                       power_state, power_target, farmerbot
                FROM nodes
                WHERE node_id = ? AND network = ?
            """,
                (node_id, network),
            )

            row = cursor.fetchone()
            if row:
                return {
                    "nodeId": row[0],
                    "status": row[2],
                    "updatedAt": row[3],
                    "power": {"state": row[4], "target": row[5]},
                    "farmerbot": bool(row[6]),
                    "violations": self.get_node_violations(node_id, network),
                }
            return None

    def create_node(self, node, network: str):
        with self.conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT OR IGNORE INTO nodes
                (node_id, network, status, updated_at,
                 power_state, power_target, farmerbot)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    node.nodeId,
                    network,
                    node.status,
                    node.updatedAt,
                    node.power["state"],
                    node.power["target"],
                    getattr(node, "farmerbot", False),
                ),
            )

    def update_node(self, node, network: str):
        with self.conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT OR REPLACE INTO nodes
                (node_id, network, status, updated_at,
                 power_state, power_target, farmerbot)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    node.nodeId,
                    network,
                    node.status,
                    node.updatedAt,
                    node.power["state"],
                    node.power["target"],
                    getattr(node, "farmerbot", False),
                ),
            )

    def get_node_violations(self, node_id: int, network: str) -> Dict[float, Dict]:
        with self.conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT boot_requested, booted_at, end_time, finalized
                FROM violations
                WHERE node_id = ? AND network = ?
            """,
                (node_id, network),
            )

            return {
                row[0]: {
                    "boot_requested": row[0],
                    "booted_at": row[1],
                    "end_time": row[2],
                    "finalized": bool(row[3]),
                }
                for row in cursor.fetchall()
            }

    def add_violation(self, node_id: int, network: str, violation: Dict[str, Any]):
        """Add a single violation (kept for backward compatibility)"""
        self.add_violations(node_id, network, [violation])

    def add_violations(self, node_id: int, network: str, violations: List[Dict[str, Any]]):
        """Add multiple violations in a single request"""
        with self.conn.cursor() as cursor:
            cursor.executemany(
                """
                INSERT OR REPLACE INTO violations
                (node_id, network, boot_requested, booted_at, end_time, finalized)
                VALUES (?, ?, ?, ?, ?, ?)
            """,
                [
                    (
                        node_id,
                        network,
                        v["boot_requested"],
                        v["booted_at"],
                        v["end_time"],
                        v["finalized"],
                    )
                    for v in violations
                ],
            )

    def get_all_subscribed_nodes(self) -> List[Tuple[int, List[int]]]:
        """Get list of all nodes with active subscriptions

        Returns:
            List of tuples where each tuple contains:
            - node_id: int
            - chat_ids: List[int] of chat IDs subscribed to this node
        """
        with self.conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT n.node_id, GROUP_CONCAT(s.chat_id)
                FROM nodes n
                JOIN subscriptions s ON n.node_id = s.node_id AND n.network = s.network
                GROUP BY n.node_id
                """
            )

            # Convert the comma-separated chat_ids string to a list of integers
            return [
                (row[0], [int(chat_id) for chat_id in row[1].split(",")])
                for row in cursor.fetchall()
            ]

    def get_chat_network(self, chat_id: int) -> str:
        """Get the selected network for a chat"""
        with self.conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT net FROM chats WHERE chat_id = ?
                """,
                (chat_id,),
            )
            row = cursor.fetchone()
            return row[0] if row else "main"

    def get_metadata(self, key: str) -> str:
        """Get metadata value by key"""
        with self.conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT value FROM metadata WHERE key = ?
                """,
                (key,),
            )
            row = cursor.fetchone()
            return row[0] if row else None

    def set_metadata(self, key: str, value: str) -> None:
        """Set metadata value by key"""
        with self.conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT OR REPLACE INTO metadata (key, value)
                VALUES (?, ?)
                """,
                (key, value),
            )

    def get_chat_timeout(self, chat_id: int) -> int:
        """Get the timeout setting for a chat"""
        with self.conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT timeout FROM chats WHERE chat_id = ?
                """,
                (chat_id,),
            )
            row = cursor.fetchone()
            return row[0] if row else 10

    def set_chat_timeout(self, chat_id: int, timeout: int) -> None:
        """Set the timeout setting for a chat"""
        with self.conn.cursor() as cursor:
            cursor.execute(
                """
                UPDATE chats SET timeout = ? WHERE chat_id = ?
                """,
                (timeout, chat_id),
            )
