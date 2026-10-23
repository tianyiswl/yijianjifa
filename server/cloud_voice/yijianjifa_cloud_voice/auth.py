import hashlib
import hmac
import re
import secrets
import sqlite3
import uuid

from .errors import CloudError

_USERNAME = re.compile(r'^[A-Za-z0-9_]{4,32}$')
_ITERATIONS = 600_000


def _digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def _password(value):
    if not isinstance(value, str) or not 6 <= len(value) <= 128:
        raise CloudError('invalid_password', '密码需为6至128位。')
    return value


def _hash_password(value):
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac('sha256', value.encode(), salt.encode(), _ITERATIONS).hex()
    return f'pbkdf2_sha256${_ITERATIONS}${salt}${digest}'


def _check_password(value, hashed):
    if not isinstance(value, str) or len(value) > 128:
        return False
    try:
        algorithm, iterations, salt, expected = hashed.split('$')
        if algorithm != 'pbkdf2_sha256' or int(iterations) != _ITERATIONS:
            return False
        actual = hashlib.pbkdf2_hmac('sha256', value.encode(), salt.encode(), int(iterations)).hex()
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError, AttributeError):
        return False


class AuthService:
    def __init__(self, store, *, access_seconds=900, refresh_seconds=30*86400):
        self.store = store
        self.access_seconds = access_seconds
        self.refresh_seconds = refresh_seconds
        # Dummy hash makes invalid account and invalid password take similar work.
        self._dummy_hash = _hash_password(secrets.token_urlsafe(24))

    def register(self, username, password):
        username = self._username(username)
        hashed = _hash_password(_password(password))
        account_id = str(uuid.uuid4())
        try:
            with self.store.transaction() as conn:
                conn.execute('INSERT INTO accounts(id,username,password_hash,created_at) VALUES(?,?,?,?)',
                             (account_id, username, hashed, self.store.clock()))
                return self._session(conn, account_id, username)
        except sqlite3.IntegrityError:
            raise CloudError('account_unavailable', '该账号名暂不可用，请换一个。', 409) from None

    @staticmethod
    def _username(username):
        if not isinstance(username, str) or not _USERNAME.fullmatch(username):
            raise CloudError('invalid_username', '账号需为4至32位，可使用字母、数字或下划线。')
        return username.casefold()

    def login(self, username, password):
        normalized = username.casefold() if isinstance(username, str) else ''
        conn = self.store.connect()
        try:
            account = conn.execute('SELECT * FROM accounts WHERE username=?', (normalized,)).fetchone()
        finally:
            conn.close()
        valid = _check_password(password, account['password_hash'] if account else self._dummy_hash)
        if not account or not valid:
            raise CloudError('invalid_credentials', '账号或密码不正确。', 401)
        with self.store.transaction() as conn:
            # Recheck if password was rotated while its hash was being verified.
            current = conn.execute('SELECT password_hash FROM accounts WHERE id=?', (account['id'],)).fetchone()
            if not current or current['password_hash'] != account['password_hash']:
                raise CloudError('invalid_credentials', '账号或密码不正确。', 401)
            return self._session(conn, account['id'], account['username'])

    def _session(self, conn, account_id, username, family=None):
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
        now = self.store.clock()
        conn.execute('''INSERT INTO sessions(id,family,account_id,access_hash,refresh_hash,
                        access_expires,refresh_expires,created_at) VALUES(?,?,?,?,?,?,?,?)''',
                     (str(uuid.uuid4()), family or str(uuid.uuid4()), account_id,
                      _digest(access), _digest(refresh), now+self.access_seconds, now+self.refresh_seconds, now))
        return {'account_id':account_id, 'username':username, 'access_token':access, 'refresh_token':refresh}

    def authenticate(self, access_token):
        if not isinstance(access_token, str) or not access_token or len(access_token) > 256:
            raise CloudError('session_expired', '配音账号登录已过期，请重新登录。', 401)
        conn = self.store.connect()
        try:
            row = conn.execute('SELECT * FROM sessions WHERE access_hash=?', (_digest(access_token),)).fetchone()
            if not row or row['revoked'] or row['used'] or row['access_expires'] <= self.store.clock():
                raise CloudError('session_expired', '配音账号登录已过期，请重新登录。', 401)
            return row['account_id']
        finally:
            conn.close()

    def refresh(self, refresh_token):
        if not isinstance(refresh_token, str) or not refresh_token or len(refresh_token) > 256:
            raise CloudError('session_expired', '配音账号登录已过期，请重新登录。', 401)
        failure = False
        result = None
        with self.store.transaction() as conn:
            row = conn.execute('SELECT * FROM sessions WHERE refresh_hash=?', (_digest(refresh_token),)).fetchone()
            if row and row['used']:
                conn.execute('UPDATE sessions SET revoked=1 WHERE family=?', (row['family'],))
                failure = True
            elif not row or row['revoked'] or row['refresh_expires'] <= self.store.clock():
                failure = True
            else:
                conn.execute('UPDATE sessions SET used=1,revoked=1 WHERE id=?', (row['id'],))
                username = conn.execute('SELECT username FROM accounts WHERE id=?', (row['account_id'],)).fetchone()['username']
                result = self._session(conn,row['account_id'],username,row['family'])
        if failure:
            raise CloudError('session_expired', '配音账号登录已过期，请重新登录。', 401)
        return result

    def logout(self, access_token):
        with self.store.transaction() as conn:
            row = conn.execute('SELECT family FROM sessions WHERE access_hash=?', (_digest(access_token),)).fetchone()
            if row:
                conn.execute('UPDATE sessions SET revoked=1 WHERE family=?', (row['family'],))
        return {'logged_out': True}

    def _verified_password(self, account_id, password):
        conn = self.store.connect()
        try:
            account = conn.execute('SELECT * FROM accounts WHERE id=?', (account_id,)).fetchone()
        finally:
            conn.close()
        if not account or not _check_password(password, account['password_hash']):
            raise CloudError('invalid_credentials', '账号或密码不正确。', 401)
        return account

    def _unchanged(self, conn, account):
        current = conn.execute('SELECT password_hash FROM accounts WHERE id=?', (account['id'],)).fetchone()
        if not current or current['password_hash'] != account['password_hash']:
            raise CloudError('session_expired', '配音账号信息已更新，请重新登录。', 401)

    def logout_all(self, account_id, password):
        account = self._verified_password(account_id, password)
        with self.store.transaction() as conn:
            self._unchanged(conn, account)
            conn.execute('UPDATE sessions SET revoked=1 WHERE account_id=?', (account_id,))
        return {'logged_out':True}

    def change_password(self, account_id, old_password, new_password):
        account = self._verified_password(account_id, old_password)
        hashed = _hash_password(_password(new_password))
        with self.store.transaction() as conn:
            self._unchanged(conn, account)
            conn.execute('UPDATE accounts SET password_hash=? WHERE id=?', (hashed,account_id))
            conn.execute('UPDATE sessions SET revoked=1 WHERE account_id=?', (account_id,))
        return {'changed':True}

    def export_recovery(self, account_id, password):
        account = self._verified_password(account_id, password)
        secret = secrets.token_urlsafe(48)
        with self.store.transaction() as conn:
            self._unchanged(conn, account)
            conn.execute('UPDATE accounts SET recovery_hash=? WHERE id=?', (_digest(secret),account_id))
        return {'account_id':account_id,'recovery_secret':secret}

    def recover(self, account_id, recovery_secret, new_password):
        if not isinstance(account_id,str) or not isinstance(recovery_secret,str) or len(recovery_secret)>256:
            raise CloudError('invalid_recovery', '恢复凭证无效，请检查原文件。', 401)
        hashed = _hash_password(_password(new_password))
        with self.store.transaction() as conn:
            account = conn.execute('SELECT * FROM accounts WHERE id=?',(account_id,)).fetchone()
            if not account or not account['recovery_hash'] or not hmac.compare_digest(account['recovery_hash'],_digest(recovery_secret)):
                raise CloudError('invalid_recovery', '恢复凭证无效，请检查原文件。', 401)
            conn.execute('UPDATE accounts SET password_hash=?,recovery_hash=NULL WHERE id=?',(hashed,account_id))
            conn.execute('UPDATE sessions SET revoked=1 WHERE account_id=?',(account_id,))
            return self._session(conn,account_id,account['username'])
