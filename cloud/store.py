"""PostgreSQL pre-effect claims and encrypted records. Never ephemeral SQLite."""
import hashlib
import json
import os
import threading


class DurableStore:
    def __init__(self, namespace, *, singleton=False):
        import psycopg
        from cryptography.fernet import Fernet
        url = os.environ.get('DATABASE_URL', '')
        if not url.startswith(('postgres://', 'postgresql://')):
            raise ValueError('durable_database_required')
        self.namespace = namespace
        self.cipher = Fernet(os.environ['STATE_ENCRYPTION_KEY'].encode())
        self.connection = psycopg.connect(url, autocommit=True, connect_timeout=5)
        self.mutex = threading.RLock()
        self.connection.execute('SET statement_timeout = 5000')
        self.connection.execute('CREATE TABLE IF NOT EXISTS cloud_claims (namespace text NOT NULL, key text NOT NULL, digest text NOT NULL, status text NOT NULL, PRIMARY KEY(namespace,key))')
        self.connection.execute('CREATE TABLE IF NOT EXISTS cloud_records (namespace text NOT NULL, key text NOT NULL, encrypted bytea NOT NULL, PRIMARY KEY(namespace,key))')
        self.lock = int.from_bytes(hashlib.sha256(namespace.encode()).digest()[:8], 'big', signed=True)
        if singleton and not self.connection.execute('SELECT pg_try_advisory_lock(%s)', (self.lock,)).fetchone()[0]:
            self.close()
            raise ValueError('single_owner_already_active')

    def alive(self):
        with self.mutex:
            self.connection.execute('SELECT 1')

    def claim(self, key, digest):
        with self.mutex:
            self.alive()
            row = self.connection.execute('INSERT INTO cloud_claims VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING key',
                (self.namespace, key, digest, 'uncertain')).fetchone()
            if row:
                return 'new'
            old = self.connection.execute('SELECT digest,status FROM cloud_claims WHERE namespace=%s AND key=%s',
                (self.namespace, key)).fetchone()
            return old[1] if old[0] == digest else 'conflict'

    def finish(self, key, status):
        if status not in {'accepted', 'rejected', 'done', 'not_attempted'}:
            raise ValueError('invalid_claim_status')
        with self.mutex:
            self.connection.execute('UPDATE cloud_claims SET status=%s WHERE namespace=%s AND key=%s',
                                    (status, self.namespace, key))

    def read(self, key):
        with self.mutex:
            row = self.connection.execute('SELECT encrypted FROM cloud_records WHERE namespace=%s AND key=%s',
                                          (self.namespace, key)).fetchone()
            return json.loads(self.cipher.decrypt(bytes(row[0]))) if row else None

    def write(self, key, value, *, reserve=False):
        encrypted = self.cipher.encrypt(json.dumps(value, separators=(',', ':')).encode())
        with self.mutex:
            if reserve:
                self.connection.execute('INSERT INTO cloud_records VALUES (%s,%s,%s)', (self.namespace, key, encrypted))
            else:
                self.connection.execute('INSERT INTO cloud_records VALUES (%s,%s,%s) ON CONFLICT(namespace,key) DO UPDATE SET encrypted=EXCLUDED.encrypted',
                                        (self.namespace, key, encrypted))

    def close(self):
        self.connection.close()
