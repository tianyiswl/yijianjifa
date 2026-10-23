import hashlib
import json
import re
import secrets
import uuid

from .errors import CloudError

PACK = {'price_fen':1990, 'words':10000}
_KEY = re.compile(r'^[A-Za-z0-9_.:-]{8,128}$')


def _code_hash(code):
    if not isinstance(code,str):
        raise CloudError('invalid_code','兑换码无效，请检查后重试。')
    normalized = code.replace('-','').replace(' ','').upper()
    if not re.fullmatch(r'[0-9A-F]{40}',normalized):
        raise CloudError('invalid_code','兑换码无效，请检查后重试。')
    return hashlib.sha256(normalized.encode()).hexdigest()


class CreditService:
    def __init__(self,store,*,operator_code_key=None):
        self.store=store
        self._code_cipher=None
        if operator_code_key is not None:
            # The optional operator dependency and key are never needed by the
            # customer API, legacy CLI, or speech generation services.
            from cryptography.fernet import Fernet
            try:
                self._code_cipher=Fernet(operator_code_key)
            except (TypeError,ValueError):
                raise ValueError('经营方兑换码保管密钥无效。') from None

    def balance(self,account_id):
        conn=self.store.connect()
        try:
            row=conn.execute('SELECT id,username,credits,used,reserved FROM accounts WHERE id=?',(account_id,)).fetchone()
            if not row:
                raise CloudError('session_expired','配音账号登录已过期，请重新登录。',401)
            return {'account_id':row['id'],'username':row['username'],
                    'available_words':row['credits']-row['used']-row['reserved'],
                    'reserved_words':row['reserved'],'used_words':row['used'],'pack':dict(PACK)}
        finally:
            conn.close()

    def issue(self,count,*,order_ref,purchase_id=None):
        if not isinstance(count,int) or isinstance(count,bool) or not 1<=count<=500:
            raise CloudError('invalid_count','单次发码数量需为1至500。')
        if not isinstance(order_ref,str) or not order_ref.strip() or len(order_ref)>128:
            raise CloudError('invalid_order_ref','请填写有效的收款订单关联号。')
        order_ref=order_ref.strip()
        note=order_ref
        if purchase_id is not None:
            if not isinstance(purchase_id,str) or not re.fullmatch(r'[0-9a-f]{32}',purchase_id):
                raise CloudError('invalid_request','本次购买编号无效，请重新打开管理页。')
            order_ref='purchase:'+purchase_id
        issued=[]
        with self.store.transaction() as conn:
            if purchase_id is not None:
                saved=conn.execute('SELECT note FROM code_purchase_notes WHERE order_ref=?',(order_ref,)).fetchone()
                if saved and saved['note']!=note:
                    raise CloudError('purchase_conflict','同一笔购买的备注不能改变，请先核对记录。',409)
                conn.execute('INSERT OR IGNORE INTO code_purchase_notes(order_ref,note) VALUES(?,?)',(order_ref,note))
            order=conn.execute('SELECT * FROM code_issuance_orders WHERE order_ref=?',(order_ref,)).fetchone()
            previous=conn.execute('SELECT * FROM redemption_codes WHERE order_ref=? ORDER BY rowid',(order_ref,)).fetchall()
            if order or previous:
                expected_count=order['code_count'] if order else len(previous)
                if expected_count!=count:
                    raise CloudError('order_count_conflict','此订单已发码，数量与原订单不一致；请核对原订单，不能重复发码。',409)
                if (not order or len(previous)!=count or
                        any(row['batch_id']!=order['batch_id'] or not row['encrypted_code'] for row in previous)):
                    raise CloudError('legacy_codes_unavailable','此订单已发码，但原码未保管；请查找原交付文件，不能重复发码。',409)
                return [self._issued(row,self._reveal(row),replayed=True) for row in previous]
            batch_id=str(uuid.uuid4())
            now=self.store.clock()
            conn.execute('INSERT INTO code_issuance_orders(order_ref,batch_id,code_count,created_at) VALUES(?,?,?,?)',
                         (order_ref,batch_id,count,now))
            for _ in range(count):
                value=secrets.token_hex(20).upper()
                code='-'.join(value[i:i+5] for i in range(0,40,5))
                code_id=str(uuid.uuid4())
                encrypted=(self._code_cipher.encrypt(json.dumps({'id':code_id,'code':code},separators=(',',':')).encode()).decode()
                           if self._code_cipher else None)
                conn.execute('''INSERT INTO redemption_codes(id,code_hash,suffix,batch_id,order_ref,
                    price_fen,words,created_at,encrypted_code) VALUES(?,?,?,?,?,?,?,?,?)''',
                    (code_id,_code_hash(code),value[-5:],batch_id,order_ref,1990,10000,now,encrypted))
                issued.append({'id':code_id,'code':code,'batch_id':batch_id,**PACK,'replayed':False})
        return issued

    @staticmethod
    def _issued(row,code,*,replayed):
        return {'id':row['id'],'code':code,'batch_id':row['batch_id'],
                'price_fen':row['price_fen'],'words':row['words'],'replayed':replayed}

    @staticmethod
    def _usable(row):
        if row['revoked']:
            raise CloudError('code_revoked','此兑换码已作废，不能再交付或复制。',409)
        if row['redeemed_by']:
            raise CloudError('already_redeemed','此兑换码已兑换，不能再次交付或复制。',409)

    def _reveal(self,row):
        self._usable(row)
        if not self._code_cipher or not row['encrypted_code']:
            raise CloudError('code_copy_unavailable','原码无法读取，请核对经营方密钥或原交付文件；不要重复发码。',409)
        from cryptography.fernet import InvalidToken
        try:
            value=json.loads(self._code_cipher.decrypt(row['encrypted_code'].encode()))
            if not isinstance(value,dict) or value.get('id')!=row['id'] or _code_hash(value.get('code'))!=row['code_hash']:
                raise ValueError()
            return value['code']
        except (InvalidToken,UnicodeError,ValueError,TypeError,AttributeError,CloudError):
            raise CloudError('code_copy_unavailable','原码无法读取，请核对经营方密钥或原交付文件；不要重复发码。',409) from None

    def reveal_code(self,code_id):
        conn=self.store.connect()
        try:
            row=conn.execute('SELECT * FROM redemption_codes WHERE id=?',(code_id,)).fetchone()
            if not row:
                raise CloudError('code_not_found','未找到该兑换码记录。',404)
            return self._issued(row,self._reveal(row),replayed=False)
        finally:
            conn.close()

    def mark_delivered(self,code_id):
        with self.store.transaction() as conn:
            row=conn.execute('SELECT * FROM redemption_codes WHERE id=?',(code_id,)).fetchone()
            if not row:
                raise CloudError('code_not_found','未找到该兑换码记录。',404)
            self._usable(row)
            conn.execute('UPDATE redemption_codes SET delivered_at=COALESCE(delivered_at,?) WHERE id=?',
                         (self.store.clock(),code_id))
        return self.code_status(code_id)

    def _status(self,row):
        result=dict(row)
        has_cipher=bool(result.pop('encrypted_code'))
        status=('revoked' if result['revoked'] else 'redeemed' if result['redeemed_by'] else
                'delivered' if result['delivered_at'] is not None else 'unused')
        return dict(result,status=status,can_copy=bool(self._code_cipher and has_cipher and status in ('unused','delivered')))

    def list_codes(self,page=0,*,page_size=20,search='',status='all'):
        if (not isinstance(page,int) or isinstance(page,bool) or not 0<=page<=100000 or
                not isinstance(page_size,int) or isinstance(page_size,bool) or not 1<=page_size<=100):
            raise CloudError('invalid_page','页码或每页数量无效。')
        if not isinstance(search,str) or len(search)>128:
            raise CloudError('invalid_search','查询内容最多128位。')
        if not isinstance(status,str) or status not in ('all','unused','delivered','redeemed','revoked'):
            raise CloudError('invalid_status','兑换码状态无效。')
        filters=[]
        values=[]
        if status=='revoked':
            filters.append('c.revoked=1')
        elif status!='all':
            filters.append('c.revoked=0')
            filters.append('c.redeemed_by IS NOT NULL' if status=='redeemed' else 'c.redeemed_by IS NULL')
            if status in ('unused','delivered'):
                filters.append('c.delivered_at IS NULL' if status=='unused' else 'c.delivered_at IS NOT NULL')
        if search.strip():
            literal=search.strip().replace('\\','\\\\').replace('%','\\%').replace('_','\\_')
            filters.append("(COALESCE(n.note,c.order_ref) LIKE ? ESCAPE '\\' OR c.suffix LIKE ? ESCAPE '\\' OR "
                           "c.id LIKE ? ESCAPE '\\' OR a.username LIKE ? ESCAPE '\\')")
            values.extend(['%'+literal+'%']*4)
        where=' WHERE '+' AND '.join(filters) if filters else ''
        tables=' FROM redemption_codes c LEFT JOIN accounts a ON a.id=c.redeemed_by LEFT JOIN code_purchase_notes n ON n.order_ref=c.order_ref'
        conn=self.store.connect()
        try:
            total=conn.execute('SELECT COUNT(*)'+tables+where,values).fetchone()[0]
            rows=conn.execute('''SELECT c.id,c.suffix,c.batch_id,COALESCE(n.note,c.order_ref) AS order_ref,c.price_fen,c.words,
                c.created_at,c.revoked,c.redeemed_by,c.redeemed_at,c.delivered_at,
                c.encrypted_code,a.username AS redeemed_username'''+tables+where+
                ' ORDER BY c.created_at DESC,c.id DESC LIMIT ? OFFSET ?',(*values,page_size,page*page_size)).fetchall()
            return {'items':[self._status(row) for row in rows],'page':page,'page_size':page_size,'total':total}
        finally:
            conn.close()

    def revoke(self,code_id):
        with self.store.transaction() as conn:
            row=conn.execute('SELECT redeemed_by FROM redemption_codes WHERE id=?',(code_id,)).fetchone()
            if not row:
                raise CloudError('code_not_found','未找到该兑换码记录。',404)
            if row['redeemed_by']:
                raise CloudError('already_redeemed','已兑换的码不能作废。',409)
            conn.execute('UPDATE redemption_codes SET revoked=1 WHERE id=?',(code_id,))
        return {'revoked':True}

    def code_status(self,code_id):
        conn=self.store.connect()
        try:
            row=conn.execute('''SELECT c.id,c.suffix,c.batch_id,COALESCE(n.note,c.order_ref) AS order_ref,c.price_fen,c.words,c.created_at,
                c.revoked,c.redeemed_by,c.redeemed_at,c.delivered_at,c.encrypted_code,a.username AS redeemed_username
                FROM redemption_codes c LEFT JOIN accounts a ON a.id=c.redeemed_by LEFT JOIN code_purchase_notes n ON n.order_ref=c.order_ref WHERE c.id=?''',(code_id,)).fetchone()
            if not row:
                raise CloudError('code_not_found','未找到该兑换码记录。',404)
            return self._status(row)
        finally:
            conn.close()

    def redeem(self,account_id,code,idempotency_key):
        if not isinstance(idempotency_key,str) or not _KEY.fullmatch(idempotency_key):
            raise CloudError('invalid_request_key','充值请求编号无效，请重新打开充值窗口。')
        hashed=_code_hash(code)
        with self.store.transaction() as conn:
            previous=conn.execute('''SELECT * FROM redemptions WHERE account_id=? AND idempotency_key=?''',
                                  (account_id,idempotency_key)).fetchone()
            if previous:
                if previous['code_hash']!=hashed:
                    raise CloudError('idempotency_conflict','本次充值请求内容已变更，请重新打开充值窗口。',409)
                return dict(json.loads(previous['receipt_json']),replayed=True)
            row=conn.execute('SELECT * FROM redemption_codes WHERE code_hash=?',(hashed,)).fetchone()
            if not row or row['revoked'] or row['redeemed_by']:
                raise CloudError('invalid_code','兑换码无效或已使用，请核对原订单。',409)
            account=conn.execute('SELECT * FROM accounts WHERE id=?',(account_id,)).fetchone()
            if not account:
                raise CloudError('session_expired','配音账号登录已过期，请重新登录。',401)
            if row['words']!=10000 or row['price_fen']!=1990:
                raise CloudError('invalid_pack','兑换套餐无法使用，请联系经营方。',409)
            now=self.store.clock()
            available=account['credits']+10000-account['used']-account['reserved']
            receipt={'id':str(uuid.uuid4()),'added_words':10000,'available_words':available,
                     'price_fen':1990,'created_at':now,'replayed':False}
            conn.execute('UPDATE accounts SET credits=credits+10000 WHERE id=?',(account_id,))
            conn.execute('UPDATE redemption_codes SET redeemed_by=?,redeemed_at=? WHERE id=?',(account_id,now,row['id']))
            conn.execute('''INSERT INTO ledger(id,account_id,event_key,kind,delta,available_after,purpose,created_at)
                            VALUES(?,?,?,?,?,?,?,?)''',
                         (receipt['id'],account_id,f'redemption:{row["id"]}','redemption',10000,available,'兑换码充值',now))
            conn.execute('INSERT INTO redemptions(id,account_id,idempotency_key,code_hash,receipt_json) VALUES(?,?,?,?,?)',
                         (receipt['id'],account_id,idempotency_key,hashed,json.dumps(receipt,ensure_ascii=False)))
            return receipt

    def usage(self,account_id,page=0,*,page_size=30):
        if not isinstance(page,int) or isinstance(page,bool) or not 0<=page<=100000:
            raise CloudError('invalid_page','页码无效。')
        conn=self.store.connect()
        try:
            total=conn.execute('SELECT COUNT(*) FROM ledger WHERE account_id=?',(account_id,)).fetchone()[0]
            rows=conn.execute('''SELECT id,kind,delta,available_after,purpose,created_at FROM ledger
                WHERE account_id=? ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?''',
                (account_id,page_size,page*page_size)).fetchall()
            return {'items':[dict(row) for row in rows],'page':page,'page_size':page_size,'total':total}
        finally:
            conn.close()
