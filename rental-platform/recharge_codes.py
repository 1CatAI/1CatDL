"""Single-use prepaid codes. Plaintext is returned only by initial issuance."""
import json
import re
import secrets
from datetime import datetime, timezone


class RedemptionRateLimited(RuntimeError):
    def __init__(self, seconds):
        self.retry_after = max(1, int(seconds))
        super().__init__(f'尝试过于频繁，请在 {self.retry_after} 秒后再试')


class RechargeCodeMixin:
    @staticmethod
    def _initialize_recharge_codes(con):
        con.executescript("""
            CREATE TABLE IF NOT EXISTS recharge_batches (
                id TEXT PRIMARY KEY, actor TEXT NOT NULL REFERENCES users(name),
                idempotency TEXT NOT NULL, intent TEXT NOT NULL, created_at TEXT NOT NULL,
                UNIQUE(actor,idempotency)
            );
            CREATE TABLE IF NOT EXISTS recharge_codes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id TEXT NOT NULL REFERENCES recharge_batches(id),
                code_hash BLOB NOT NULL UNIQUE, code_hint TEXT NOT NULL,
                cents INTEGER NOT NULL CHECK(cents>0),
                kind TEXT NOT NULL CHECK(kind IN ('cash','gift')),
                bound_owner TEXT REFERENCES users(name), note TEXT NOT NULL,
                created_by TEXT NOT NULL REFERENCES users(name), created_at TEXT NOT NULL,
                redeemed_by TEXT REFERENCES users(name), redeemed_at TEXT,
                revoked_by TEXT REFERENCES users(name), revoked_at TEXT,
                CHECK(redeemed_at IS NULL OR revoked_at IS NULL)
            );
            CREATE INDEX IF NOT EXISTS recharge_code_batch ON recharge_codes(batch_id);
            CREATE TABLE IF NOT EXISTS recharge_attempts (
                owner TEXT PRIMARY KEY REFERENCES users(name),
                window_at TEXT NOT NULL, failures INTEGER NOT NULL DEFAULT 0
            );
        """)

    @staticmethod
    def _code_public(row):
        return {
            'id': row['id'], 'batchId': row['batch_id'], 'hint': row['code_hint'],
            'cents': row['cents'], 'kind': row['kind'], 'boundOwner': row['bound_owner'],
            'note': row['note'], 'createdBy': row['created_by'], 'createdAt': row['created_at'],
            'redeemedBy': row['redeemed_by'], 'redeemedAt': row['redeemed_at'],
            'revokedBy': row['revoked_by'], 'revokedAt': row['revoked_at'],
            'status': 'redeemed' if row['redeemed_at'] else 'revoked' if row['revoked_at'] else 'available',
        }

    def issue_recharge_codes(self, admin, cents, count, kind, bound_owner, note, idempotency):
        if not self.is_admin(admin):
            raise PermissionError('admin required')
        if type(cents) is not int or not 1 <= cents <= 1_000_000:
            raise ValueError('单张面额须为 0.01–10000 元')
        if type(count) is not int or not 1 <= count <= 100 or cents * count > 10_000_000:
            raise ValueError('每批 1–100 张，总额不超过 100000 元')
        if kind not in ('cash', 'gift'):
            raise ValueError('请选择已收款充值或赠送额度')
        if not isinstance(note, str) or len(note.strip()) > 120:
            raise ValueError('备注不得超过 120 字')
        note = note.strip()
        if kind == 'cash' and not note:
            raise ValueError('已收款充值须填写收款编号或核验备注')
        if bound_owner is not None and not isinstance(bound_owner, str):
            raise ValueError('绑定账户格式错误')
        bound_owner = (bound_owner or '').strip() or None
        if not isinstance(idempotency, str) or not 8 <= len(idempotency) <= 128:
            raise ValueError('idempotency key required')
        intent = json.dumps([cents, count, kind, bound_owner, note], ensure_ascii=False)
        with self._transaction() as con:
            previous = con.execute('SELECT id,intent FROM recharge_batches WHERE actor=? AND idempotency=?', (admin,idempotency)).fetchone()
            if previous:
                if previous['intent'] != intent:
                    raise ValueError('请求编号已用于其他生成参数，请重新确认')
                rows = con.execute('SELECT * FROM recharge_codes WHERE batch_id=? ORDER BY id', (previous['id'],)).fetchall()
                return {'batchId': previous['id'], 'replayed': True, 'codes': [self._code_public(r) for r in rows]}
            if bound_owner and not con.execute("SELECT 1 FROM users WHERE name=? AND role='customer'", (bound_owner,)).fetchone():
                raise ValueError('绑定客户账户不存在')
            now = datetime.now(timezone.utc).isoformat()
            batch_id = secrets.token_hex(12)
            con.execute('INSERT INTO recharge_batches VALUES (?,?,?,?,?)', (batch_id,admin,idempotency,intent,now))
            issued = []
            for _ in range(count):
                token = secrets.token_hex(16).upper()  # 128 bits, independent of IDs.
                canonical = '1CAT' + token
                code = '1CAT-' + '-'.join(token[n:n+4] for n in range(0,32,4))
                ident = con.execute('''INSERT INTO recharge_codes
                    (batch_id,code_hash,code_hint,cents,kind,bound_owner,note,created_by,created_at)
                    VALUES (?,?,?,?,?,?,?,?,?)''',
                    (batch_id,self._hash_secret(canonical),'1CAT-••••-…-'+token[-4:],cents,kind,bound_owner,note,admin,now)).lastrowid
                row = con.execute('SELECT * FROM recharge_codes WHERE id=?', (ident,)).fetchone()
                issued.append({**self._code_public(row), 'code': code})
            self._audit(con,admin,'recharge_codes_issued',batch_id, {'count':count,'cents':cents,'kind':kind,'boundOwner':bound_owner})
        return {'batchId': batch_id, 'replayed': False, 'codes': issued}

    def list_recharge_codes(self, admin, before=0, status='all', query=''):
        if not self.is_admin(admin):
            raise PermissionError('admin required')
        if type(before) is not int or before < 0 or status not in ('all','available','redeemed','revoked'):
            raise ValueError('invalid code filter')
        if not isinstance(query, str) or len(query) > 120:
            raise ValueError('搜索内容过长')
        conditions, params = ['1=1'], []
        if before:
            conditions.append('id<?'); params.append(before)
        filters = {'available':'redeemed_at IS NULL AND revoked_at IS NULL', 'redeemed':'redeemed_at IS NOT NULL', 'revoked':'revoked_at IS NOT NULL'}
        if status != 'all': conditions.append(filters[status])
        if query.strip():
            conditions.append("(batch_id LIKE ? OR COALESCE(bound_owner,'') LIKE ? OR COALESCE(redeemed_by,'') LIKE ? OR note LIKE ? OR CAST(id AS TEXT)=?)")
            params.extend(['%'+query.strip()+'%']*4 + [query.strip()])
        with self._connection() as con:
            rows = con.execute('SELECT * FROM recharge_codes WHERE '+ ' AND '.join(conditions)+' ORDER BY id DESC LIMIT 51',params).fetchall()
            totals = con.execute('''SELECT COUNT(*) total,
                COALESCE(SUM(CASE WHEN redeemed_at IS NULL AND revoked_at IS NULL THEN cents ELSE 0 END),0) availableCents,
                COALESCE(SUM(CASE WHEN redeemed_at IS NOT NULL AND kind='cash' THEN cents ELSE 0 END),0) redeemedCashCents,
                COALESCE(SUM(CASE WHEN redeemed_at IS NOT NULL AND kind='gift' THEN cents ELSE 0 END),0) redeemedGiftCents
                FROM recharge_codes''').fetchone()
        return {'codes':[self._code_public(r) for r in rows[:50]], 'nextCursor':rows[49]['id'] if len(rows)>50 else None, 'summary':dict(totals)}

    def revoke_recharge_codes(self, admin, *, code_id=None, batch_id=None):
        if not self.is_admin(admin): raise PermissionError('admin required')
        if (code_id is None) == (batch_id is None): raise ValueError('choose a code or batch')
        if code_id is not None and (type(code_id) is not int or code_id < 1): raise ValueError('invalid code id')
        if batch_id is not None and (not isinstance(batch_id,str) or not re.fullmatch('[0-9a-f]{24}',batch_id)): raise ValueError('invalid batch id')
        column, value = ('id',code_id) if code_id is not None else ('batch_id',batch_id)
        with self._transaction() as con:
            if not con.execute(f'SELECT 1 FROM recharge_codes WHERE {column}=?', (value,)).fetchone(): raise KeyError('code not found')
            count = con.execute(f'''UPDATE recharge_codes SET revoked_at=?,revoked_by=? WHERE {column}=?
                AND redeemed_at IS NULL AND revoked_at IS NULL''', (datetime.now(timezone.utc).isoformat(),admin,value)).rowcount
            if count: self._audit(con,admin,'recharge_codes_revoked',str(value),{'count':count,'batch':batch_id is not None})
        return {'revoked':count}

    def redeem_recharge_code(self, owner, code):
        error, result = None, None
        with self._transaction() as con:
            user = con.execute("SELECT balance_cents FROM users WHERE name=? AND role='customer'",(owner,)).fetchone()
            if user is None: raise PermissionError('仅客户账户可以兑换充值码')
            now = datetime.now(timezone.utc)
            limit = con.execute('SELECT * FROM recharge_attempts WHERE owner=?',(owner,)).fetchone()
            elapsed = (now-datetime.fromisoformat(limit['window_at'])).total_seconds() if limit else 600
            if limit and elapsed < 600 and limit['failures'] >= 10:
                raise RedemptionRateLimited(600-elapsed)
            if limit is None or elapsed >= 600:
                con.execute('INSERT OR REPLACE INTO recharge_attempts(owner,window_at,failures) VALUES (?,?,0)',(owner,now.isoformat()))
            canonical = re.sub(r'[\s-]','',code).upper() if isinstance(code,str) and len(code)<=128 else ''
            row = con.execute('SELECT * FROM recharge_codes WHERE code_hash=?',(self._hash_secret(canonical),)).fetchone() if re.fullmatch(r'1CAT[0-9A-F]{32}',canonical) else None
            if row is None or row['revoked_at'] or (row['bound_owner'] and row['bound_owner'] != owner) or (row['redeemed_by'] and row['redeemed_by'] != owner):
                con.execute('UPDATE recharge_attempts SET failures=failures+1 WHERE owner=?',(owner,))
                error = '兑换码无效、不可用或不属于当前账户，请核对后重试'
            else:
                already = row['redeemed_by'] == owner
                if not already:
                    con.execute('UPDATE recharge_codes SET redeemed_by=?,redeemed_at=? WHERE id=?',(owner,now.isoformat(),row['id']))
                    con.execute('UPDATE users SET balance_cents=balance_cents+? WHERE name=?',(row['cents'],owner))
                    con.execute('''INSERT INTO billing_entries(owner,cents,reason,created_at,actor,idempotency)
                        VALUES (?,?,?,?,?,?)''',(owner,row['cents'],'redeem_'+row['kind']+':'+str(row['id']),now.isoformat(),'recharge-code','code:'+str(row['id'])))
                    self._audit(con,owner,'recharge_code_redeemed',str(row['id']),{'cents':row['cents'],'kind':row['kind'],'batchId':row['batch_id']})
                con.execute('DELETE FROM recharge_attempts WHERE owner=?',(owner,))
                balance = con.execute('SELECT balance_cents FROM users WHERE name=?',(owner,)).fetchone()[0]
                result = {'id':row['id'],'cents':row['cents'],'kind':row['kind'],'balanceCents':balance,'alreadyRedeemed':already}
        # Invalid attempts must commit, not roll back their rate-limit counter.
        if error: raise ValueError(error)
        return result
