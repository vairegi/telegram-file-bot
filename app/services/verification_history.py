"""v5.5 per-solve audit, separate from the old daily user aggregate.
Only successful token redemption records a row. No raw verification token,
API key or shortener destination is stored. Historical missing data is unknown.
"""
import hashlib
import time
from . import repo


def _ensure_sql():
    repo.execute('''CREATE TABLE IF NOT EXISTS verification_history (
        event_id TEXT PRIMARY KEY, user_id INTEGER NOT NULL,
        sent_at REAL, elapsed REAL, provider TEXT NOT NULL, redeemed_at REAL NOT NULL)''')
    repo.execute('CREATE INDEX IF NOT EXISTS idx_vh_user ON verification_history(user_id, redeemed_at)')


async def record(uid, sent_at, elapsed, provider, event_id):
    key = hashlib.sha256(f'{int(uid)}:{event_id}'.encode()).hexdigest()
    row = {'user_id': int(uid), 'sent_at': float(sent_at) if sent_at else None,
           'elapsed': round(float(elapsed), 1) if elapsed is not None else None,
           'provider': str(provider or 'unknown')[:40], 'redeemed_at': time.time()}
    if repo._mongo():
        from .. import mongo_db
        async def op(db):
            result = await db.verification_history.update_one(
                {'_id': key}, {'$setOnInsert': row}, upsert=True)
            return result.upserted_id is not None
        return await mongo_db.with_retry(op)
    _ensure_sql()
    return bool(repo.execute('INSERT OR IGNORE INTO verification_history VALUES(?,?,?,?,?,?)',
        (key, row['user_id'], row['sent_at'], row['elapsed'], row['provider'], row['redeemed_at'])))


async def history(uid, page=1, page_size=20):
    page, page_size = max(1, int(page)), max(1, min(100, int(page_size)))
    skip = (page - 1) * page_size
    if repo._mongo():
        from .. import mongo_db
        async def op(db):
            filt = {'user_id': int(uid)}
            total = await db.verification_history.count_documents(filt)
            rows = await db.verification_history.find(filt).sort([
                ('redeemed_at', -1), ('_id', -1)]).skip(skip).limit(page_size).to_list(length=page_size)
            return rows, total
        return await mongo_db.with_retry(op)
    _ensure_sql()
    total = repo.query_scalar('SELECT count(*) FROM verification_history WHERE user_id=?', (int(uid),), 0)
    rows = repo.query_all('SELECT * FROM verification_history WHERE user_id=? '
                          'ORDER BY redeemed_at DESC,event_id DESC LIMIT ? OFFSET ?',
                          (int(uid), page_size, skip))
    return rows, total
