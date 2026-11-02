"""Durable closed-candle collection and per-market model registry.

Only authenticated, validated requests explicitly declaring real market data enter
this store. Forecast output is never an input to this module.
"""
import json
import os
import sqlite3
from pathlib import Path
from contextlib import contextmanager

import pandas as pd

BASE_MODEL = 'NeoQuasar/Kronos-base'
TOKENIZER = 'NeoQuasar/Kronos-Tokenizer-base'


def enabled():
    return os.getenv('KRONOS_LEARNING_ENABLED', '0') == '1'


def state_dir():
    return Path(os.getenv('KRONOS_LEARNING_DIR', str(Path(__file__).resolve().parent / 'learning_state'))).resolve()


class Store:
    def __init__(self, root=None):
        self.root = Path(root) if root else state_dir()
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / 'learning.sqlite3'
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS candles (
                    symbol TEXT, timeframe TEXT, stamp INTEGER, payload TEXT,
                    PRIMARY KEY(symbol, timeframe, stamp));
                CREATE TABLE IF NOT EXISTS registry (
                    symbol TEXT, timeframe TEXT, active TEXT NOT NULL, previous TEXT,
                    PRIMARY KEY(symbol, timeframe));
                CREATE TABLE IF NOT EXISTS attempts (
                    symbol TEXT, timeframe TEXT, cutoff INTEGER, report TEXT,
                    PRIMARY KEY(symbol, timeframe));
            ''')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        try:
            with db:
                yield db
        finally:
            db.close()

    def collect(self, symbol, timeframe, history, stamps, seconds, now=None):
        now = pd.Timestamp.now(tz='UTC') if now is None else pd.Timestamp(now)
        rows = [(symbol, timeframe, int(stamp.value), json.dumps(row))
                for stamp, row in zip(stamps, history.to_dict('records'))
                if stamp + pd.Timedelta(seconds=seconds) <= now]
        with self.connect() as db:
            before = db.total_changes
            # First closed observation is immutable: retries cannot change training labels.
            db.executemany('INSERT OR IGNORE INTO candles VALUES (?, ?, ?, ?)', rows)
            return db.total_changes - before

    def markets(self):
        with self.connect() as db:
            return db.execute('SELECT DISTINCT symbol, timeframe FROM candles ORDER BY symbol, timeframe').fetchall()

    def snapshot(self, symbol, timeframe, limit=20000):
        with self.connect() as db:
            rows = db.execute('SELECT stamp, payload FROM candles WHERE symbol=? AND timeframe=? '
                              'ORDER BY stamp DESC LIMIT ?', (symbol, timeframe, limit)).fetchall()
        return pd.DataFrame([{'timestamps': pd.Timestamp(stamp, tz='UTC'), **json.loads(payload)}
                             for stamp, payload in reversed(rows)])

    def active(self, symbol, timeframe):
        with self.connect() as db:
            row = db.execute('SELECT active FROM registry WHERE symbol=? AND timeframe=?',
                             (symbol, timeframe)).fetchone()
        return row[0] if row else BASE_MODEL

    def cutoff(self, symbol, timeframe):
        with self.connect() as db:
            row = db.execute('SELECT cutoff FROM attempts WHERE symbol=? AND timeframe=?',
                             (symbol, timeframe)).fetchone()
        return row[0] if row else 0

    def finish(self, symbol, timeframe, cutoff, report, candidate=None, expected=None):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT active FROM registry WHERE symbol=? AND timeframe=?',
                             (symbol, timeframe)).fetchone()
            active = row[0] if row else BASE_MODEL
            if candidate is not None and active != expected:
                raise RuntimeError('Active model changed while training; refusing promotion')
            if candidate is not None:
                db.execute('INSERT OR REPLACE INTO registry VALUES (?, ?, ?, ?)',
                           (symbol, timeframe, candidate, active))
            db.execute('INSERT OR REPLACE INTO attempts VALUES (?, ?, ?, ?)',
                       (symbol, timeframe, cutoff, json.dumps(report, allow_nan=False)))

    def rollback(self, symbol, timeframe):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT active, previous FROM registry WHERE symbol=? AND timeframe=?',
                             (symbol, timeframe)).fetchone()
            if not row or not row[1]:
                raise ValueError('No previous model to restore')
            db.execute('UPDATE registry SET active=?, previous=? WHERE symbol=? AND timeframe=?',
                       (row[1], row[0], symbol, timeframe))
            return row[1]
